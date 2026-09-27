#!/usr/bin/env python3
"""Promote quotes-draft.json -> quotes.json (live).

Writes the draft's quotes into the live file exactly the way the admin UI's
publish action does: {"quotes": [{id, text}, ...]}, 2-space indent, raw UTF-8,
no trailing newline (byte-identical output, so the widget parses it the same).

Safety properties:
- Validates both files before writing anything.
- Refuses to promote an empty or malformed draft (would wipe live quotes).
- Never modifies the draft file itself.
- Idempotent: exits 0 without committing when live already matches the draft.
- Optional one-shot date guard for scheduled runs (--only-date).

Exit codes: 0 = success (including no-op / skipped), 1 = validation or git error.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
LIVE_PATH = REPO_ROOT / "quotes.json"
DRAFT_PATH = REPO_ROOT / "quotes-draft.json"

COMMIT_MESSAGE = "Promote draft quotes to live (automated)"
GIT_AUTHOR_NAME = "github-actions[bot]"
GIT_AUTHOR_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class PromotionError(Exception):
    """Any validation or execution failure that must abort the promotion."""


def load_json(path: Path) -> dict:
    if not path.exists():
        raise PromotionError(f"{path.name} not found")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"{path.name} is not valid JSON: {exc}")


def normalize_quotes(quotes: object, source: str) -> list[dict]:
    """Validate the quotes array and reduce each entry to {id, text}."""
    if not isinstance(quotes, list):
        raise PromotionError(
            f"{source}: 'quotes' must be a list, got {type(quotes).__name__}"
        )
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for index, quote in enumerate(quotes):
        if not isinstance(quote, dict):
            raise PromotionError(f"{source}: quotes[{index}] is not an object")
        qid = quote.get("id")
        text = quote.get("text")
        if not isinstance(qid, str) or not qid:
            raise PromotionError(
                f"{source}: quotes[{index}] is missing a non-empty string 'id'"
            )
        if not isinstance(text, str) or not text:
            raise PromotionError(
                f"{source}: quotes[{index}] ({qid}) is missing a non-empty string 'text'"
            )
        if qid in seen_ids:
            raise PromotionError(f"{source}: duplicate quote id '{qid}'")
        seen_ids.add(qid)
        normalized.append({"id": qid, "text": text})
    return normalized


def admin_encoded(data: dict) -> str:
    """Base64 in the exact format the admin UI uses for GitHub content."""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def summarize_changes(live_quotes: list[dict], draft_quotes: list[dict]) -> str:
    live_by_id = {q["id"]: q["text"] for q in live_quotes}
    draft_by_id = {q["id"]: q["text"] for q in draft_quotes}
    added = sum(1 for qid in draft_by_id if qid not in live_by_id)
    removed = sum(1 for qid in live_by_id if qid not in draft_by_id)
    edited = sum(
        1
        for qid in draft_by_id
        if qid in live_by_id and draft_by_id[qid] != live_by_id[qid]
    )
    kept = len(draft_by_id) - added - edited
    return (
        f"{len(draft_by_id)} quotes total: "
        f"{kept} unchanged, {edited} edited, {added} added, {removed} removed"
    )


def write_live_file(new_live: dict) -> None:
    text = json.dumps(new_live, ensure_ascii=False, indent=2)
    LIVE_PATH.write_text(text, encoding="utf-8")
    # Post-write verification: the file must round-trip and match the exact
    # encoding the admin UI would have produced.
    reloaded = json.loads(LIVE_PATH.read_text(encoding="utf-8"))
    if reloaded != new_live:
        raise PromotionError(
            "post-write verification failed: quotes.json does not round-trip"
        )
    if admin_encoded(reloaded) != admin_encoded(new_live):
        raise PromotionError("post-write verification failed: encoding mismatch")


def git_commit_and_push() -> None:
    def run_git(*args: str) -> None:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise PromotionError(f"git {' '.join(args)} failed: {detail}")

    run_git("add", LIVE_PATH.name)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT).returncode == 0:
        print("No changes to commit — quotes.json already matches the draft.")
        return
    run_git(
        "-c",
        f"user.name={GIT_AUTHOR_NAME}",
        "-c",
        f"user.email={GIT_AUTHOR_EMAIL}",
        "commit",
        "-m",
        COMMIT_MESSAGE,
    )
    run_git("push", "origin", "HEAD")
    print("Committed and pushed to origin.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Promote quotes-draft.json to quotes.json"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="validate and report only; write nothing"
    )
    parser.add_argument(
        "--only-date",
        metavar="YYYY-MM-DD",
        help="scheduled-run guard: skip unless today (UTC) matches this date",
    )
    args = parser.parse_args()

    if args.only_date and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        today = datetime.now(timezone.utc).date().isoformat()
        if today != args.only_date:
            print(
                f"Scheduled run outside the intended date "
                f"({today} != {args.only_date}) — skipping."
            )
            return 0

    try:
        draft = load_json(DRAFT_PATH)
        live = load_json(LIVE_PATH) if LIVE_PATH.exists() else {"quotes": []}
        if not isinstance(live, dict) or not isinstance(live.get("quotes", []), list):
            raise PromotionError("quotes.json: expected an object with a 'quotes' list")

        draft_quotes = normalize_quotes(draft.get("quotes"), "quotes-draft.json")
        live_quotes = normalize_quotes(live.get("quotes", []), "quotes.json")

        if not draft_quotes:
            raise PromotionError(
                "the draft has no quotes — refusing to overwrite live quotes "
                "with an empty list"
            )

        if draft_quotes == live_quotes:
            print("Live quotes already match the draft — nothing to do.")
            return 0

        summary = summarize_changes(live_quotes, draft_quotes)
        print(summary)

        if args.dry_run:
            print("Dry run — quotes.json was NOT modified.")
            return 0

        new_live = dict(live)
        new_live["quotes"] = draft_quotes
        write_live_file(new_live)
        print("quotes.json updated and verified.")
        git_commit_and_push()

        step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if step_summary:
            with open(step_summary, "a", encoding="utf-8") as fh:
                fh.write(f"**Draft promoted to live** — {summary}\n")
        return 0
    except PromotionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
