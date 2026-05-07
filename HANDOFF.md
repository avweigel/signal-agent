# signal-agent — Project Handoff

This document is the rolling handoff between Claude.ai conversations and local Claude Code sessions. Read this first when resuming.

## The Big Picture

The user is building a personal AI agent that scans their data sources (notes, mail, calendar, code) and surfaces tasks they need to do. They are a Project Scientist leaving their current institution in early June 2026 for a new role abroad. The agent has two purposes:

1. **Day-to-day task discovery** across overlapping work, personal, transition, and game-management contexts
2. **Insurance against work data going dark** — capturing mail, notes, calendars locally before access expires

Two-layer architecture:

- **Data layer** — built and running. Per-source dumpers snapshot to flat files in `~/workspace/agent-data/`. Scheduled via launchd.
- **Agent layer (Phase 7)** — index + extract validated; digest + ask + explore still to build.

## Where Things Live

```
~/workspace/signal-agent/             # the code (this repo, public on GitHub)
├── scripts/
│   ├── dump_apple_notes.py + .js     # Apple Notes via JXA
│   ├── dump_apple_mail.py            # Work mail via Apple Mail's local cache
│   ├── dump_game_calendar.py         # Game calendars via iCal feeds
│   ├── dump_gmail.py                 # Personal Gmail via Gmail API
│   ├── agent_common.py               # Shared: secrets, clients, retry, CostTracker, cached_system
│   ├── agent_index.py                # Phase 7: entity indexing + cross-source dedup
│   ├── agent_extract.py              # Phase 7: task extraction with entity-linking + past-due triage
│   ├── agent_dedup_cleanup.py        # One-time DB cleanup pass for short→long entity merges
│   ├── migrate.py                    # Migration runner (psycopg2 or dashboard SQL editor)
│   ├── migrations/
│   │   ├── 001_initial_schema.sql    # tasks, cursors, categories, digests
│   │   └── 002_index_schema.sql      # entities, mentions, relationships
│   ├── setup.sh
│   ├── apple_mail_config.example.json
│   ├── apple_mail_config.local.json  # gitignored
│   ├── agent_config.example.json     # user identity for prompts
│   ├── agent_config.local.json       # gitignored
│   ├── calendars.example.json
│   └── calendars.local.json          # gitignored
├── launchd/
│   ├── com.aubrey.agent.notes.plist
│   ├── com.aubrey.agent.calendar.plist
│   ├── com.aubrey.agent.mail.plist
│   ├── com.aubrey.agent.gmail.plist
│   └── install.sh
├── docs/
│   ├── exporting-work-mail.md
│   ├── decisions.md
│   └── TODO.md
├── requirements.txt
├── README.md
├── LICENSE
└── HANDOFF.md                        # this file

~/workspace/agent-data/               # the data (NEVER in the repo)
├── notes/                            # one .md per Apple note, frontmatter + body
├── calendar-game/                    # raw .ics + per-event .json
├── mail-work/                        # .eml + .json sidecars (year/month sharded)
│   └── attachments/                  # 15 GB of attachments from partial messages
├── mail-personal/                    # Gmail dump (year/month sharded)
├── archives/
│   └── work-mail/<date>.olm          # Outlook export safety net
├── personal-notes/                   # out-of-repo notes
├── state/
│   ├── anthropic-api-key             # 0600
│   ├── supabase-url                  # 0600 (URL is not strictly secret)
│   ├── supabase-service-role-key     # 0600
│   ├── gmail-oauth-client.json       # 0600
│   ├── gmail-token.json              # 0600
│   ├── gmail-history-id.txt
│   ├── dump_*.json                   # per-dumper run state
│   └── agent_*.json                  # per-Phase-7-script run state
└── logs/                             # per-script logs
```

## Data Layer — What's Working

| Source | Output | Schedule | Status | Notes |
|---|---|---|---|---|
| Apple Notes | 2,068 .md files (1.5 GB) | Hourly | ✅ | First dump 27 min; idempotent re-runs are fast |
| Game calendars (4) | 178 events | Every 4 hr | ✅ | |
| Apple Mail (work) | 44,336 bodies + 17,140 partial stubs + 28,199 attachments (15 GB) | Every 30 min | ✅ | Reads `.emlx` cache directly |
| Personal Gmail | ~37k messages | Every 30 min | ✅ | Cached refresh token, non-interactive |

All four launchd jobs load and run. Apple Mail dumper required granting `/usr/bin/python3` Full Disk Access in System Settings → Privacy & Security.

## Critical Context: The Microsoft Graph Failure

The original plan for work mail was a Microsoft Graph API dumper. Failed at the consent step (HHMI tenant requires admin consent for all third-party apps regardless of scope; user can't grant). Pivoted to reading Apple Mail's local `.emlx` cache. Graph dumper removed from repo. If a future workplace allows tenant-level admin, reimplement there.

## The Partial Message Problem

17,140 messages (25%) are `.partial.emlx` — header-only, no body. The .olm covers all 46k bodies but no attachments. Apple Mail's account setting was flipped to "All attachments" + a `Janelia Force Sync` Smart Mailbox added. Periodic re-runs of `dump_apple_mail.py` should pick up newly-completed bodies (the script has an `upgraded` counter). Final pre-departure `.olm` export still pending.

## Phase 7: Agent Layer — Progress Through 2026-05-07

### Storage

- **Supabase** project: `data-hub` (URL `ztnnmpnphedmpvwdqkya.supabase.co`)
- **Schema:** `signal_agent`
- **Tables:** `tasks`, `cursors`, `categories`, `digests`, `entities`, `mentions`, `relationships` (all in place)
- **Credentials:** `~/workspace/agent-data/state/supabase-url` and `supabase-service-role-key`, both `0600`. The anon key isn't used (we always go in as service role from local scripts).
- Anthropic key at `~/workspace/agent-data/state/anthropic-api-key`, `0600`.

### Architecture: index-first, then extract

Build order (proven by this point in the project):

```
schema migration  →  agent_index.py  →  agent_extract.py
                                              ↓
                              (next) agent_digest.py  →  agent_explore.py  →  agent_ask.py
```

The index layer builds a stable graph of named entities (people, projects, topics, deadlines) before task extraction. Extraction then grounds tasks against the existing entity index — so the model doesn't re-discover "Wyatt Korff" or "CellMap" every run, and extracted tasks get a `primary_entity_id` foreign key.

### Index state — 272 entities post-cleanup

| Type | Count |
|---|---|
| person | 130 |
| project | 43 |
| deadline | 63 |
| topic | 36 |
| **total** | **272** |

Built from samples across notes, mail-work, mail-personal. Cross-source linking confirmed working: a mail-extracted "Wyatt" merges into the existing notes-extracted "Wyatt Korff" rather than creating a duplicate. Bug-fix history below.

### Extract state — 49 tasks from 50-note sample

`agent_extract.py` shipped with:
- Per-source system prompts (notes / mail; calendar pending)
- Entity index embedded in cached prompt prefix (model emits `primary_entity_name`, post-resolved to `primary_entity_id` via canonical+alias lookup)
- Past-due triage as Python post-processor (not in prompt — see decisions below)
- Tool-use via `submit_tasks` schema
- Idempotent on `(source_type, source_path, title)`
- `--dry-run` and `--max-cost` flags

Validation run (50 notes, --since 2026-01-01): **49 tasks extracted, 82% entity-linked, $0.07** (with cache; 9/10 batches hit cache).

### Cost discipline

- **Models:** `claude-haiku-4-5-20251001` for extraction + indexing; `claude-opus-4-7` reserved for synthesis (digest narrative, ask). Configurable via `EXTRACT_MODEL` / `INDEX_MODEL` / `SYNTHESIS_MODEL` constants in `agent_common.py`.
- **Prompt caching active.** System prompt is 5,303 tokens (worked examples + edge cases + entity index), padded past Haiku 4.5's empirical cache threshold (~4,000 tokens — higher than the 2,048 documented minimum). Subsequent batches get `cache_r=4980` reads. ~60–70% cost reduction on input tokens.
- **Hard cost cap.** `CostTracker` raises `CostBudgetExceeded` before any batch that would push run total past `--max-cost`. Default $5; sample runs use $1.

### Available API credit

- **$8.13** from Anthropic credit grant, **~$1 spent** so far on indexing + extract iteration.
- **Auto-reload disabled.** When credits hit zero, API stops; no auto-charge. Hard ceiling already in place.
- A monthly spend limit was also set in the Anthropic console as a backup guardrail.

## Decisions That Took Multiple Iterations to Land

These are documented so future sessions don't re-litigate them.

### 1. Don't trust the model for date arithmetic.

**What we tried first:** put past-due triage rules in the system prompt ("today is {today}; if due_date < today and ≤30d past, prepend [OVERDUE] and set priority 1"). Haiku 4.5 didn't reliably apply the transformation — the only past-due task in our test (Harald talk, 33 days past) came back without the [STALE] tag.

**What works:** moved triage to deterministic Python post-processor `apply_past_due_rules(tasks, today)` that runs in `extract_batch` after the model returns. Three buckets:

- ≤30 days past → prepend `[OVERDUE] `, set priority 1
- 30 < x ≤ 180 days past → prepend `[STALE] `, set priority 4
- \>180 days past → drop entirely

Unit-tested on 13 synthetic cases (including 30-day and 180-day boundaries); validated on real data with 4–13 STALE tags per run. **Don't put this back in the prompt.**

### 2. Don't trust the model to invent dates.

**What we hit:** the model fabricated `due_date: "2026-04-30"` for 5 separate tasks because the source said "do this by april". False-confidence dates are worse than missing dates.

**What works:** explicit prompt rule with examples — "by April" → null, "by end of April" → null, "next week" → null. Only an explicit date in the source becomes a `due_date`. Vague references go in `description` only. Verified by spot-checking source notes.

### 3. Conservative dedup with `needs_disambiguation` flag.

Short-form fuzzy merge (e.g., `Wyatt` → `Wyatt Korff`) auto-applies only when the long-form match is unambiguous. If multiple long-form candidates exist (e.g., `Stephan` could be `Stephan Preibisch` OR `Stephan Saalfeld`), the short form keeps its row but gets `metadata.needs_disambiguation: true`. `agent_explore.py` (planned) will be the interactive resolver.

### 4. Long-form-subsumes-short-form bug.

**Symptom:** notes extracted `Dr. Shahzeidi`, mail later extracted `Dr. Shahriar Shahzeidi`. Both ended up as separate rows.

**Fix:** in `dedup_entities`, after the short→long pass, also check each new long-form against existing DB short-forms. If a new long-form unambiguously subsumes an existing short, tag the entity with `_consume_db_short`. `upsert_entity` reads that tag and UPDATEs the existing row's `canonical_name` to the new long form (instead of inserting a duplicate). Old short stays as alias. Mentions stay attached by foreign key — no migration needed.

For data already in the DB before this fix, `agent_dedup_cleanup.py` does a one-time pass: 12 merges applied (e.g., `Oliver` → `Oliver Weigel`, `Christel` → `Christel Genoud`, `Wyatt` → `Wyatt Korff`).

### 5. Cross-source dedup uses DB state, not just within-run.

`dedup_entities` accepts `existing_by_type: dict[str, set[str]]` (fetched once at run start via `fetch_existing_canonicals()`). Short-form fuzzy candidates are pulled from BOTH the current batch AND existing DB canonicals. A mail-extracted "Wyatt" with no in-batch long form still redirects to the existing notes-extracted "Wyatt Korff".

**Bug we fixed along the way:** the same name appearing in both new batch and DB previously counted as 2 candidates → false ambiguity. Now deduplicated via a single `set` before checking ambiguity.

### 6. Conservative date inference.

The Harald talk note says "Talk is April 2" and was modified 2026-02-23 — the year is contextually unambiguous (2026). The model still emitted `due_date: null` (correct under the strengthened don't-fabricate rule). One missed [STALE] tag per run is the cost. **We accepted this** as the right tradeoff vs. risking false-confidence year inference. Not worth a prompt rule for ~1 task per run.

## Next Paths from Here — Recommended Order

1. **First: build `agent_digest.py`** against the 49 currently-extracted tasks. See what daily output actually looks like before scaling extraction further. This is the fastest way to surface "is the prompt good enough" feedback — we'll see categories or gaps the user wants the prompt to handle.
2. **Then: scale notes extract** to the full archive (~2,068 notes, projected ~$3 with cache).
3. **Then: run mail extract** on `mail-work` and `mail-personal` samples (prompt machinery written but unvalidated on real mail).
4. **Defer: `agent_explore.py`** for disambiguation. Only build after extraction reveals which entities actually matter for surfacing tasks. The 19 disambiguation flags can wait — many may resolve naturally as more data arrives.
5. **Defer indefinitely: Harald-style year-inference rule.** One missed task per run isn't worth the prompt complexity.

## Open Issues / Known Gaps

- **19 disambiguation flags** in `signal_agent.entities` waiting for `agent_explore.py`
- **`mail-personal` scaled run** not yet done (only 100-message sample)
- **`mail-work` scaled run** not yet done (only 100-message sample)
- **`agent_extract.py` has only been run on notes**; mail extract prompt machinery is in place but not validated against real mail
- **No digest output exists yet** — `agent_digest.py` is the next thing to build
- **No `agent_explore.py`, `agent_ask.py`** — both planned, neither started
- Recurring-event dedup bug in calendar dumper (RECURRENCE-ID overrides; flagged in `docs/TODO.md`)
- Stray `**` lines in some notes from html2text (cosmetic; flagged in `docs/TODO.md`)
- Python 3.9 EOL warnings — migration to 3.12 still pending (flagged in `docs/TODO.md`)

## Operational Notes

### Smart Mailbox: Force Sync (still relevant)

`Janelia Force Sync` Smart Mailbox in Apple Mail is filtering by Account = work; "Include messages from Sent" checked; account setting changed from default to "All" attachments. Slowly backfilling the 17k partial messages. Don't quit Apple Mail; let it run.

### Calendar URL secrets

Rotated once. Then the rotated ones got pasted in chat. User decided this was acceptable risk (read-only feeds, low-stakes content). NOT rotated a second time.

### Full Disk Access

Granted to `/usr/bin/python3` system-wide. When migrating to Python 3.12, FDA needs to be re-granted to the new binary path.

### Secrets layout

All in `~/workspace/agent-data/state/`, all `0600`. None go in the repo. The repo's `.gitignore` covers `*.local.json` plus belt-and-suspenders entries for specific filenames.

## Anti-Goals (Things NOT to Do)

- **Don't try to fix Graph admin consent.** Org policy. Apple Mail workaround is good enough.
- **Don't preemptively build a web UI.** Markdown digest + CLI are the v1 surface.
- **Don't try to extract tasks from every email.** Per-source prompts reflect different signal-to-noise levels.
- **Don't auto-execute proposed solutions.** Agent surfaces; user reviews and triggers.
- **Don't expand to "scan everything" prematurely.** Slack, GitHub, OneDrive, Notion are deferred.
- **Don't put past-due rules back in the prompt.** Python post-processor is correct here.
- **Don't fabricate dates.** Null beats wrong.

## How to Resume

When you start the next session:

1. Read this file.
2. Check `~/workspace/agent-data/state/dump_*.json` for current dumper status.
3. Look at `signal_agent.entities` and `signal_agent.tasks` in Supabase to see current DB state. Quick sanity:
   ```
   python3 -c "import sys; sys.path.insert(0,'scripts'); from agent_common import get_supabase_client, SCHEMA; c=get_supabase_client(); print('entities:', len(c.schema(SCHEMA).table('entities').select('id', count='exact').execute().data or [])); print('tasks:', len(c.schema(SCHEMA).table('tasks').select('id', count='exact').execute().data or []))"
   ```
4. Read `docs/decisions.md` for the long-form rationale on architectural choices.
5. **First action: build `agent_digest.py`.** Reads from `signal_agent.tasks`, joins to `entities` for context, writes a markdown daily digest at `~/workspace/agent-data/digests/<date>.md`. Use `claude-opus-4-7` for the narrative synthesis, with the task list as structured input.

Good luck.
