#!/usr/bin/env python3
"""
Phase 7 task extraction.

Reads dumped items (notes, mail-work, mail-personal), calls Claude (Haiku
4.5) to identify actionable tasks, and writes them to signal_agent.tasks.
Each task is loosely entity-linked: the model emits a best-guess
`primary_entity_name`, and a post-extraction pass resolves it against the
existing entity index to set `tasks.primary_entity_id`.

Per-source prompts: notes targets self-authored TODOs; mail targets
things the user owes/promised. Calendar will be added in a later
iteration.

Defaults are conservative: --since 90 days ago, --limit 50 items, $5
hard cost cap. Pass --full to lift the limit, --since to widen the
window, --max-cost to raise the cap.

Usage:
    python3 scripts/agent_extract.py                                 # notes, default scope
    python3 scripts/agent_extract.py --source notes --limit 30 --dry-run
    python3 scripts/agent_extract.py --source mail-work --since 2026-04-01 --limit 100
    python3 scripts/agent_extract.py --source mail-personal --since 2026-04-01 --limit 100
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from agent_common import (
    AGENT_DATA,
    EXTRACT_MODEL,
    SCHEMA,
    USER_ID,
    CostBudgetExceeded,
    CostTracker,
    cached_system,
    call_claude_with_retry,
    get_anthropic_client,
    get_supabase_client,
    load_agent_config,
    setup_logging,
    write_run_state,
)
# Reuse the source loaders from agent_index — same .emlx/.md handling
from agent_index import (
    BATCH_SIZE,
    MAIL_PERSONAL_DIR,
    MAIL_WORK_DIR,
    NOTES_DIR,
    list_mail_eml_since,
    list_notes_filtered,
    load_mail,
    load_note,
    strip_data_uris,
)

MAX_BODY_CHARS = 8000


# ---- per-source prompts -------------------------------------------------

NOTES_SYSTEM_TEMPLATE = """\
You are extracting actionable tasks from {user_full_name}'s personal notes.

WHAT COUNTS AS A TASK
- Self-authored TODOs the user wrote to themselves (lists, checkboxes,
  "things to follow up on...")
- Follow-up reminders ("ping X about Y", "check status of Z")
- Pending decisions explicitly noted as needing resolution
- Things the user has explicitly committed to do for someone

WHAT DOES NOT COUNT
- Reference content (recipes, articles, quotes, copied text, cheatsheets)
- Generic ideas or musings without explicit commitment
- Items already marked done (struck-through, "DONE", checked checkbox)
- Tasks belonging to other people that the user is just tracking
- Items {user_full_name} herself is the actor for, but the action is one-time
  and already happened (e.g., "wrote up notes" — past tense)

FOR EACH TASK
- title: short imperative-mood action ("Reply to Marwan re: budget",
  "Pick up dry cleaning"). Max ~80 chars.
- description: 1-2 sentences of context grounding the task. Include any
  vague time references from the source ("by April", "next week", "EOQ")
  here — keep them in description but DO NOT translate them into the
  due_date field.
- due_date: ISO date YYYY-MM-DD ONLY when the source contains an explicit
  unambiguous date. Otherwise null. Examples:
    - "by Friday April 18" → 2026-04-18 (assuming year is clear)
    - "deadline 2026-04-15" → 2026-04-15
    - "by April" → null  (month-only is NOT a specific date)
    - "by end of April" → null  (still imprecise)
    - "next week", "soon", "EOQ", "ASAP" → null
    - "before spring break" → null
  Better to have a null date than to fabricate. False-confidence deadlines
  are worse than missing deadlines.
- priority: integer 1-5, where 1 is highest. Default 3. Reserve 1-2 for
  explicitly urgent or hard-deadline items. Past-due triage (overdue/stale
  flagging) is applied programmatically AFTER your output, so do not
  attempt to mark stale items yourself — just emit the literal date.
- source_id: the source_id provided for the note this task came from.
- primary_entity_name: best guess at the most relevant person, project,
  or topic from the ENTITY INDEX section. Use the canonical name or any
  alias listed there. Null if no clear central entity.

If a note has no actionable tasks (reference content, blank, just
metadata), submit an empty tasks array — that's correct behavior.

{entity_index}

EXAMPLES

--- Example 1: meeting note with action items ---

INPUT:
=== source_id: 2026-04-08-wyatt-11 ===
title: 2026-04-08 Wyatt 1:1
folder: JLS Lab
modified: 2026-04-08T15:00:00Z
---
# Agenda
- Alyson's time for the segmentation work
- Software choices (Amira vs WebKnossos)
- Data access on the cluster
- Publication timeline for the ER paper

# Action items
- Aubrey: send formal request for Alyson time with hours estimate
- Aubrey: review WebKnossos vs Amira for {user_first_name}'s use case
- Wyatt: sort out cluster access permissions

EXPECTED tool call: submit_tasks({{
  "tasks": [
    {{
      "title": "Send formal request for Alyson's time with hours estimate",
      "description": "Per 1:1 with Wyatt — formalize ask for Alyson's allocation on segmentation work, with explicit hour count.",
      "due_date": null,
      "priority": 2,
      "source_id": "2026-04-08-wyatt-11",
      "primary_entity_name": "Alyson Petruncio"
    }},
    {{
      "title": "Compare WebKnossos vs Amira for use case",
      "description": "Discussed in Wyatt 1:1; pick the right tool for ongoing segmentation work.",
      "due_date": null,
      "priority": 3,
      "source_id": "2026-04-08-wyatt-11",
      "primary_entity_name": "Wyatt Korff"
    }}
  ]
}})

WHY:
- Wyatt's own action item ("Wyatt: sort out cluster access...") is NOT extracted —
  that's Wyatt's task, not the user's.
- Two user-actor action items become tasks with relevant entity links.
- Past-tense "discussed" content stays in description as context, not
  separate tasks.

--- Example 2: pure reference note ---

INPUT:
=== source_id: tylenol-dosage-5625f70b ===
title: Tylenol Dosage
folder: Notes
---
Children's Tylenol: 10-15 mg/kg every 4-6 hours. Max 5 doses per day.
Suspension: 160 mg / 5 mL.

EXPECTED tool call: submit_tasks({{"tasks": []}})

WHY: this is reference material the user keeps for lookup. No action.
"""


MAIL_SYSTEM_TEMPLATE = """\
You are extracting actionable tasks from {user_full_name}'s email
({source_kind}).

WHAT COUNTS AS A TASK
- Things {user_first_name} owes someone (someone explicitly asks for a reply,
  review, deliverable, decision, or action from her)
- Things {user_first_name} has explicitly promised in her replies (in this
  message or in a quoted-but-not-stripped fragment from her own prior reply)
- Hard deadlines mentioned in messages she's engaged with

WHAT DOES NOT COUNT
- FYI / informational threads where no action is requested of her
- Marketing, newsletters, promotional content
- Transactional notifications (receipts, shipping updates, login alerts,
  delivery confirmations, account updates)
- Mass announcements where she's one of many recipients without a specific
  ask directed at her
- Calendar invites with no action items in the description
- Auto-generated content (system notifications, "your package has shipped")

THINGS TO BE CAREFUL ABOUT
- The preprocessor strips quoted reply history before this message, but
  some quoted fragments may slip through. Don't extract tasks from
  obviously-old quoted content (especially ones that started "On <date>,
  <person> wrote:").
- A "Best, X" sign-off doesn't make X an entity to act on; look at the
  body request.
- Auto-replies and out-of-office messages: skip.
- An email's subject line counts toward task extraction (e.g., "Re: budget
  due Friday" implies a deadline).

FOR EACH TASK
- title: short imperative-mood action. Max ~80 chars.
- description: 1-2 sentences. Include who is asking and what. Keep any
  vague time references ("by Friday", "ASAP", "next week") in the
  description but DO NOT translate them into the due_date field.
- due_date: YYYY-MM-DD ONLY for explicit, unambiguous dates from the
  message (subject or body). Examples:
    - "deadline April 18" with year clear → 2026-04-18
    - "by 2026-04-30" → 2026-04-30
    - "by April" → null  (month alone is not a date)
    - "by end of April" → null
    - "next week", "EOD Tuesday" without a date → null
    - "ASAP", "soon" → null
  Better null than fabricated. False-confidence deadlines are worse than
  missing ones.
- priority: 1-5, default 3. Use 1-2 for explicit urgent / hard-deadline.
  Past-due triage (overdue/stale flagging) is applied programmatically
  AFTER your output, so do not attempt to mark stale items yourself —
  just emit the literal date.
- source_id: the source_id I provided.
- primary_entity_name: best guess at the most relevant person, project,
  or topic from the ENTITY INDEX section. Use canonical name or any alias.
  Null if no clear central entity.

Empty tasks array is correct when the message has no action.

{entity_index}

EXAMPLES

--- Example 1: explicit ask from a sender ---

INPUT:
=== source_id: 20260420-abc123 ===
subject: Re: AlphaFold 3 demo for Wednesday
from: David Ackerman <d@example.org>
to: {user_full_name} <user@example.com>
date: 2026-04-20
---
{user_first_name},

Could you bring the analyzed segmentation outputs to the Wednesday demo?
We'll project them on the screen during the walkthrough. Need them by EOD
Tuesday so we can pre-load.

— David

EXPECTED tool call: submit_tasks({{
  "tasks": [
    {{
      "title": "Bring segmentation outputs to AF3 demo Wednesday",
      "description": "David Ackerman asked for analyzed segmentation outputs for the Wednesday AlphaFold 3 demo. Needs them by EOD Tuesday for pre-loading.",
      "due_date": "2026-04-21",
      "priority": 2,
      "source_id": "20260420-abc123",
      "primary_entity_name": "David Ackerman"
    }}
  ]
}})

--- Example 2: notification with no action ---

INPUT:
=== source_id: 20260418-amazon-shipped ===
subject: Your Amazon order has shipped
from: auto-shipping@amazon.com
to: {user_full_name} <user@example.com>
date: 2026-04-18
---
Your package will arrive April 22. Track here: <link>

EXPECTED tool call: submit_tasks({{"tasks": []}})

WHY: transactional, no action requested of the user.
"""


# ---- entity index for in-prompt linking ---------------------------------

def fetch_entity_index_text(client) -> str:
    """Fetch all entities from DB and format them as a compact reference
    block to embed in the system prompt. Returns the formatted text."""
    try:
        ents = client.schema(SCHEMA).table("entities").select(
            "type,canonical_name,aliases,metadata"
        ).execute().data or []
    except Exception as e:
        logging.warning("Could not fetch entity index: %s", e)
        return "ENTITY INDEX: (unavailable — extraction will not link to entities)"

    by_type: dict[str, list[dict]] = {}
    for e in ents:
        by_type.setdefault(e.get("type", ""), []).append(e)

    lines = ["ENTITY INDEX",
             "Use entries below for `primary_entity_name`. Null if no clear match.",
             ""]
    for t in ("person", "project", "topic", "deadline"):
        bucket = by_type.get(t, [])
        if not bucket:
            continue
        lines.append(f"{t.upper()}S:")
        for e in sorted(bucket, key=lambda x: x.get("canonical_name", "")):
            aliases = e.get("aliases") or []
            aliases_str = f" (aliases: {', '.join(aliases)})" if aliases else ""
            lines.append(f"  - {e['canonical_name']}{aliases_str}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_entity_resolver(client) -> dict[str, int]:
    """Map every canonical_name + alias → entity_id for post-extraction lookup.
    Lower-cased for matching robustness."""
    resolver: dict[str, int] = {}
    try:
        ents = client.schema(SCHEMA).table("entities").select(
            "id,canonical_name,aliases"
        ).execute().data or []
    except Exception as e:
        logging.warning("Could not build entity resolver: %s", e)
        return resolver
    for e in ents:
        resolver[e["canonical_name"].lower()] = int(e["id"])
        for alias in e.get("aliases") or []:
            resolver.setdefault(alias.lower(), int(e["id"]))
    return resolver


def resolve_entity(name: Optional[str], resolver: dict[str, int]) -> Optional[int]:
    if not name:
        return None
    return resolver.get(name.strip().lower())


# ---- tool schema --------------------------------------------------------

EXTRACT_TOOL: dict[str, Any] = {
    "name": "submit_tasks",
    "description": "Submit extracted tasks. Reference each task to its source_id.",
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
                        "due_date": {"type": ["string", "null"]},
                        "priority": {"type": "integer", "minimum": 1, "maximum": 5},
                        "source_id": {"type": "string"},
                        "primary_entity_name": {"type": ["string", "null"]},
                    },
                    "required": ["title", "description", "priority", "source_id"],
                },
            }
        },
        "required": ["tasks"],
    },
}


# ---- batch formatting ---------------------------------------------------

def build_user_message(items: list[tuple[str, dict]], source: str) -> str:
    """Same shape as agent_index — keeps prompt consistent across modules."""
    parts = []
    for source_id, item in items:
        if source in ("mail-work", "mail-personal"):
            body = item.get("body", "")
            if len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "\n\n[truncated]"
            parts.append(
                f"=== source_id: {source_id} ===\n"
                f"subject: {item.get('subject', '')}\n"
                f"from: {item.get('from', '')}\n"
                f"to: {item.get('to', '')}\n"
                f"cc: {item.get('cc', '')}\n"
                f"date: {item.get('date', '')}\n"
                f"---\n"
                f"{body}\n"
            )
        else:
            body = strip_data_uris(item.get("body", ""))
            if len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "\n\n[truncated]"
            parts.append(
                f"=== source_id: {source_id} ===\n"
                f"title: {item.get('title', '')}\n"
                f"folder: {item.get('folder', '')}\n"
                f"modified: {item.get('modified', '')}\n"
                f"---\n"
                f"{body}\n"
            )
    return "\n\n".join(parts)


def apply_past_due_rules(tasks: list[dict], today: date) -> tuple[list[dict], dict]:
    """Three-bucket past-due triage. Deterministic post-processor.

    For each task with non-null parseable due_date, compute days_past =
    (today - due_date).days, then:
      - days_past <= 0: future or today, no change
      - 0 < days_past <= 30:  prepend "[OVERDUE] ", set priority 1
      - 30 < days_past <= 180: prepend "[STALE] ", set priority 4
      - days_past > 180: drop the task entirely (likely done or irrelevant)

    Tasks with null or unparseable due_date pass through unchanged.

    Returns (filtered_tasks, stats_dict).
    """
    out: list[dict] = []
    stats = {"overdue": 0, "stale": 0, "dropped": 0, "future_or_today": 0, "no_date": 0}
    for t in tasks:
        due = t.get("due_date")
        if not due:
            stats["no_date"] += 1
            out.append(t)
            continue
        try:
            due_date = date.fromisoformat(due)
        except (ValueError, TypeError):
            # Unparseable date string — leave as-is, treat like no date
            stats["no_date"] += 1
            out.append(t)
            continue
        days_past = (today - due_date).days
        if days_past <= 0:
            stats["future_or_today"] += 1
            out.append(t)
        elif days_past <= 30:
            t["title"] = "[OVERDUE] " + (t.get("title") or "")
            t["priority"] = 1
            stats["overdue"] += 1
            out.append(t)
        elif days_past <= 180:
            t["title"] = "[STALE] " + (t.get("title") or "")
            t["priority"] = 4
            stats["stale"] += 1
            out.append(t)
        else:
            stats["dropped"] += 1
            # don't append — task is too old to surface
    return out, stats


def extract_batch(
    anthropic_client,
    cost_tracker: CostTracker,
    items: list[tuple[str, dict]],
    system_prompt: str,
    source: str,
    today: date,
) -> tuple[list[dict], dict]:
    cost_tracker.assert_within_budget()
    user_message = build_user_message(items, source)

    def _call():
        return anthropic_client.messages.create(
            model=EXTRACT_MODEL,
            max_tokens=8192,
            metadata={"user_id": USER_ID},
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "submit_tasks"},
            system=cached_system(system_prompt),
            messages=[{"role": "user", "content": user_message}],
        )

    response = call_claude_with_retry(_call)
    cost_this = cost_tracker.record(EXTRACT_MODEL, response.usage)

    raw_tasks: list[dict] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_tasks":
            raw_tasks = block.input.get("tasks", []) or []
            break

    # Apply past-due triage deterministically (model emits literal dates;
    # we transform).
    tasks, triage = apply_past_due_rules(raw_tasks, today)

    logging.info(
        "  → %d tasks (raw=%d overdue=%d stale=%d dropped_old=%d) "
        "cost=$%.4f in=%d cache_r=%d cache_w=%d out=%d",
        len(tasks), len(raw_tasks),
        triage["overdue"], triage["stale"], triage["dropped"],
        cost_this,
        getattr(response.usage, "input_tokens", 0) or 0,
        getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        getattr(response.usage, "output_tokens", 0) or 0,
    )
    return tasks, triage


# ---- DB writes ----------------------------------------------------------

def insert_task(client, task: dict) -> str:
    """Returns one of: new | skipped_existing | error."""
    try:
        client.schema(SCHEMA).table("tasks").insert(task).execute()
        return "new"
    except Exception as e:
        err = str(e).lower()
        if "duplicate" in err or "unique" in err or "23505" in err:
            return "skipped_existing"
        logging.error("Insert failed for task %r: %s", task.get("title"), e)
        return "error"


# ---- main ---------------------------------------------------------------

def parse_since(s: Optional[str]) -> datetime:
    if s:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(days=90)


def select_items(source: str, since_dt: datetime,
                 exclude_folders: set[str]) -> tuple[list[Path], list[tuple[str, dict]]]:
    if source == "notes":
        items_paths = list_notes_filtered(since_dt, exclude_folders)
        items = [(p.stem, load_note(p)) for p in items_paths]
        return items_paths, items
    if source == "mail-work":
        items_paths = list_mail_eml_since(MAIL_WORK_DIR, since_dt)
        items = [(p.stem, load_mail(p)) for p in items_paths]
        return items_paths, items
    if source == "mail-personal":
        items_paths = list_mail_eml_since(MAIL_PERSONAL_DIR, since_dt)
        items = [(p.stem, load_mail(p)) for p in items_paths]
        return items_paths, items
    raise ValueError(f"unsupported source: {source}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="notes",
                        choices=["notes", "mail-work", "mail-personal"])
    parser.add_argument("--since", default=None,
                        help="ISO date (YYYY-MM-DD); default: 90 days ago")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--max-cost", type=float, default=5.0)
    parser.add_argument("--exclude-folder", action="append", default=[],
                        metavar="FOLDER",
                        help="Skip notes from this Apple Notes folder (notes only).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print extracted tasks; don't write to DB")
    args = parser.parse_args()

    setup_logging("agent_extract")
    started = datetime.now(timezone.utc)
    since_dt = parse_since(args.since)
    exclude_folders = set(args.exclude_folder or [])

    cfg = load_agent_config()
    logging.info(
        "Starting agent_extract source=%s since=%s limit=%s full=%s "
        "max_cost=$%.2f dry_run=%s",
        args.source, since_dt.date().isoformat(),
        args.limit, args.full, args.max_cost, args.dry_run,
    )

    # Source items
    items_paths, items = select_items(args.source, since_dt, exclude_folders)
    if not args.full and args.limit is not None:
        items_paths = items_paths[: args.limit]
        items = items[: args.limit]
    logging.info("Selected %d %s items", len(items_paths), args.source)
    if not items:
        logging.info("Nothing to do")
        return 0

    sid_to_path: dict[str, Path] = {sid: p for (sid, _), p in zip(items, items_paths)}

    # Clients (read-only is fine even for dry-run; needed for entity index)
    anthropic_client = get_anthropic_client()
    supabase = get_supabase_client()
    cost_tracker = CostTracker(max_cost=args.max_cost)

    # Build entity index (cached prefix) and resolver (post-extraction lookup)
    entity_index_text = fetch_entity_index_text(supabase)
    entity_resolver = build_entity_resolver(supabase)
    logging.info("Entity index: %d resolvable names", len(entity_resolver))

    today = date.today()
    # Choose per-source prompt template (no `today` placeholder — past-due
    # triage is now done in Python post-processing on the raw model output).
    if args.source == "notes":
        prompt_template = NOTES_SYSTEM_TEMPLATE
        system_prompt = prompt_template.format(
            user_full_name=cfg["user_full_name"],
            user_first_name=cfg.get("user_first_name", cfg["user_full_name"]),
            entity_index=entity_index_text,
        )
    else:  # mail-work or mail-personal
        prompt_template = MAIL_SYSTEM_TEMPLATE
        system_prompt = prompt_template.format(
            user_full_name=cfg["user_full_name"],
            user_first_name=cfg.get("user_first_name", cfg["user_full_name"]),
            source_kind=args.source,
            entity_index=entity_index_text,
        )

    # Batch loop
    all_tasks: list[dict] = []
    triage_totals = {"overdue": 0, "stale": 0, "dropped": 0,
                     "future_or_today": 0, "no_date": 0}
    aborted_for_cost = False
    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i : i + BATCH_SIZE]
        logging.info("Batch %d: %d items", i // BATCH_SIZE + 1, len(batch))
        try:
            tasks, triage = extract_batch(
                anthropic_client, cost_tracker, batch, system_prompt,
                args.source, today,
            )
        except CostBudgetExceeded as e:
            logging.warning("Stopping early: %s", e)
            aborted_for_cost = True
            break
        for k in triage_totals:
            triage_totals[k] += triage.get(k, 0)

        # Augment each task with source metadata + resolved entity_id
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
            entity_id = resolve_entity(t.get("primary_entity_name"), entity_resolver)
            all_tasks.append({
                "source_type": args.source,
                "source_path": rel_path,
                "source_id": sid,
                "title": (t.get("title") or "")[:500],
                "description": t.get("description") or "",
                "due_date": t.get("due_date") or None,
                "priority": int(t.get("priority", 3)),
                "primary_entity_id": entity_id,
                # primary_entity_name kept on the in-memory dict for dry-run
                # display; not a column. Stripped before insert.
                "_primary_entity_name": t.get("primary_entity_name"),
            })

    # Output
    new_count = 0
    skipped_count = 0
    error_count = 0
    if args.dry_run:
        print()
        print("=" * 72)
        print(f"DRY RUN — {len(all_tasks)} tasks extracted (no DB writes)")
        print("=" * 72)
        for t in all_tasks:
            display = dict(t)
            print(json.dumps(display, indent=2, default=str))
            print()
    else:
        for t in all_tasks:
            insertable = {k: v for k, v in t.items() if not k.startswith("_")}
            status = insert_task(supabase, insertable)
            if status == "new":
                new_count += 1
            elif status == "skipped_existing":
                skipped_count += 1
            else:
                error_count += 1
        logging.info("DB writes: %d new, %d skipped (dup), %d errors",
                     new_count, skipped_count, error_count)

    finished = datetime.now(timezone.utc)
    summary = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "source": args.source,
        "since": since_dt.date().isoformat(),
        "limit": args.limit,
        "full": args.full,
        "dry_run": args.dry_run,
        "aborted_for_cost": aborted_for_cost,
        "items_processed": len(items),
        "tasks_extracted": len(all_tasks),
        "tasks_inserted_new": new_count,
        "tasks_skipped_dup": skipped_count,
        "tasks_errored": error_count,
        "past_due_triage": triage_totals,
        "cost": cost_tracker.summary(),
    }
    write_run_state("agent_extract", summary)
    logging.info(
        "Done. items=%d tasks=%d new=%d skipped=%d errors=%d cost=$%.4f",
        len(items), len(all_tasks), new_count, skipped_count, error_count,
        cost_tracker.total_cost,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
