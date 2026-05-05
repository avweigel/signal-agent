#!/usr/bin/env python3
"""
Phase 7 extraction: read new files since last cursor for a given source,
call Claude to extract tasks via tool-use, write to Supabase.

Step 2 scope: 'notes' source only. Other sources (mail-work, mail-personal,
calendar-game) added in step 3 with their own prompts.

Usage:
    python3 scripts/agent_extract.py --source notes --limit 5 --dry-run
    python3 scripts/agent_extract.py --source notes --limit 5
    python3 scripts/agent_extract.py --source notes
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from agent_common import (
    AGENT_DATA,
    EXTRACT_MODEL,
    SCHEMA,
    USER_ID,
    call_claude_with_retry,
    get_anthropic_client,
    get_supabase_client,
    setup_logging,
    write_run_state,
)

NOTES_DIR = AGENT_DATA / "notes"

# Strip data: URIs (base64-encoded inline images) before sending to Claude —
# they're huge and irrelevant for task extraction.
DATA_URI_RE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
MAX_BODY_CHARS = 8000
NOTES_BATCH_SIZE = 5

NOTES_SYSTEM_PROMPT = """\
You are extracting actionable tasks from a user's personal notes.

The user is a busy professional. Their notes contain a mix of:
- Self-authored TODOs (lists, action items, "things to follow up on...")
- Meeting notes with embedded action items
- Reference content (recipes, quotes, articles) — IGNORE these entirely
- Drafts and ideas without commitment — IGNORE unless explicitly marked TODO

Extract ONLY:
- Self-authored TODOs and action items the user wrote for themselves
- Follow-up reminders ("ping X about Y", "check status of Z")
- Pending decisions the user has explicitly noted as needing resolution
- Things they've explicitly promised someone they would do

DO NOT extract:
- Pure reference content (recipes, instructions, copied articles)
- Items already marked done (struck-through, "DONE", checked checkbox)
- Generic ideas or musings without commitment
- Tasks belonging to other people that the user is just tracking
- Imported social media posts (Instagram captions, etc.)

For each extracted task:
- title: short imperative-mood action ("Reply to Marwan re: budget", "Pick up dry cleaning"). Max ~80 chars.
- description: 1-2 sentences of context from the note (who/what/why)
- due_date: ISO date YYYY-MM-DD if explicitly mentioned in the note, else null
- priority: integer 1-5, where 1 is highest. Default 3. Reserve 1-2 for explicitly urgent or hard-deadline items.
- source_id: the source_id I provided for the note this came from

If a note has no actionable tasks, emit nothing for it. Always return via the
submit_tasks tool, even if the tasks array is empty.
"""

NOTES_TOOL: dict[str, Any] = {
    "name": "submit_tasks",
    "description": "Submit the list of tasks extracted from the input notes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "due_date": {
                            "type": ["string", "null"],
                            "description": "ISO date YYYY-MM-DD or null",
                        },
                        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                        "source_id": {"type": "string"},
                    },
                    "required": ["title", "description", "priority", "source_id"],
                },
            }
        },
        "required": ["tasks"],
    },
}


def strip_data_uris(text: str) -> str:
    return DATA_URI_RE.sub("[image]", text)


def load_note(path: Path) -> dict:
    """Read a note's frontmatter + body. Returns dict with id, title, folder, modified, body."""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        return {"id": "", "title": path.stem, "folder": "", "modified": "", "body": raw}
    end = raw.find("\n---\n", 4)
    if end == -1:
        return {"id": "", "title": path.stem, "folder": "", "modified": "", "body": raw}
    fm_text = raw[4:end]
    body = raw[end + 5:]
    fm: dict[str, str] = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip().strip('"')
    return {
        "id": fm.get("id", ""),
        "title": fm.get("title", path.stem),
        "folder": fm.get("folder", ""),
        "modified": fm.get("modified", ""),
        "body": body,
    }


def list_notes_since(cursor_mtime: float) -> list[Path]:
    """Return notes with mtime > cursor_mtime, newest first."""
    candidates: list[tuple[float, Path]] = []
    for p in NOTES_DIR.glob("*.md"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime > cursor_mtime:
            candidates.append((mtime, p))
    candidates.sort(reverse=True)  # newest first
    return [p for _, p in candidates]


def get_cursor(client, source: str) -> float:
    """Return last_seen_value as float (epoch). 0 if no cursor."""
    try:
        resp = client.schema(SCHEMA).table("cursors").select("*").eq("source_type", source).execute()
        if resp.data:
            return float(resp.data[0]["last_seen_value"])
    except Exception as e:
        logging.warning("Cursor read failed for %s: %s", source, e)
    return 0.0


def save_cursor(client, source: str, value: float) -> None:
    client.schema(SCHEMA).table("cursors").upsert({
        "source_type": source,
        "last_seen_value": str(value),
        "last_run_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


def build_user_message(notes: list[tuple[str, dict]]) -> str:
    """Format notes for the user message. Each note prefixed with its source_id."""
    parts = []
    for source_id, note in notes:
        body = strip_data_uris(note["body"])
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n\n[truncated]"
        parts.append(
            f"=== source_id: {source_id} ===\n"
            f"title: {note['title']}\n"
            f"folder: {note['folder']}\n"
            f"modified: {note['modified']}\n"
            f"---\n"
            f"{body}\n"
        )
    return "\n\n".join(parts)


def extract_tasks_for_batch(
    anthropic_client, notes: list[tuple[str, dict]]
) -> tuple[list[dict], dict]:
    """Call Claude on a batch. Returns (tasks_list, usage_dict)."""
    user_message = build_user_message(notes)

    def _call():
        return anthropic_client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=4096,
            metadata={"user_id": USER_ID},
            tools=[NOTES_TOOL],
            tool_choice={"type": "tool", "name": "submit_tasks"},
            system=NOTES_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

    response = call_claude_with_retry(_call)

    tasks: list[dict] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_tasks":
            tasks = block.input.get("tasks", []) or []
            break
    else:
        logging.warning("Model didn't call submit_tasks; got content blocks: %s",
                        [getattr(b, "type", None) for b in response.content])

    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return tasks, usage


def insert_tasks(supabase_client, tasks: list[dict]) -> int:
    """Insert tasks one at a time; silently skip duplicates."""
    if not tasks:
        return 0
    inserted = 0
    for t in tasks:
        try:
            supabase_client.schema(SCHEMA).table("tasks").insert(t).execute()
            inserted += 1
        except Exception as e:
            err = str(e).lower()
            if "duplicate" in err or "unique" in err or "23505" in err:
                continue  # silently skip dup on (source_type, source_path, title)
            logging.error("Insert failed for task %r: %s", t.get("title"), e)
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="notes", choices=["notes"],
                        help="Source to extract from (only 'notes' implemented in step 2)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N items")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be inserted; don't write to DB")
    args = parser.parse_args()

    setup_logging("agent_extract")
    started = datetime.now(timezone.utc)
    logging.info("Starting agent_extract source=%s limit=%s dry_run=%s",
                 args.source, args.limit, args.dry_run)

    if args.source != "notes":
        logging.error("Source %r not implemented yet (step 2 = notes only)", args.source)
        return 1

    supabase = get_supabase_client()
    anthropic = get_anthropic_client()

    cursor = 0.0 if args.limit else get_cursor(supabase, "notes")
    if args.limit:
        logging.info("--limit set; ignoring stored cursor")

    notes_paths = list_notes_since(cursor)
    if args.limit is not None:
        notes_paths = notes_paths[: args.limit]
    logging.info("Selected %d notes", len(notes_paths))

    if not notes_paths:
        logging.info("No new notes to process")
        write_run_state("agent_extract", {
            "started": started.isoformat(),
            "finished": datetime.now(timezone.utc).isoformat(),
            "source": args.source, "notes_processed": 0,
            "tasks_extracted": 0, "tasks_inserted": 0,
        })
        return 0

    notes: list[tuple[str, dict]] = [(p.stem, load_note(p)) for p in notes_paths]

    all_tasks: list[dict] = []
    total_usage = {"input_tokens": 0, "output_tokens": 0}
    max_mtime = cursor

    for i in range(0, len(notes), NOTES_BATCH_SIZE):
        batch = notes[i : i + NOTES_BATCH_SIZE]
        batch_paths = notes_paths[i : i + NOTES_BATCH_SIZE]
        logging.info("Batch %d: %d notes", i // NOTES_BATCH_SIZE + 1, len(batch))

        tasks, usage = extract_tasks_for_batch(anthropic, batch)
        total_usage["input_tokens"] += usage["input_tokens"]
        total_usage["output_tokens"] += usage["output_tokens"]
        logging.info(
            "  -> extracted %d tasks (in=%d out=%d tokens)",
            len(tasks), usage["input_tokens"], usage["output_tokens"],
        )

        sid_to_path = {sid: p for (sid, _), p in zip(batch, batch_paths)}
        for t in tasks:
            sid = t.get("source_id", "")
            note_path = sid_to_path.get(sid)
            if note_path is None:
                logging.warning("Task references unknown source_id %r; skipping", sid)
                continue
            try:
                rel_path = str(note_path.relative_to(AGENT_DATA))
            except ValueError:
                rel_path = str(note_path)
            all_tasks.append({
                "source_type": "notes",
                "source_path": rel_path,
                "source_id": sid,
                "title": (t.get("title") or "")[:500],
                "description": t.get("description") or "",
                "due_date": t.get("due_date") or None,
                "priority": int(t.get("priority", 3)),
            })

        for p in batch_paths:
            try:
                mtime = p.stat().st_mtime
                if mtime > max_mtime:
                    max_mtime = mtime
            except OSError:
                pass

    if args.dry_run:
        print()
        print("=" * 70)
        print(f"DRY RUN — would insert {len(all_tasks)} tasks")
        print("=" * 70)
        for t in all_tasks:
            print(json.dumps(t, indent=2))
            print()
        inserted = 0
    else:
        inserted = insert_tasks(supabase, all_tasks)
        logging.info("Inserted %d tasks (of %d extracted)", inserted, len(all_tasks))
        if args.limit is None:
            save_cursor(supabase, "notes", max_mtime)
            logging.info("Saved cursor: %s", max_mtime)

    finished = datetime.now(timezone.utc)
    summary = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "source": args.source,
        "limited": args.limit is not None,
        "dry_run": args.dry_run,
        "notes_processed": len(notes_paths),
        "tasks_extracted": len(all_tasks),
        "tasks_inserted": inserted,
        "usage": total_usage,
    }
    write_run_state("agent_extract", summary)
    logging.info(
        "Done. notes=%d tasks_extracted=%d tasks_inserted=%d in_tokens=%d out_tokens=%d",
        len(notes_paths), len(all_tasks), inserted,
        total_usage["input_tokens"], total_usage["output_tokens"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
