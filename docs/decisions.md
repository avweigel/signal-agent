# Decisions

A log of consequential design decisions and the reasoning behind each.
Entries are dated and ordered chronologically. The point is not to relitigate
later, but to make the rationale findable when something changes and the
old reasoning needs to be re-examined.

---

## 2026-05-02 — Per-source dumpers, not a unified ETL

**Context.** Could write a single dumper with a plugin pattern: one binary
that reads from all sources, dispatches by type, writes to one schema.

**Decision.** One Python script per source. Each owns its config, state,
idempotency strategy, scheduling, and failure modes.

**Rationale.** Sources differ enough (auth model, rate limits, page shape,
output format) that a unified abstraction would leak everywhere. Per-source
files are easier to schedule independently in launchd, easier to debug in
isolation, easier to delete or replace if a source goes away. The sidecar
`.json` schema gives downstream consumers a uniform read interface without
forcing a uniform write path.

---

## 2026-05-02 — Apple Notes: file-handoff over osascript stdout

**Context.** Initial `dump_apple_notes.py` used osascript stdout to receive
a JSON array of all notes. With ~2,000 notes averaging ~750 KB each (lots
of inline base64 images), the serialized JSON was ~1.5 GB. osascript stdout
silently truncated to empty — no error, no partial output. Two consecutive
runs returned `0 notes` after ~22 minutes each. Diagnostic JXA inline
showed all 2,068 notes were readable; the failure was strictly in the
stdout pipeline.

**Decision.** Pass an output file path as JXA argv. The JS writes JSON to
that file via `NSString.writeToFileAtomicallyEncodingError` and prints a
small metadata object (path, byte count, note count, totals) to stdout.
The Python wrapper validates the byte count matches the file on disk before
parsing, and hard-errors if `noteCount == 0` while `totalAvail > 0`.

**Rationale.** Removes an unbounded stdout-buffering failure mode that's
invisible without explicit length validation. Forces the dumper to fail
loudly if anything goes wrong, instead of silently writing zero notes.
Generalizes: any tool whose output is unbounded should hand off via a file
plus a manifest, not stdout.

---

## 2026-05-03 — Two-layer architecture: dumpers + agent

**Context.** Personal task discovery requires reading multiple sources
(mail, notes, calendars). The instinct was a single agent that reads
sources and extracts tasks in one pass.

**Decision.** Split into two layers. Layer 1 (this repo): dumpers that
snapshot sources to flat files; no LLM calls; auditable. Layer 2 (planned):
agent that reads dumps, calls Claude, persists tasks to SQLite.

**Rationale.** Either layer can be replaced without touching the other.
Dumpers are deterministic and rerunnable — invaluable for prompt iteration
on the agent layer (don't re-fetch the world every time the prompt
changes). Plain files survive any choice of downstream surface (CLI, web
app, Obsidian, anything). The dumpers also have value standalone as a
local archive even if the agent layer never gets built.

---

## 2026-05-03 — Local Python + SQLite for the agent layer, not a web app

**Context.** Agent layer could live anywhere. Considered: local Python
script, Claude Code itself, web app on Vercel/Supabase (matching existing
side-project stack).

**Decision.** Local Python script reading flat files, writing to SQLite,
calling the Anthropic API for extraction. Daily-digest markdown plus an
on-demand CLI as the surfaces. No web UI, no persistent server.

**Rationale.** Data lives on the Mac and stays there — no exfiltration of
~16 GB of work mail or all personal notes to a third-party DB. No infra
fees. Easy upgrade to a web UI later if it earns the build cost; SQLite →
Postgres is a one-day refactor. Don't pay for architecture before the
prompt has earned it.

---

## 2026-05-03 — Microsoft Graph dumper abandoned for Apple Mail local cache

**Context.** Initial mail dumper (`dump_janelia_mail.py`) used Microsoft
Graph with device-code OAuth + delta query. Functionally correct, but the
OAuth flow timed out repeatedly during admin-consent collection on the
HHMI tenant. The user wasn't an Azure admin; IT escalation timeline was
uncertain against a 3-week access deadline.

**Decision.** Pivot to reading `~/Library/Mail/V10/` directly. Apple Mail
had been syncing the work account for years; ~16 GB of full MIME including
attachments was on disk, accessible with Full Disk Access for `python3`
(System Settings → Privacy & Security → Full Disk Access).

**Rationale.** The local cache turned out to be strictly more complete than
what Graph would have produced for this account. The Graph approach also
required tenant-level cooperation that wasn't coming. Trades portability
(only works on this Mac) for completeness, which is the right tradeoff
for a one-time pre-departure archive.

The Graph dumper was deleted entirely rather than kept as a stub. Anyone
needing a Graph-based incremental sync would need to redesign for their
own tenant constraints anyway.

---

## 2026-05-03 — `.emlx` parsing: trust the length prefix, ignore the trailing plist

**Context.** Apple's `.emlx` format is `<byte-length>\n<RFC 822
bytes><optional plist>`. The byte length refers to on-disk bytes (not
normalized); CRLF vs LF line endings vary per file. Some files include a
trailing Apple-specific metadata plist after the RFC 822 body.

**Decision.** Parse as `data[first_newline+1 : first_newline+1+length]`.
Don't normalize line endings. Discard the trailing plist.

**Rationale.** The length prefix is the authoritative slice point. Standard
email parsers (Python's `email.parser.BytesParser`) handle either line
ending. The trailing plist contains Apple-specific flags that diverge from
any other mail tool's view of the same message — preserving them would
make the output less portable, not more.

---

## 2026-05-03 — Partial messages: capture attachments even when bodies are missing

**Context.** Roughly 25% of `.emlx` files in the Apple Mail tree were
`.partial.emlx` — Apple Mail's marker for "headers downloaded, body
deferred." First version of the dumper skipped them. Investigation showed
that **every** external `Attachments/<msgId>/` directory in the source
corresponded to a partial message, not a fully-downloaded one. Apple Mail
downloads attachments on demand (when the user clicks them) without
backfilling the body. Skipping partials would have left 14.98 GB of
attachments behind.

**Decision.** Process partials. For each, write a stub `.eml` (headers
only) plus a sidecar `.json` with the attachment file list. Copy
attachment binaries to `mail-work/attachments/<safe_id>/`. When a later
run sees a fully-synced version of a previously-partial message,
overwrite the stub with the real body, flip `is_partial: false`, and
preserve the existing `attachments[]` list.

**Rationale.** A "partial" in Apple Mail isn't useless — it has headers
and often attachments. Skipping them would have lost ~15 GB of
irreplaceable content. The upgrade path means the system handles eventual
consistency cleanly: bodies arrive later via Mail re-sync, attachments are
preserved either way, downstream consumers can rely on the sidecar
schema being stable across the partial→full transition.

---

## 2026-05-03 — Smart Mailbox / forced-sync remediation for partial bodies

**Context.** With ~17,000 partials and a 3-week window before account
expiry, manually clicking each in Mail.app to force body download wasn't
feasible. The dumper warns about folders with >100 partials so the user
knows where to focus remediation.

**Decision.** Operational guidance, not code: change Apple Mail's account
setting to "Download attachments and bodies → Always" and use a Smart
Mailbox listing partials so Mail backfills bodies in an overnight sync.
The dumper's `upgraded` counter on the next run quantifies how many bodies
arrived.

**Rationale.** Don't fight Apple Mail's lazy-download default in code;
change the default at the source and let Mail do a bulk fetch. Cheaper
and less error-prone than implementing IMAP-fetch-by-id from scratch.
Implementing the fetch ourselves would also burden the dumper with
auth, rate limiting, and a third sync state machine to reason about.

---

## 2026-05-03 — Calendar URL rotation policy

**Context.** Google Calendar's "secret address in iCal format" URLs are
equivalent to read-only API keys: anyone with the URL can read the
calendar without auth, and they're not revocable except by rotation. The
URLs were pasted into chat history during initial setup; chat logs are a
leak surface.

**Decision.** Rotate via Google Calendar settings → Integrate calendar →
Reset → copy the new URL directly into `calendars.local.json`
(gitignored). Don't paste new URLs back into chat. If a paste slips,
rotate again.

**Rationale.** Defense by rotation is cheaper than defense by trying to
control where the URL ends up. The cost of rotation is a few clicks; the
cost of trying to redact a URL from a transcript later is unbounded.

---

## 2026-05-03 — `.local.json` convention for secret-bearing config

**Context.** Secrets (Azure IDs, OAuth client config, Apple Mail account
UUID, calendar URLs) need to be readable by the dumpers but absent from
version control.

**Decision.** All non-public config files use the suffix `.local.json`
(e.g., `apple_mail_config.local.json`, `calendars.local.json`). The repo
ships `.example.json` templates with placeholders. `.gitignore` includes
`*.local.json` as a glob plus belt-and-suspenders entries for specific
filenames.

**Rationale.** The naming itself signals intent — anyone editing or
reviewing a file knows from the name whether it's local-only. Defense in
depth alongside the gitignore. New secret-bearing files in the future are
covered automatically by the glob without touching `.gitignore`.

---

## 2026-05-03 — Python 3.9 → 3.12 migration deferred but tracked

**Context.** macOS ships system Python 3.9 by default; that's the binary
launchd jobs currently invoke (`/usr/bin/python3`). `google-auth` and
related deps print `FutureWarning` on import about 3.9 EOL. Functional
today, but a future dep release dropping 3.9 support would silently break
scheduled jobs.

**Decision.** Tracked in `docs/TODO.md`. Plan: `brew install python@3.12`,
point launchd plists at `/opt/homebrew/bin/python3.12`, re-grant Full
Disk Access to the new binary path.

**Rationale.** Don't migrate while building. The warning is non-fatal and
the migration touches macOS Privacy settings (a context-switch out of the
terminal). Keep it visible in `TODO.md` so it doesn't get lost; do it on
a calm day or when forced by a dep dropping 3.9 support.
