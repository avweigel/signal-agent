# signal-agent — Project Handoff

This document is the handoff from a Claude.ai conversation to a local Claude Code session in VSCode. It summarizes everything built, the architecture, what's left, and the immediate next steps.

## The Big Picture

The user is building a personal AI agent that scans across their data sources (email, calendar, notes, code) and surfaces tasks they need to do, with proposed solutions. They are a Project Scientist leaving their current institution in early June 2026 for a new role abroad. The agent has two purposes:

1. **Day-to-day task discovery** across overlapping work, personal, transition, and game-management contexts
2. **Insurance against work data going dark** when access expires — capturing mail, notes, calendars locally before they become inaccessible

The architecture has two layers:

- **Data layer (mostly built):** dumpers that snapshot each source to flat files in `~/workspace/agent-data/`. Run on schedule via launchd.
- **Agent layer (Phase 7, not yet built):** scripts that read from the data layer, call Claude API to extract tasks, write to SQLite, surface results via daily digest + on-demand CLI.

## Where Things Live

```
~/workspace/signal-agent/           # the code (this repo)
├── scripts/
│   ├── dump_apple_notes.py + .js   # Apple Notes via JXA
│   ├── dump_apple_mail.py          # Work mail via Apple Mail's local cache
│   ├── dump_game_calendar.py       # Game calendars via iCal feeds
│   ├── dump_gmail.py               # Personal Gmail via Gmail API
│   ├── setup.sh
│   ├── apple_mail_config.example.json
│   ├── apple_mail_config.local.json   # gitignored
│   ├── calendars.example.json
│   └── calendars.local.json           # gitignored
├── launchd/
│   ├── com.aubrey.agent.notes.plist
│   ├── com.aubrey.agent.calendar.plist
│   ├── com.aubrey.agent.mail.plist
│   ├── com.aubrey.agent.gmail.plist
│   └── install.sh
├── docs/
│   ├── exporting-work-mail.md      # generic version
│   ├── decisions.md
│   └── TODO.md
├── requirements.txt
├── README.md
├── LICENSE
└── HANDOFF.md                      # this file

~/workspace/agent-data/             # the data (NEVER in the repo)
├── notes/                          # one .md per Apple note, frontmatter + body
├── calendar-game/                  # raw .ics + per-event .json
├── mail-work/                      # .eml + .json sidecars (year/month sharded)
│   └── attachments/                # 15 GB of attachments from partial messages
├── mail-personal/                  # Gmail dump (year/month sharded)
├── archives/
│   ├── work-mail/
│   │   └── work-mail-2026.olm      # 210 MB, irreplaceable safety net
│   ├── work-onedrive/              # not yet captured
│   └── work-workday/               # not yet captured
├── personal-notes/
│   └── janelia-departure.md        # personal departure logistics, out of repo
├── state/                          # cursors, hashes, OAuth tokens
└── logs/                           # per-script logs
```

## What's Working

| Source | Output | Schedule | Status | Notes |
|---|---|---|---|---|
| Apple Notes | 2,068 .md files (1.5 GB) | Hourly | ✅ Working | First dump took 27 min; idempotent re-runs are fast |
| Game calendars (4) | 178 events | Every 4 hr | ✅ Working | |
| Apple Mail (work) | 44,336 bodies + 17,140 partial stubs + 28,199 attachments (15 GB) | Every 30 min | ✅ Working | Reads Apple Mail's local `.emlx` cache directly |
| Personal Gmail | ~37k messages full pull | Every 30 min | ✅ Working | Smoke test of 5 messages passed; full pull was kicked off at handoff time |

All four launchd jobs load and run. Apple Mail dumper required granting `/usr/bin/python3` Full Disk Access in System Settings → Privacy & Security.

## Critical Context: The Microsoft Graph Failure

The original plan for work mail was a Microsoft Graph API dumper that would do incremental sync via the Graph delta endpoint. This **failed** at the consent step:

- An Azure app was successfully registered
- Permissions were added: Mail.Read, offline_access, User.Read (all delegated)
- The org's tenant policy requires admin consent for all third-party apps regardless of permission scope, and the user doesn't have admin and can't get it granted
- So when the OAuth flow runs, the user sees "Need admin approval" and can't proceed

**The pivot:** read Apple Mail's local `.emlx` cache instead. Apple Mail had already downloaded most messages locally over years of normal use. The Apple Mail dumper (`dump_apple_mail.py`) walks `~/Library/Mail/V10/<account-uuid>/`, parses `.emlx` files, copies attachments from sibling `Attachments/<msgId>/` directories, and writes clean `.eml` + `.json` sidecars.

The Graph dumper has been removed from the repo. If the user ends up at a future workplace where they control their own Azure tenant, the same approach could be reimplemented there.

## The Partial Message Problem

Apple Mail's "Download Attachments" setting was historically not aggressive, so 17,140 messages (25% of mailbox) are `.partial.emlx` — header-only, no body. The .olm export from Outlook had 100% of bodies but no attachments. So:

- Bodies: .olm covers all 46k. Apple Mail dump covers 44k full + 17k stubs.
- Attachments: .olm has none. Apple Mail dump has 15 GB across 28k files.

The setting has been flipped to "All attachments" and a Smart Mailbox was created (`Janelia Force Sync`) to force Mail to iterate over all messages. As of handoff time, Mail is re-syncing in the background. Re-running `dump_apple_mail.py` periodically over the coming weeks will pick up newly-completed bodies — the script has an `upgraded` counter that tracks partial → full transitions.

There's a final pre-departure pass to do close to the user's last day: another `.olm` export and another `dump_apple_mail.py` run to capture anything that arrived in the final week.

## Phase 7: The Agent (NOT BUILT YET)

This is what the user actually wants. The dumpers above are infrastructure; the agent is the product.

### Architecture (decided)

- **Local Python + SQLite + Anthropic API.** No web app for v1.
- **Daily digest + on-demand CLI.** No persistent task state UI.
- **Per-source extraction.** Calendar, mail, and notes have very different signal-to-noise; they want different prompts.

### Scripts to build

```
scripts/
  agent_extract.py    # reads new files since last cursor, calls Claude per source,
                      # writes tasks to SQLite
  agent_digest.py     # SQLite → ~/workspace/agent-data/digests/<date>.md
  agent_ask.py        # CLI: "what's pending from <person>?"
  agent_review.py     # spot-check recent extractions during prompt iteration
state/
  agent.sqlite        # tasks table + per-source cursors
```

### Open design questions

1. **What is a "task" for the user?** Needs concrete answers before writing the extraction prompt. The shape: things they owe someone, deadlines they owe themselves, self-authored TODOs in notes, promised replies. NOT: FYI threads, recurring calendar events with no prep, meeting notes without follow-ups.

2. **What categories should the daily digest organize tasks by?** Not generic. Theirs: work exit, new-job onboarding, international move, manuscripts, project handoffs, alliance scheduling, personal admin. The user was asked to think about this overnight and write categories in a notes file.

3. **Per-source prompts vs. unified.** Lean per-source. Calendar extraction is mostly "is there prep work needed for this event"; mail extraction is "what is being asked of me, what have I promised"; notes extraction is "what TODOs did I write to myself."

4. **Cursor strategy.** For each source, a "last seen" pointer (file mtime or hash) so each run only processes new content. Starting cursor = epoch (process everything once).

## Outstanding TODOs (in priority order)

### Immediate

1. **Confirm Gmail full pull completed.** Check `~/workspace/agent-data/state/dump_gmail.json` — `found` should be ~37,066, `errors` near zero. If still running, wait.
2. **The user writes "agent extraction prompt seeds" note.** 5 minutes of thinking about task categories. This shapes Phase 7 design.

### Short-term (this week)

3. **Phase 7 build:** `agent_extract.py` first, with a deliberately small per-source prompt. Then `agent_review.py` to eyeball output. Iterate prompt until it feels right. Then `agent_digest.py` and `agent_ask.py`.
4. **Migrate to Python 3.12 via Homebrew.** System Python 3.9 is EOL; google-auth has warned. `brew install python@3.12`, update launchd plists, re-grant FDA to the new binary.
5. **GitHub dumper.** The third gap from the original "scan everything" goal was GitHub issues/PRs/TODOs. Worth picking back up. Auth via PAT, repos to track: a few of the user's active repos. Decisions deferred.

### Medium-term (next 2-4 weeks)

6. **Final pre-departure mail capture.** ~1 week before last day at current job:
   - Second .olm export
   - Final `dump_apple_mail.py` run
   - Final `dump_apple_notes.py` run
   - Capture OneDrive contents
   - Capture Workday docs (W-2, pay stubs, benefits PDFs)
   - Save TIAA / ADP login info
7. **Recurring-event dedup fix in calendar dumper.** Known bug: VEVENTs with `RECURRENCE-ID` overrides currently overwrite each other on disk (last-wins). Fine for now but flagged in TODO.md.
8. **Cosmetic fix in notes dumper.** Stray `**` lines from html2text on empty bold tags. One-line regex fix; bundled with next iteration.

### Long-term (post-move)

9. **New workplace M365 dumper.** Once onboarded, set up Graph API access in the new tenant where the user controls their own permissions.
10. **Web UI / mobile access.** If the daily digest workflow earns its keep, port to Next.js + Supabase.

## Operational Notes

### Smart Mailbox: Force Sync

A Smart Mailbox was created in Apple Mail to force re-download of partial messages. It's filtered by Account = (work account), with "Include messages from Sent" checked. Apple Mail's "Download Attachments" account setting was changed from default to "All". Together these will slowly backfill the 17k partial messages. Don't quit Apple Mail; let it run. Plug in power.

### Calendar URL secrets

Were rotated once. Then the rotated ones got pasted in the original conversation history. The user decided this was acceptable risk given the calendars are read-only feeds of low-stakes content. They are NOT rotated a second time. Don't worry about this further unless something changes.

### Full Disk Access

Granted to `/usr/bin/python3` system-wide so launchd-spawned mail dumper can read `~/Library/Mail/`. Tighter scoping was discussed but not implemented; the broad grant is fine on a personal Mac. When migrating to Python 3.12, FDA needs to be re-granted to the new binary path.

### OAuth tokens and secrets

Live in `~/workspace/agent-data/state/`:
- `gmail-token.json` (Google Credentials JSON, file mode 0600) — refresh token, lasts indefinitely with use
- `gmail-oauth-client.json` (Google OAuth client secret, file mode 0600) — from Google Cloud Console
- `gmail-history-id.txt` (Gmail incremental cursor)

None of these go in the repo.

## Anti-Goals (Things NOT to Do)

- **Don't try to fix the Graph admin consent thing.** It's an org policy decision and the workaround (Apple Mail) is good enough.
- **Don't preemptively build a web UI.** Daily digest in markdown + CLI for queries is the v1 surface.
- **Don't try to extract tasks from every email.** Per-source prompts should reflect different signal-to-noise levels.
- **Don't auto-execute proposed solutions.** The agent surfaces tasks and proposes actions; the user reviews and triggers. No write actions without human approval.
- **Don't expand to "scan everything" prematurely.** Slack, GitHub, OneDrive, Notion all exist as future sources. Add when demonstrably needed.

## How to Resume

When you start the next session:

1. Read this file
2. Read `docs/TODO.md` for active work items
3. Run `python3 scripts/dump_gmail.py --non-interactive` to verify Gmail is still working
4. Check `launchctl list | grep com.aubrey.agent` — should show four entries
5. Start the immediate todos: confirm Gmail full pull, wait for the user's "task categories" note before starting Phase 7

Good luck.
