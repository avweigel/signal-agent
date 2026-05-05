#!/usr/bin/env python3
"""
Phase 7 indexing: read dumps, extract entities (people, projects, topics,
deadlines), store them in signal_agent.entities + signal_agent.mentions.

This runs *before* task extraction. The point is to build a stable index of
named entities the user actually deals with, so downstream task extraction
can ground tasks against known people, projects, and topics rather than
re-discovering them every run.

Defaults are conservative: last 90 days, --limit 50 items per source, hard
$5 cost cap. Pass --full to lift the limit, --since to widen the date
window, --max-cost to raise the cap.

Usage:
    python3 scripts/agent_index.py                       # notes, last 90d, limit 50
    python3 scripts/agent_index.py --dry-run             # don't write to DB
    python3 scripts/agent_index.py --since 2026-01-01    # widen date range
    python3 scripts/agent_index.py --full                # no item limit
    python3 scripts/agent_index.py --max-cost 1.0        # tighter budget
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Optional

from agent_common import (
    AGENT_DATA,
    INDEX_MODEL,
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

NOTES_DIR = AGENT_DATA / "notes"
MAIL_WORK_DIR = AGENT_DATA / "mail-work"
MAIL_PERSONAL_DIR = AGENT_DATA / "mail-personal"

DATA_URI_RE = re.compile(r"!\[[^\]]*\]\(data:[^)]*\)")
MAX_BODY_CHARS = 8000
BATCH_SIZE = 5

QUOTED_REPLY_MARKERS = [
    re.compile(r"^On .+wrote:\s*$", re.IGNORECASE),
    re.compile(r"^From:\s+", re.IGNORECASE),
    re.compile(r"^-+\s*Original Message\s*-+", re.IGNORECASE),
    re.compile(r"^-+\s*Forwarded message\s*-+", re.IGNORECASE),
    re.compile(r"^Sent from my", re.IGNORECASE),
    re.compile(r"^_{2,}\s*$"),
]

INDEX_SYSTEM_PROMPT_TEMPLATE = """\
You are building an index of recurring entities mentioned in a user's
personal data (notes, emails, calendar events). The user is a busy
professional. Goal: identify named entities that recur in their work and
life, so we can later ground task extraction against this index.

THE USER IS NOT AN ENTITY TO INDEX
The user is {user_full_name} (also known as {user_first_name}). Skip all
self-references including:
- Their own social media captions and public-facing writing (Instagram
  posts, fitness/lifestyle content, journal entries)
- Notes the user wrote in a first-person voice about themselves
- Login/credential entries that just contain the user's own accounts
If a note is unambiguously the user's own first-person voice, extract OTHER
people and topics mentioned in it but NOT the user.

Entity types to extract:
- person: People mentioned by name (collaborators, family, contacts).
  Use the most complete name available ("Marwan Zouinkhi", not just "Marwan",
  if both forms appear).
- project: Named projects, products, papers, grants, datasets the user is
  actively working on. Things with a proper name and ongoing engagement.
- topic: Recurring themes / lifecycles. E.g., "Sweden move logistics",
  "tenure case prep", "kids' schooling transition", "ADP onboarding".
- deadline: Specific calendar dates with associated obligations. Format
  metadata.date as YYYY-MM-DD.

HIGH BAR FOR INCLUSION
Extract an entity ONLY if it has meaningful recurring context. Specifically:
  (a) it appears in 2 or more notes within this batch, OR
  (b) it appears in a single note that provides substantial context — a
      full meeting note, project description, or ongoing thread (NOT a
      short capture, password entry, or one-line reference under ~200
      characters of meaningful prose).
If neither condition holds, skip the entity entirely. Better to miss
something than to fill the index with noise.

CONSOLIDATE AGGRESSIVELY
If a person and a topic about that same person both appear (e.g., "Oliver"
the person and "Oliver medical care" the topic), create ONE entity (the
person) and roll the topic into metadata.context. Do NOT create both.
Same for a project and discussion threads about that project — prefer the
project entity.

For EACH entity you do extract:
- entity_type: one of person | project | topic | deadline
- canonical_name: the most complete form of the name
- aliases: array of OTHER strings referring to the same entity (omit if none)
- metadata: free-form object. Useful keys: role, organization, context,
  date, needs_disambiguation (true if the name is ambiguous like a bare
  "John")
- mentions: array of {{source_id, context_snippet}}. Snippet is ~30-50 words
  of context that grounds why this entity was extracted.

DO NOT extract:
- The user themselves ({user_full_name})
- One-off incidental names with no engagement
- Generic tools / software (Python, Excel, Google Drive) unless they are
  themselves the subject of recurring work
- Pure reference content (recipes, articles, copied text)
- Login or password vault entries unless the entity has substantive
  evidence elsewhere in the batch

Mail threads often contain quoted replies and forwarded content. The
preprocessor strips quoted history before sending to you, but some
fragments may slip through. Extract only entities relevant to the ORIGINAL
message context, not chains of unrelated CCs or recipients in quoted forwards.

Submit via the submit_entities tool. If the input has no entities meeting
the bar, return an empty array — that is the correct response when nothing
qualifies.

EXAMPLES OF EXPECTED EXTRACTION

The following examples show how the rules apply in practice. Pay close
attention to what is extracted vs. skipped, and to the consolidation /
disambiguation patterns.

--- Example 1: work email with multiple recipients and a quoted reply ---

INPUT:
=== source_id: 20260415-cellmap-q3-meeting ===
subject: Re: CellMap Steering Committee Q3 review
from: David Ackerman <ackermand@example.org>
to: {user_full_name} <user@example.com>, Wyatt Korff <korffw@example.org>
cc: assistants@example.org, intern@example.org
date: 2026-04-15
---
Hi all,

Quick update on steering committee logistics. Wyatt — can you confirm the
Caltech room booking? Marwan needs the latest segmentation results before
the meeting; I'll pull them from the cluster tonight.

Also flagging the ramp-down milestone — we should plan an offsite to align
on the segmentation challenge closeout. Stephan can chair the
data-standards subgroup if needed.

Best,
David

EXPECTED tool call:
submit_entities({{
  "entities": [
    {{
      "entity_type": "person",
      "canonical_name": "David Ackerman",
      "metadata": {{"role": "collaborator", "context": "Steering committee coordinator"}},
      "mentions": [{{"source_id": "20260415-cellmap-q3-meeting", "context_snippet": "Sender; coordinating CellMap Steering Committee Q3 review and ramp-down planning."}}]
    }},
    {{
      "entity_type": "person",
      "canonical_name": "Wyatt Korff",
      "aliases": ["Wyatt"],
      "mentions": [{{"source_id": "20260415-cellmap-q3-meeting", "context_snippet": "Asked to confirm Caltech room booking for Q3 review."}}]
    }},
    {{
      "entity_type": "person",
      "canonical_name": "Marwan",
      "metadata": {{"needs_disambiguation": true, "context": "Awaiting segmentation results before meeting"}},
      "mentions": [{{"source_id": "20260415-cellmap-q3-meeting", "context_snippet": "Marwan needs latest segmentation results before the meeting."}}]
    }},
    {{
      "entity_type": "person",
      "canonical_name": "Stephan",
      "metadata": {{"needs_disambiguation": true, "context": "Possible chair for data-standards subgroup"}},
      "mentions": [{{"source_id": "20260415-cellmap-q3-meeting", "context_snippet": "Stephan can chair data-standards subgroup if needed."}}]
    }},
    {{
      "entity_type": "project",
      "canonical_name": "CellMap Steering Committee",
      "aliases": ["CellMap Steering Committee Q3 review"],
      "metadata": {{"cadence": "recurring", "current_phase": "ramp-down planning"}},
      "mentions": [{{"source_id": "20260415-cellmap-q3-meeting", "context_snippet": "Q3 review with ramp-down milestone discussion and segmentation challenge closeout."}}]
    }}
  ]
}})

Why:
- {user_full_name} (in `to:` line): SKIPPED — user is not an entity.
- assistants@, intern@ (CC'd, no engagement in body): SKIPPED — incidental.
- David, Wyatt, Marwan, Stephan: each engaged in body → extracted.
- "Marwan" and "Stephan" are bare first names → needs_disambiguation = true.
- "Wyatt Korff" canonical with "Wyatt" alias (consolidated short → long).
- The project is the recurring committee, not just the Q3 review event.
- Sign-off "Best, David" doesn't add a new entity.

--- Example 2: meeting note with attendees and action items ---

INPUT:
=== source_id: 2026-03-12-jls-lab-meeting ===
title: 2026-03-12 JLS Lab meeting
folder: JLS Lab
modified: 2026-03-12T16:00:00Z
---
# Attendees
Jennifer Lippincott-Schwartz, Sarah Cohen, {user_full_name}, J Lippincott-Schwartz

# Agenda
- ER membrane dynamics in Sarah's PostDoc thread
- Collaboration timeline with KTH
- Sarah's manuscript: target Jan 2027 submission

# Action items
- {user_first_name}: send raw FIB-SEM data to Sarah
- Sarah: send updated manuscript draft by 2026-04-15
- Jennifer: review COSEM grant renewal section

EXPECTED tool call:
submit_entities({{
  "entities": [
    {{
      "entity_type": "person",
      "canonical_name": "Jennifer Lippincott-Schwartz",
      "aliases": ["J Lippincott-Schwartz", "Jennifer"],
      "metadata": {{"role": "PI"}},
      "mentions": [{{"source_id": "2026-03-12-jls-lab-meeting", "context_snippet": "Lab meeting attendee; reviewing COSEM grant renewal section."}}]
    }},
    {{
      "entity_type": "person",
      "canonical_name": "Sarah Cohen",
      "aliases": ["Sarah"],
      "metadata": {{"role": "PostDoc"}},
      "mentions": [{{"source_id": "2026-03-12-jls-lab-meeting", "context_snippet": "PostDoc working on ER membrane dynamics; manuscript draft due 2026-04-15."}}]
    }},
    {{
      "entity_type": "project",
      "canonical_name": "Sarah Cohen ER membrane dynamics manuscript",
      "metadata": {{"target_submission": "2027-01"}},
      "mentions": [{{"source_id": "2026-03-12-jls-lab-meeting", "context_snippet": "Sarah's PostDoc work on ER membrane dynamics; target Jan 2027 submission."}}]
    }},
    {{
      "entity_type": "project",
      "canonical_name": "COSEM grant renewal",
      "mentions": [{{"source_id": "2026-03-12-jls-lab-meeting", "context_snippet": "Jennifer reviewing the renewal section."}}]
    }},
    {{
      "entity_type": "deadline",
      "canonical_name": "Sarah Cohen manuscript draft",
      "metadata": {{"date": "2026-04-15"}},
      "mentions": [{{"source_id": "2026-03-12-jls-lab-meeting", "context_snippet": "Sarah sends updated manuscript draft by 2026-04-15."}}]
    }}
  ]
}})

Why:
- {user_full_name} appears in attendees AND as the actor in an action item → SKIPPED both times.
- "J Lippincott-Schwartz" merged into "Jennifer Lippincott-Schwartz" as alias.
- "Sarah" merged into "Sarah Cohen" with alias.
- Two project entities (manuscript, grant renewal) — both have substantive context.
- One deadline entity for the manuscript draft date.
- Note that the project entity for the manuscript rolls in the personal attribution
  ("Sarah Cohen ER membrane dynamics manuscript") so we don't create a separate
  topic just to record "Sarah is working on a manuscript".

--- Example 3: low-signal vs. substantive calendar events ---

LOW-SIGNAL INPUT (recurring event with no prep, just attendees):
=== source_id: 2026-04-22-cellmap-steering ===
title: CellMap Steering Committee
attendees: Wyatt Korff, Stephan Saalfeld, Kayvon Pedram, {user_full_name}
description: Recurring steering committee meeting.

EXPECTED tool call: submit_entities({{"entities": []}})  -- empty

Why: this event is a stub recurrence with attendees who appear elsewhere with
substantive context. Don't extract them here — we'd just create noisy mentions.
The project (CellMap Steering Committee) is also covered by other examples, so
no need to create a duplicate from this source.

SUBSTANTIVE INPUT (one-off event with real engagement context):
=== source_id: 2026-05-03-sweden-relocation-quote ===
title: Sweden relocation: receive quote from Movinga
attendees: {user_full_name}
description:
Movinga sales rep Hans Erikson sending Q2 estimate by EOD. Comparing against
Pickfords and Crown Relocations. Need to lock vendor by 2026-05-15.

EXPECTED tool call: submit_entities({{"entities": [
  {{
    "entity_type": "person",
    "canonical_name": "Hans Erikson",
    "metadata": {{"role": "Movinga sales rep"}},
    "mentions": [{{"source_id": "2026-05-03-sweden-relocation-quote", "context_snippet": "Movinga sales rep providing Q2 relocation estimate."}}]
  }},
  {{
    "entity_type": "topic",
    "canonical_name": "Sweden relocation vendor selection",
    "metadata": {{"context": "Comparing Movinga, Pickfords, Crown Relocations"}},
    "mentions": [{{"source_id": "2026-05-03-sweden-relocation-quote", "context_snippet": "Comparing relocation vendors; need to lock by 2026-05-15."}}]
  }},
  {{
    "entity_type": "deadline",
    "canonical_name": "Sweden relocation vendor lock-in",
    "metadata": {{"date": "2026-05-15"}},
    "mentions": [{{"source_id": "2026-05-03-sweden-relocation-quote", "context_snippet": "Need to lock relocation vendor by 2026-05-15."}}]
  }}
]}})

Why: this calendar event has substantive context — a named external contact,
multiple vendors being compared, an explicit deadline. Even as a one-off
event, it crosses the (b) "substantial context block" bar for inclusion.

--- end of examples ---

ADDITIONAL EDGE CASES AND ANTI-PATTERNS

These cover real situations that have caused incorrect extraction in the past.
Each is a do/don't pair.

Edge case A: a calendar event with attendees and a substantive description.
INPUT title: "AlphaFold 3 hands-on session"
attendees: J Lippincott-Schwartz, Sarah Cohen, {user_full_name}, Marwan Zouinkhi
description: Walk through running AF3 on the new GPU cluster. Marwan will
demo job submission. Bring your own protein sequence to test.
EXPECTED: extract project "AlphaFold 3 onboarding" (one-off learning event
with substantive context), extract person "Marwan Zouinkhi" (specific role
in the event — demoing job submission). Don't extract the other attendees
without engagement context — they're seen elsewhere.

Edge case B: a forwarded email with a long quoted history.
The preprocessor strips quoted content, but if a fragment slips through
(e.g., "On 2026-03-04, X wrote:" survived), do NOT extract entities from
the quoted portion — those are stale context, not the current message.
ALSO: do not extract the quoter as a new person. Stick to the entities
relevant to the most recent (top) message.

Edge case C: a note that's just a reference list.
A note titled "Common Python imports" containing import statements is
reference material with NO entities to extract. Return an empty entities
array. Same for: cheatsheets, recipes, copied article excerpts, song
lyrics, code snippets without commentary.

Edge case D: an email signature line.
Signatures often contain a person's title, organization, and contact info.
The sender themselves is the person entity (already in the from: header).
Don't double-extract the signature as a separate "person" — and don't
extract the company / department name from the signature as a project
unless the body engages with it substantively.

Edge case E: a name appearing only in a Cc field with no engagement.
INPUT to: collaborator@example.com  cc: alice@example.com, bob@example.com
body: "Hi all, attaching the report. — Carol"
EXPECTED: extract Carol (sender) only. Do NOT extract Alice or Bob —
they're CC'd but not engaged. The body must reference them substantively.

Edge case F: an introduction note.
INPUT: "I'm Aubrey, a research scientist at Janelia working on cellular
imaging." (a profile bio note, in the user's own voice)
EXPECTED: empty entities array. The note is the user describing
themselves — skip per the user-is-not-an-entity rule. Don't extract
"Janelia" as a project either — it's the user's organization, not a
recurring named project the user is engaging with.

Edge case G: a deadline expressed relatively.
INPUT body: "Need to send the draft by next Monday."
EXPECTED: do NOT fabricate an absolute date. The model has no calendar
context to resolve "next Monday" reliably. Extract a deadline entity if
the relative date appears in metadata.context, but don't put a fabricated
metadata.date. Better to leave date null than to guess wrong.

Edge case H: the same person mentioned with role variants.
INPUT: "Prof. Chen reviewed the manuscript. Dr. Chen will submit the
revised version next month."
EXPECTED: ONE entity "Chen" (or full name if known elsewhere) with
metadata.needs_disambiguation = true (multiple Chens may exist), and
aliases = ["Prof. Chen", "Dr. Chen"] if these are honorifics for the same
person. If unsure they're the same, emit two entities flagged for
disambiguation rather than guessing.

ANTI-PATTERN summary (things to NEVER do):
- Never extract {user_full_name} or {user_first_name} as an entity
- Never extract a person who only appears in a quoted/forwarded fragment
- Never extract from a Cc list without substantive body engagement
- Never extract a public figure / celebrity not engaged with the user's work
- Never fabricate a date for a deadline; null is fine
- Never extract software tools, websites, or generic services as projects
  (e.g., "Slack", "Notion", "GitHub" — only if the project itself is
  about that tool)
- Never split a person and a topic about that person into two entities;
  consolidate via metadata.context
- Never merge across entity_type boundaries (a person and a project with
  the same name remain two entities)

OPERATIONAL REMINDERS

Always submit through the submit_entities tool. The tool's input schema
is the contract — emit `aliases: []` when there are no aliases (or omit
the field entirely; it'll default). Same for `metadata: {{}}`.

The `mentions` array is the most important field for downstream usage —
each mention's context_snippet should be ~30-50 words that grounds why
the entity was extracted. Don't make the snippet a generic restatement
of the title; quote or paraphrase actual content from the body that
references the entity.

Empty arrays are valid responses. If a batch of inputs has no entities
meeting the bar, return submit_entities({{"entities": []}}). That's the
correct behavior — better than fabricating low-confidence entities to
appear thorough.
"""

INDEX_TOOL: dict[str, Any] = {
    "name": "submit_entities",
    "description": "Submit indexed entities and their mentions in this batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "entity_type": {
                            "type": "string",
                            "enum": ["person", "project", "topic", "deadline"],
                        },
                        "canonical_name": {"type": "string"},
                        "aliases": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "metadata": {"type": "object"},
                        "mentions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "source_id": {"type": "string"},
                                    "context_snippet": {"type": "string"},
                                },
                                "required": ["source_id", "context_snippet"],
                            },
                        },
                    },
                    "required": ["entity_type", "canonical_name", "mentions"],
                },
            }
        },
        "required": ["entities"],
    },
}


# ---- source listing -----------------------------------------------------

def strip_data_uris(text: str) -> str:
    return DATA_URI_RE.sub("[image]", text)


def load_note(path: Path) -> dict:
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


def peek_frontmatter(path: Path) -> dict:
    """Read just the YAML frontmatter; return folder + parsed modified datetime."""
    info: dict = {"folder": "", "modified": None}
    try:
        with path.open("r", encoding="utf-8") as f:
            first = f.readline()
            if first.rstrip() != "---":
                return info
            for _ in range(20):  # frontmatter is short
                line = f.readline()
                if not line or line.rstrip() == "---":
                    break
                if line.startswith("folder:"):
                    info["folder"] = line.split(":", 1)[1].strip().strip('"')
                elif line.startswith("modified:"):
                    raw = line.split(":", 1)[1].strip().strip('"')
                    try:
                        info["modified"] = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    except ValueError:
                        pass
    except OSError:
        pass
    return info


def strip_quoted_replies(text: str) -> str:
    """Cut at the first quoted-reply / forwarded marker."""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        if any(p.match(stripped) for p in QUOTED_REPLY_MARKERS):
            break
        if stripped.startswith(">"):
            break
        out.append(line)
    return "\n".join(out).strip()


def extract_eml_body(eml_path: Path) -> str:
    """Get first text/plain part (or text/html fallback) with quoted history stripped."""
    try:
        raw = eml_path.read_bytes()
    except OSError:
        return ""
    msg = BytesParser().parsebytes(raw)
    plain = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    plain = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    elif msg.get_content_type() == "text/plain":
        payload = msg.get_payload(decode=True)
        if payload:
            plain = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    if not plain:
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    html_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    html_text = re.sub(r"<script.*?</script>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
                    html_text = re.sub(r"<style.*?</style>", "", html_text, flags=re.DOTALL | re.IGNORECASE)
                    plain = re.sub(r"<[^>]+>", " ", html_text)
                    plain = re.sub(r"\s+", " ", plain).strip()
                    break
    return strip_quoted_replies(plain)


def load_mail(eml_path: Path) -> dict:
    """Read .eml + sidecar .json. Body is stripped of quoted history."""
    json_path = eml_path.with_suffix(".json")
    sidecar = {}
    if json_path.exists():
        try:
            sidecar = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "subject": sidecar.get("subject") or "",
        "from": sidecar.get("from") or "",
        "to": sidecar.get("to") or "",
        "cc": sidecar.get("cc") or "",
        "date": sidecar.get("date") or "",
        "body": extract_eml_body(eml_path),
    }


def list_mail_eml_since(mail_dir: Path, since_dt: datetime) -> list[Path]:
    """List .eml files in year/month subdirs >= since.year-month, newest first."""
    cutoff_year = since_dt.year
    cutoff_month = since_dt.month
    if not mail_dir.is_dir():
        return []
    candidates: list[tuple[int, int, Path]] = []
    for year_dir in mail_dir.iterdir():
        if not year_dir.is_dir():
            continue
        try:
            year = int(year_dir.name)
        except ValueError:
            continue
        if year < cutoff_year:
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            try:
                month = int(month_dir.name)
            except ValueError:
                continue
            if year == cutoff_year and month < cutoff_month:
                continue
            for eml in month_dir.glob("*.eml"):
                candidates.append((year, month, eml))
    candidates.sort(key=lambda t: (t[0], t[1], t[2].name), reverse=True)
    return [p for _, _, p in candidates]


def list_notes_filtered(
    since_dt: datetime,
    exclude_folders: set[str],
) -> list[Path]:
    """Notes filtered by frontmatter modified >= since and folder not in
    exclude_folders. Sorted newest first by frontmatter modified."""
    candidates: list[tuple[datetime, Path]] = []
    skipped_excluded = 0
    skipped_no_date = 0
    skipped_too_old = 0
    for p in NOTES_DIR.glob("*.md"):
        info = peek_frontmatter(p)
        if info["folder"] in exclude_folders:
            skipped_excluded += 1
            continue
        modified = info.get("modified")
        if modified is None:
            skipped_no_date += 1
            continue
        if modified < since_dt:
            skipped_too_old += 1
            continue
        candidates.append((modified, p))
    candidates.sort(reverse=True)
    logging.info(
        "Walker: %d candidates (excluded %d folders, %d undated, %d too old)",
        len(candidates), skipped_excluded, skipped_no_date, skipped_too_old,
    )
    return [p for _, p in candidates]


# ---- prompt building ----------------------------------------------------

def build_user_message(items: list[tuple[str, dict]], source: str) -> str:
    """Format items for the user message. Layout depends on source kind."""
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
        else:  # notes (and any future source that follows the notes layout)
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


def index_batch(
    anthropic_client,
    cost_tracker: CostTracker,
    items: list[tuple[str, dict]],
    system_prompt: str,
    source: str,
) -> list[dict]:
    """Call Claude on a batch. Returns the entities list."""
    cost_tracker.assert_within_budget()
    user_message = build_user_message(items, source)

    def _call():
        return anthropic_client.messages.create(
            model=INDEX_MODEL,
            max_tokens=8192,
            metadata={"user_id": USER_ID},
            tools=[INDEX_TOOL],
            tool_choice={"type": "tool", "name": "submit_entities"},
            system=cached_system(system_prompt),
            messages=[{"role": "user", "content": user_message}],
        )

    response = call_claude_with_retry(_call)
    cost_this = cost_tracker.record(INDEX_MODEL, response.usage)

    entities: list[dict] = []
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "submit_entities":
            entities = block.input.get("entities", []) or []
            break
    else:
        logging.warning("Model didn't call submit_entities; got: %s",
                        [getattr(b, "type", None) for b in response.content])

    logging.info(
        "  -> %d entities (cost $%.4f, in=%d cache_r=%d cache_w=%d out=%d)",
        len(entities), cost_this,
        getattr(response.usage, "input_tokens", 0) or 0,
        getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        getattr(response.usage, "output_tokens", 0) or 0,
    )
    return entities


# ---- dedup --------------------------------------------------------------

def _tokenize_name(name: str) -> list[str]:
    """Lowercase tokenize on whitespace, stripping leading/trailing dots and commas."""
    return [t.strip(".,").lower() for t in name.split() if t.strip(".,")]


def _merge_payload(target: dict, source: dict) -> None:
    """Merge source's aliases / mentions / metadata into target in place.
    Target keys win on metadata key conflicts."""
    aliases = set(target.get("aliases") or [])
    aliases.update(source.get("aliases") or [])
    target["aliases"] = sorted(a for a in aliases if a and a != target.get("canonical_name"))
    target["mentions"] = (target.get("mentions") or []) + (source.get("mentions") or [])
    tgt_meta = target.setdefault("metadata", {})
    for k, v in (source.get("metadata") or {}).items():
        if k not in tgt_meta:
            tgt_meta[k] = v


def dedup_entities(
    entities: list[dict],
    existing_by_type: Optional[dict[str, set[str]]] = None,
) -> tuple[list[dict], dict]:
    """Two-stage dedup, optionally cross-source-aware:
      1. Exact-match collapse on (entity_type, canonical_name) within batch.
      2. Short-form fuzzy merge. A shorter name X is merged into a longer
         name Y iff X's tokens are a subset of Y's tokens AND Y is the
         *unique* longer match (within type). Y may be:
           - another new entity in the batch (in-batch consolidation), OR
           - an existing DB entity (the new entity is rewritten so its
             canonical_name matches the DB row; subsequent upsert will
             merge into the existing row).
         If multiple longer candidates exist, flag needs_disambiguation.
    Never merges across entity types. Mentions are preserved.
    Returns (deduped, stats). Stats include cross-source-link counts.
    """
    stats = {
        "input_count": len(entities),
        "exact_collapsed": 0,
        "fuzzy_merged": 0,
        "redirected_to_existing": 0,
        "ambiguous_flagged": 0,
        "output_count": 0,
    }
    existing_by_type = existing_by_type or {}

    by_type: dict[str, list[dict]] = {}
    for e in entities:
        ent_type = e.get("entity_type", "")
        if not ent_type:
            continue
        by_type.setdefault(ent_type, []).append(e)

    output: list[dict] = []

    for ent_type, ents in by_type.items():
        # Stage 1: exact-match collapse
        by_name: dict[str, dict] = {}
        for e in ents:
            name = (e.get("canonical_name") or "").strip()
            if not name:
                continue
            if name in by_name:
                _merge_payload(by_name[name], e)
                stats["exact_collapsed"] += 1
            else:
                by_name[name] = {
                    "entity_type": ent_type,
                    "canonical_name": name,
                    "aliases": list(e.get("aliases") or []),
                    "metadata": dict(e.get("metadata") or {}),
                    "mentions": list(e.get("mentions") or []),
                }

        # Stage 2: short-form fuzzy merge (within type)
        new_names = list(by_name.keys())
        db_names = existing_by_type.get(ent_type, set())
        token_cache = {n: _tokenize_name(n) for n in set(new_names) | db_names}

        merge_into_new: dict[str, str] = {}
        redirect_to_db: dict[str, str] = {}
        ambiguous: set[str] = set()

        for short_name in new_names:
            short_toks = token_cache.get(short_name) or []
            if not short_toks:
                continue
            new_cands: list[str] = []
            db_cands: list[str] = []
            # Candidates from other new names in this batch
            for long_name in new_names:
                if long_name == short_name:
                    continue
                long_toks = token_cache.get(long_name) or []
                if len(long_toks) <= len(short_toks):
                    continue
                if all(t in long_toks for t in short_toks):
                    new_cands.append(long_name)
            # Candidates from existing DB canonicals
            for long_name in db_names:
                if long_name == short_name:
                    continue  # exact match handled by upsert
                long_toks = token_cache.get(long_name) or []
                if len(long_toks) <= len(short_toks):
                    continue
                if all(t in long_toks for t in short_toks):
                    db_cands.append(long_name)
            total_cands = len(new_cands) + len(db_cands)
            if total_cands == 1:
                if new_cands:
                    merge_into_new[short_name] = new_cands[0]
                else:
                    redirect_to_db[short_name] = db_cands[0]
            elif total_cands > 1:
                ambiguous.add(short_name)

        # Apply in-batch fuzzy merges
        for short_name, long_name in merge_into_new.items():
            if short_name not in by_name or long_name not in by_name:
                continue
            target = by_name[long_name]
            source = by_name[short_name]
            target_aliases = set(target.get("aliases") or [])
            target_aliases.add(short_name)
            target_aliases.update(source.get("aliases") or [])
            target["aliases"] = sorted(
                a for a in target_aliases if a and a != target["canonical_name"]
            )
            target["mentions"] = (target.get("mentions") or []) + (source.get("mentions") or [])
            tgt_meta = target.setdefault("metadata", {})
            for k, v in (source.get("metadata") or {}).items():
                if k not in tgt_meta:
                    tgt_meta[k] = v
            del by_name[short_name]
            stats["fuzzy_merged"] += 1

        # Apply DB redirects: rewrite canonical_name to match existing DB row.
        # The subsequent upsert_entity will find this existing row by
        # (type, canonical_name) and merge aliases/metadata/mentions there.
        for short_name, db_long in redirect_to_db.items():
            if short_name not in by_name:
                continue
            ent = by_name[short_name]
            ent_aliases = set(ent.get("aliases") or [])
            ent_aliases.add(short_name)
            ent["aliases"] = sorted(a for a in ent_aliases if a)
            ent["canonical_name"] = db_long
            stats["redirected_to_existing"] += 1

        for name in ambiguous:
            if name in by_name:
                by_name[name].setdefault("metadata", {})["needs_disambiguation"] = True
                stats["ambiguous_flagged"] += 1

        output.extend(by_name.values())

    stats["output_count"] = len(output)
    return output, stats


def fetch_existing_canonicals(client) -> dict[str, set[str]]:
    """Fetch all existing entity (type, canonical_name) pairs from DB."""
    out: dict[str, set[str]] = {}
    try:
        resp = (
            client.schema(SCHEMA).table("entities")
            .select("type,canonical_name").execute()
        )
        for row in resp.data or []:
            t = row.get("type") or ""
            cn = row.get("canonical_name") or ""
            if t and cn:
                out.setdefault(t, set()).add(cn)
    except Exception as e:
        logging.warning("fetch_existing_canonicals failed: %s", e)
    return out


# ---- DB writes ----------------------------------------------------------

def upsert_entity(client, ent_type: str, canonical_name: str,
                  new_aliases: list[str], new_metadata: dict) -> tuple[int, bool]:
    """Find or create an entity. Merges aliases + metadata if it exists.
    Returns (entity_id, was_newly_inserted).
    """
    existing = (
        client.schema(SCHEMA).table("entities")
        .select("*").eq("type", ent_type).eq("canonical_name", canonical_name)
        .execute()
    )
    if existing.data:
        e = existing.data[0]
        merged_aliases = sorted(set((e.get("aliases") or []) + (new_aliases or [])))
        merged_metadata = {**(e.get("metadata") or {}), **(new_metadata or {})}
        if merged_aliases != (e.get("aliases") or []) or merged_metadata != (e.get("metadata") or {}):
            client.schema(SCHEMA).table("entities").update({
                "aliases": merged_aliases,
                "metadata": merged_metadata,
            }).eq("id", e["id"]).execute()
        return int(e["id"]), False

    result = client.schema(SCHEMA).table("entities").insert({
        "type": ent_type,
        "canonical_name": canonical_name,
        "aliases": new_aliases or [],
        "metadata": new_metadata or {},
    }).execute()
    return int(result.data[0]["id"]), True


def insert_mentions(client, entity_id: int, mentions: list[dict],
                    source_type: str, sid_to_path: dict[str, Path]) -> int:
    rows = []
    for m in mentions:
        sid = m.get("source_id", "")
        path = sid_to_path.get(sid)
        if path is None:
            continue
        try:
            rel = str(path.relative_to(AGENT_DATA))
        except ValueError:
            rel = str(path)
        rows.append({
            "entity_id": entity_id,
            "source_type": source_type,
            "source_path": rel,
            "context_snippet": (m.get("context_snippet") or "")[:1500],
        })
    if not rows:
        return 0
    client.schema(SCHEMA).table("mentions").insert(rows).execute()
    return len(rows)


# ---- post-run summary ---------------------------------------------------

def print_index_summary(client) -> None:
    """Print: count by type, sample of 20 entities mixed by type, disambiguation list."""
    try:
        ents = client.schema(SCHEMA).table("entities").select("*").execute().data or []
    except Exception as e:
        logging.error("Could not fetch entities for summary: %s", e)
        return

    by_type: dict[str, list[dict]] = {}
    needs_dis: list[dict] = []
    for e in ents:
        by_type.setdefault(e.get("type", ""), []).append(e)
        if (e.get("metadata") or {}).get("needs_disambiguation") is True:
            needs_dis.append(e)

    print()
    print("=" * 70)
    print(f"INDEX STATE  (signal_agent.entities total: {len(ents)})")
    print("=" * 70)
    print()
    print("Counts by type:")
    for t in sorted(by_type.keys()):
        print(f"  {t:10s}  {len(by_type[t])}")
    print()

    # Mixed sample: up to 7 from each of person/project/topic, prefer those
    # with non-empty aliases or richer mentions.
    print("Sample of 20 entities (mix of types, aliases visible):")
    print("-" * 70)
    sample: list[dict] = []
    for t in ("person", "project", "topic", "deadline"):
        bucket = by_type.get(t, [])
        # sort by len(aliases) desc, so merged entities surface first
        bucket = sorted(bucket, key=lambda e: -len(e.get("aliases") or []))
        sample.extend(bucket[:7])
    sample = sample[:20]
    for e in sample:
        aliases = e.get("aliases") or []
        alias_str = ("  aliases=" + ", ".join(aliases)) if aliases else ""
        meta = e.get("metadata") or {}
        flag = "  [needs_disambiguation]" if meta.get("needs_disambiguation") else ""
        print(f"  [{e['type']:7s}] {e['canonical_name']}{alias_str}{flag}")
    print()

    print(f"Entities flagged needs_disambiguation: {len(needs_dis)}")
    print("-" * 70)
    if needs_dis:
        for e in sorted(needs_dis, key=lambda x: (x.get("type", ""), x.get("canonical_name", ""))):
            print(f"  [{e['type']:7s}] {e['canonical_name']}")
    print()


# ---- main ---------------------------------------------------------------

def parse_since(s: Optional[str]) -> datetime:
    if s:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - timedelta(days=90)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="notes",
                        choices=["notes", "mail-work", "mail-personal"],
                        help="Source to index")
    parser.add_argument("--since", default=None,
                        help="ISO date (YYYY-MM-DD); default: 90 days ago")
    parser.add_argument("--limit", type=int, default=50,
                        help="Process at most N items (default: 50)")
    parser.add_argument("--full", action="store_true",
                        help="Ignore --limit; process every item since cutoff")
    parser.add_argument("--max-cost", type=float, default=5.0,
                        help="Hard USD cap for this run (default: 5.0)")
    parser.add_argument("--exclude-folder", action="append", default=[],
                        metavar="FOLDER",
                        help="Skip notes from this Apple Notes folder. "
                             "Repeatable: --exclude-folder Logins --exclude-folder Instagram")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be inserted; don't write to DB")
    args = parser.parse_args()

    setup_logging("agent_index")
    started = datetime.now(timezone.utc)
    since_dt = parse_since(args.since)
    exclude_folders = set(args.exclude_folder or [])
    agent_cfg = load_agent_config()
    system_prompt = INDEX_SYSTEM_PROMPT_TEMPLATE.format(
        user_full_name=agent_cfg["user_full_name"],
        user_first_name=agent_cfg.get("user_first_name", agent_cfg["user_full_name"]),
    )
    logging.info(
        "Starting agent_index source=%s since=%s limit=%s full=%s max_cost=$%.2f "
        "exclude_folders=%s dry_run=%s",
        args.source, since_dt.date().isoformat(),
        args.limit, args.full, args.max_cost,
        sorted(exclude_folders) or "[]", args.dry_run,
    )

    # Source dispatch: collect (source_id, item_dict) pairs and a parallel
    # source_paths list for resolving mentions to file paths.
    if args.source == "notes":
        items_paths = list_notes_filtered(since_dt, exclude_folders)
        if not args.full and args.limit is not None:
            items_paths = items_paths[: args.limit]
        items: list[tuple[str, dict]] = [(p.stem, load_note(p)) for p in items_paths]
        logging.info("Selected %d notes", len(items_paths))
    elif args.source in ("mail-work", "mail-personal"):
        mail_dir = MAIL_WORK_DIR if args.source == "mail-work" else MAIL_PERSONAL_DIR
        items_paths = list_mail_eml_since(mail_dir, since_dt)
        if not args.full and args.limit is not None:
            items_paths = items_paths[: args.limit]
        items = [(p.stem, load_mail(p)) for p in items_paths]
        logging.info("Selected %d %s messages", len(items_paths), args.source)
    else:
        logging.error("Unknown source %r", args.source)
        return 1

    if not items:
        logging.info("Nothing to do")
        return 0

    sid_to_path: dict[str, Path] = {sid: p for (sid, _), p in zip(items, items_paths)}

    anthropic_client = get_anthropic_client()
    cost_tracker = CostTracker(max_cost=args.max_cost)
    supabase = get_supabase_client() if not args.dry_run else None

    all_entities: list[dict] = []
    aborted_for_cost = False

    for i in range(0, len(items), BATCH_SIZE):
        batch = items[i : i + BATCH_SIZE]
        logging.info("Batch %d: %d items", i // BATCH_SIZE + 1, len(batch))
        try:
            entities = index_batch(
                anthropic_client, cost_tracker, batch, system_prompt, args.source,
            )
        except CostBudgetExceeded as e:
            logging.warning("Stopping early: %s", e)
            aborted_for_cost = True
            break
        all_entities.extend(entities)

    # Fetch existing canonicals from DB so dedup can redirect short forms
    # (e.g., mail "Wyatt" → existing "Wyatt Korff" from notes).
    existing_canonicals = (
        fetch_existing_canonicals(supabase) if supabase is not None else {}
    )

    # Dedup
    deduped, dedup_stats = dedup_entities(all_entities, existing_canonicals)
    logging.info(
        "Dedup: %d → %d (exact=%d, fuzzy_in_batch=%d, redirected_to_db=%d, ambiguous=%d)",
        dedup_stats["input_count"], dedup_stats["output_count"],
        dedup_stats["exact_collapsed"], dedup_stats["fuzzy_merged"],
        dedup_stats["redirected_to_existing"], dedup_stats["ambiguous_flagged"],
    )

    # Output
    inserted_entities_new = 0
    matched_existing = 0
    inserted_mentions = 0
    if args.dry_run:
        print()
        print("=" * 70)
        print(f"DRY RUN — {dedup_stats['output_count']} entities after dedup "
              f"(was {dedup_stats['input_count']})")
        print("=" * 70)
        for ent in deduped:
            print(json.dumps(ent, indent=2))
            print()
    else:
        for ent in deduped:
            try:
                entity_id, was_new = upsert_entity(
                    supabase,
                    ent.get("entity_type", ""),
                    ent.get("canonical_name", ""),
                    ent.get("aliases", []) or [],
                    ent.get("metadata", {}) or {},
                )
                if was_new:
                    inserted_entities_new += 1
                else:
                    matched_existing += 1
                inserted_mentions += insert_mentions(
                    supabase, entity_id,
                    ent.get("mentions", []) or [],
                    args.source, sid_to_path,
                )
            except Exception as e:
                logging.error("Entity write failed for %r: %s",
                              ent.get("canonical_name"), e)
        print_index_summary(supabase)
        logging.info(
            "DB writes: %d new, %d matched existing, %d mentions",
            inserted_entities_new, matched_existing, inserted_mentions,
        )

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
        "items_processed": len(items_paths),
        "entities_extracted": len(all_entities),
        "entities_after_dedup": dedup_stats["output_count"],
        "dedup_stats": dedup_stats,
        "entities_inserted_new": inserted_entities_new,
        "entities_matched_existing": matched_existing,
        "mentions_written": inserted_mentions,
        "cost": cost_tracker.summary(),
    }
    write_run_state("agent_index", summary)
    logging.info(
        "Done. items=%d entities_extracted=%d new=%d matched=%d mentions=%d cost=$%.4f",
        len(items_paths), len(all_entities),
        inserted_entities_new, matched_existing, inserted_mentions,
        cost_tracker.total_cost,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
