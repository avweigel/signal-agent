# signal-agent

A personal task-discovery agent. Reads your data sources (mail, notes,
calendars), extracts tasks and deadlines, surfaces them to you.

## Status

**Data layer** — built and running. Four dumpers snapshot their sources to
flat files under `~/workspace/agent-data/` on a schedule. Idempotent,
incremental, survive restarts.

**Agent layer** — not yet built. The plan: one or more local Python scripts
read the dumped files, call the Claude API to extract tasks, persist results
to SQLite, and produce a daily digest plus on-demand queries. See
`HANDOFF.md` for full context and `docs/decisions.md` for the rationale
behind the design.

## Architecture

The two-layer split is deliberate. The dumpers don't call any LLM and don't
interpret content; they produce auditable, replayable flat-file snapshots.
The agent layer is the only thing that calls Claude. Either layer can be
swapped without touching the other.

```
data sources              data layer (this repo)         agent layer (planned)
─────────────             ──────────────────────         ─────────────────────
Apple Notes      ─JXA──→  dump_apple_notes.py
Apple Mail       ─────→   dump_apple_mail.py    ──→     extract → agent.sqlite
                                                         digest  → digests/*.md
iCal feeds       ─HTTP─→  dump_game_calendar.py          ask     (CLI query)
Gmail API        ─OAuth→  dump_gmail.py
```

## Data sources

Each dumper produces flat files plus a `.json` sidecar with parsed metadata.

1. **Apple Notes** via JXA scripting bridge. One `.md` per note, YAML
   frontmatter + body. Content-hash for change detection.
2. **Apple Mail** via the local `~/Library/Mail/V10/` cache. Walks `.emlx`
   files, converts to RFC 822 `.eml`, captures attachments from sibling
   `Attachments/` directories. Requires Full Disk Access for the Python
   binary launchd uses.
3. **iCal feeds** via direct HTTP fetch of secret iCal URLs from Google
   Calendar settings. No OAuth, no Google API quotas. Anyone with the URL
   can read the calendar — treat the URL itself as the secret.
4. **Gmail** via the Gmail API with OAuth desktop flow + refresh token.
   Uses the History API for incremental sync after the first full pull.
   First run is interactive (browser); subsequent runs are non-interactive.

## Install

```
git clone https://github.com/avweigel/signal-agent.git ~/workspace/signal-agent
cd ~/workspace/signal-agent
bash scripts/setup.sh
```

`setup.sh` creates the data tree under `~/workspace/agent-data/` and
installs Python dependencies. Idempotent; safe to re-run.

### Configure each source

The repo ships `*.example.json` files. Copy each to `*.local.json` and fill
in your values. The `.local.json` files are gitignored.

| Source | Setup |
|---|---|
| Apple Notes | None. Grant Automation permission to your terminal on first run when macOS prompts. |
| Apple Mail | Find your account UUID with `ls ~/Library/Mail/V10/`. Copy `apple_mail_config.example.json` to `.local.json` and fill in. Grant `/usr/bin/python3` Full Disk Access in System Settings → Privacy & Security. |
| iCal | Copy `calendars.example.json` to `.local.json`. Add one entry per calendar with name + secret iCal URL ("Secret address in iCal format" in calendar settings). |
| Gmail | Create a Google Cloud OAuth client (Desktop App type). Save the downloaded JSON as `~/workspace/agent-data/state/gmail-oauth-client.json` with mode `0600`. Run `python3 scripts/dump_gmail.py` once interactively to seed the token cache. |

### Run

```
python3 scripts/dump_apple_notes.py
python3 scripts/dump_apple_mail.py
python3 scripts/dump_game_calendar.py
python3 scripts/dump_gmail.py
```

Each writes a per-run state JSON under `~/workspace/agent-data/state/` and
appends to a per-dumper log under `~/workspace/agent-data/logs/`.

### Schedule (optional)

```
bash launchd/install.sh
```

Loads four launchd jobs that fire every 30 minutes. Adjust `StartInterval`
in the `.plist` files if needed.

## Data layout

```
~/workspace/agent-data/
├── notes/                 one .md per Apple note
├── mail-work/             Apple Mail dump (year/month sharded .eml + .json)
├── mail-personal/         Gmail dump (year/month sharded .eml + .json)
├── calendar-game/         one .ics per calendar + per-event .json
├── archives/              one-time snapshots kept for safety
│   ├── work-mail/         e.g. .olm exports before leaving an org
│   ├── work-cloud/
│   ├── work-hr/
│   └── work-misc/
├── state/                 per-dumper cursors, tokens, run summaries
├── logs/                  per-dumper logs
└── personal-notes/        out-of-repo notes for your own reference
```

The data tree lives outside the repo on purpose — it's specific to your
machine and contains the full content of your mail, notes, and calendars.

## Security

- **Secrets stay outside the repo.** Files matching `*.local.json` are
  gitignored. The `.local.json` naming is itself a signal: anything matching
  that glob is local-only.
- **OAuth tokens** (Gmail) live under `~/workspace/agent-data/state/` with
  mode `0600`.
- **iCal URLs are effectively secrets.** Anyone with the URL can read the
  calendar without auth. Rotate via Google Calendar's "Reset" button if a
  URL is exposed (e.g. pasted into chat history).
- **The data tree is sensitive.** Your full mail archive, notes, and
  calendar history live in plaintext under `~/workspace/agent-data/`. Don't
  sync that path to anywhere you wouldn't put your password manager.

## Dependencies

- macOS (the Apple Notes and Apple Mail dumpers use macOS-specific APIs).
- Python 3.11+ recommended. The system Python 3.9 still works but is past
  end-of-life — see `docs/TODO.md` for the migration plan.
- See `requirements.txt` for the pip dependency list.

## References

- `HANDOFF.md` — full project context for picking this up later
- `docs/decisions.md` — log of consequential design decisions
- `docs/exporting-work-mail.md` — how to archive work mail before leaving an organization
- `docs/TODO.md` — open items
