#!/usr/bin/env python3
"""
Personal Gmail dumper via the Gmail API.

Mirrors dump_apple_mail.py's structure, but for a personal Google account
rather than a local Apple Mail cache. OAuth via google-auth-oauthlib's
InstalledAppFlow on first run; afterward uses the cached refresh token.

Outputs to ~/workspace/agent-data/mail-personal/<year>/<month>/<safeid>.eml
plus matching .json sidecars. Idempotent: skips existing files.

Incremental sync via Gmail's History API. The first run has no cursor and
does a full pull via users.messages.list; subsequent runs only fetch
changes since the stored historyId. If the cursor is too old (>30 days),
falls back to a full pull automatically.

First run is interactive (opens a browser for OAuth consent). Subsequent
runs are non-interactive and can be scheduled via launchd. Pass
--non-interactive to fail fast instead of attempting the browser flow.

Usage:
    python3 dump_gmail.py
    python3 dump_gmail.py --limit 5             # smoke test
    python3 dump_gmail.py --reset-history       # discard cursor, full pull
    python3 dump_gmail.py --non-interactive     # fail rather than open browser
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

AGENT_DATA = Path(os.environ.get("AGENT_DATA", str(Path.home() / "workspace" / "agent-data")))
DEFAULT_OUTPUT = AGENT_DATA / "mail-personal"
DEFAULT_STATE = AGENT_DATA / "state"
DEFAULT_LOG = AGENT_DATA / "logs" / "dump_gmail.log"
CLIENT_SECRETS_PATH = DEFAULT_STATE / "gmail-oauth-client.json"
TOKEN_PATH = DEFAULT_STATE / "gmail-token.json"
HISTORY_ID_PATH = DEFAULT_STATE / "gmail-history-id.txt"

COUNT_DROP_WARN_THRESHOLD = 0.10


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )


def save_credentials(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def load_credentials(non_interactive: bool = False) -> Credentials:
    """Load cached creds, refresh if needed, otherwise InstalledAppFlow.

    With non_interactive=True, raises rather than opening a browser, so
    launchd-scheduled runs fail fast instead of hanging.
    """
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
            return creds
        except Exception as e:
            if non_interactive:
                raise RuntimeError(
                    f"Token refresh failed and --non-interactive set: {e}. "
                    "Re-run interactively to re-seed the token cache."
                ) from e
            # fall through to interactive flow

    if non_interactive:
        raise RuntimeError(
            "OAuth interactive flow needed (no valid token cache) but "
            "--non-interactive was set. Run once without --non-interactive "
            f"to seed {TOKEN_PATH}."
        )

    if not CLIENT_SECRETS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CLIENT_SECRETS_PATH}. Place the OAuth client secret JSON "
            "from Google Cloud Console there (mode 0600)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds


def safe_id(message_id_header: str | None, gmail_id: str) -> str:
    """Stable filename component. Prefer RFC 822 Message-ID, fall back to Gmail id."""
    if message_id_header:
        mid = message_id_header.strip().lstrip("<").rstrip(">").lower()
        return hashlib.sha1(mid.encode("utf-8")).hexdigest()[:16]
    return "gid" + hashlib.sha1(gmail_id.encode("utf-8")).hexdigest()[:13]


def parse_received_dt(parsed) -> datetime:
    """Best-effort timestamp for path bucketing. 1970 sentinel = unknown."""
    raw = parsed.get("Date")
    if raw:
        try:
            dt = email.utils.parsedate_to_datetime(str(raw))
            if dt:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def message_dest(output: Path, dt: datetime, sid: str) -> Path:
    if dt.year == 1970:
        folder = output / "unknown-date"
    else:
        folder = output / f"{dt.year:04d}" / f"{dt.month:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{sid}.eml"


def has_attachments_sniff(rfc822: bytes) -> bool:
    return (
        b"\nContent-Disposition: attachment" in rfc822
        or b"\nContent-Type: multipart/mixed" in rfc822
    )


def _hdr(parsed, name: str) -> str:
    """Get a header as a plain str. email.parser sometimes returns
    email.header.Header objects (for RFC 2047 encoded-word headers) which
    aren't JSON-serializable; str() invokes their decode-to-string path."""
    v = parsed.get(name)
    return "" if v is None else str(v)


def fetch_message(service, gmail_id: str) -> dict:
    """Single API call: returns metadata + raw RFC 822 (base64url)."""
    return service.users().messages().get(
        userId="me", id=gmail_id, format="raw",
    ).execute()


def write_message(output: Path, msg: dict) -> str:
    """Returns: new | skipped_existing | error."""
    gmail_id = msg["id"]
    raw_b64 = msg.get("raw")
    if not raw_b64:
        logging.error("Message %s missing raw content", gmail_id)
        return "error"
    try:
        rfc822 = base64.urlsafe_b64decode(raw_b64.encode("ascii"))
    except Exception as e:
        logging.error("Failed to decode raw for %s: %s", gmail_id, e)
        return "error"

    parsed = BytesParser().parsebytes(rfc822, headersonly=True)
    message_id_header = _hdr(parsed, "Message-ID") or _hdr(parsed, "Message-Id")
    sid = safe_id(message_id_header or None, gmail_id)
    dt = parse_received_dt(parsed)
    eml_path = message_dest(output, dt, sid)
    json_path = eml_path.with_suffix(".json")

    if eml_path.exists() and json_path.exists():
        return "skipped_existing"

    eml_path.write_bytes(rfc822)
    label_ids = msg.get("labelIds", [])
    metadata = {
        "gmail_id": gmail_id,
        "thread_id": msg.get("threadId"),
        "message_id": message_id_header.strip(),
        "subject": _hdr(parsed, "Subject"),
        "from": _hdr(parsed, "From"),
        "to": _hdr(parsed, "To"),
        "cc": _hdr(parsed, "Cc"),
        "date": _hdr(parsed, "Date"),
        "snippet": msg.get("snippet", ""),
        "label_ids": label_ids,
        "is_unread": "UNREAD" in label_ids,
        "has_attachments": has_attachments_sniff(rfc822),
    }
    json_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return "new"


def list_all_message_ids(service) -> list[str]:
    """Full enumeration via users.messages.list."""
    ids = []
    request = service.users().messages().list(userId="me", maxResults=500)
    page = 0
    while request is not None:
        page += 1
        resp = request.execute()
        for m in resp.get("messages", []):
            ids.append(m["id"])
        if page % 10 == 0:
            logging.info("Listed %d messages so far", len(ids))
        request = service.users().messages().list_next(request, resp)
    return ids


def list_added_message_ids_since(service, history_id: str) -> tuple[list[str], str | None]:
    """Returns (new_ids, latest_history_id)."""
    ids: list[str] = []
    latest: str | None = None
    request = service.users().history().list(
        userId="me", startHistoryId=history_id,
        historyTypes=["messageAdded"],
    )
    while request is not None:
        resp = request.execute()
        if "historyId" in resp:
            latest = resp["historyId"]
        for h in resp.get("history", []):
            for entry in h.get("messagesAdded", []):
                m = entry.get("message", {})
                if m.get("id"):
                    ids.append(m["id"])
        request = service.users().history().list_next(request, resp)
    return ids, latest


def get_current_history_id(service) -> str | None:
    """Get current historyId — used as cursor after a full pull."""
    try:
        profile = service.users().getProfile(userId="me").execute()
        return profile.get("historyId")
    except HttpError as e:
        logging.warning("getProfile failed: %s", e)
        return None


def load_history_cursor() -> str | None:
    if HISTORY_ID_PATH.exists():
        v = HISTORY_ID_PATH.read_text(encoding="utf-8").strip()
        return v or None
    return None


def save_history_cursor(history_id: str) -> None:
    HISTORY_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_ID_PATH.write_text(history_id, encoding="utf-8")


def previous_total(state_dir: Path) -> int | None:
    """Used for full-pull drift check only."""
    state_path = state_dir / "dump_gmail.json"
    if not state_path.exists():
        return None
    try:
        prev = json.loads(state_path.read_text(encoding="utf-8"))
        if not prev.get("full_pull"):
            return None
        return int(prev.get("totals", {}).get("found", 0))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--reset-history", action="store_true",
                        help="Discard cursor and do a full pull")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Fail fast instead of opening a browser for OAuth "
                             "(safe for launchd-scheduled runs)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N message IDs (smoke test)")
    args = parser.parse_args()

    setup_logging(args.log)
    args.output.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    logging.info("Starting Gmail dump")

    try:
        creds = load_credentials(non_interactive=args.non_interactive)
    except Exception as e:
        logging.error("Auth failed: %s", e)
        return 1
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    cursor = None if args.reset_history else load_history_cursor()
    full_pull = cursor is None
    new_cursor: str | None = None

    if full_pull:
        logging.info("No cursor or --reset-history — performing full pull")
        try:
            ids = list_all_message_ids(service)
        except HttpError as e:
            logging.error("Full-pull list failed: %s", e)
            return 1
        new_cursor = get_current_history_id(service)
    else:
        logging.info("Resuming from historyId=%s", cursor)
        try:
            ids, new_cursor = list_added_message_ids_since(service, cursor)
        except HttpError as e:
            if e.resp.status == 404:
                logging.warning("History cursor too old; falling back to full pull")
                full_pull = True
                try:
                    ids = list_all_message_ids(service)
                except HttpError as ee:
                    logging.error("Full-pull list failed: %s", ee)
                    return 1
                new_cursor = get_current_history_id(service)
            else:
                logging.error("History list failed: %s", e)
                return 1
        if new_cursor is None:
            new_cursor = cursor

    logging.info("Found %d candidate messages", len(ids))

    if args.limit is not None:
        ids = ids[: args.limit]
        logging.info("Limiting to first %d messages (--limit)", len(ids))

    counts = {"found": len(ids), "new": 0, "skipped": 0, "errors": 0}
    for i, gid in enumerate(ids, 1):
        try:
            msg = fetch_message(service, gid)
        except HttpError as e:
            logging.error("Fetch failed for %s: %s", gid, e)
            counts["errors"] += 1
            continue
        status = write_message(args.output, msg)
        if status == "new":
            counts["new"] += 1
        elif status == "skipped_existing":
            counts["skipped"] += 1
        elif status == "error":
            counts["errors"] += 1
        if i % 500 == 0:
            logging.info("Processed %d/%d (new=%d skipped=%d errors=%d)",
                         i, len(ids), counts["new"], counts["skipped"], counts["errors"])

    if new_cursor and args.limit is None:
        # Don't advance the cursor on a smoke-test run; otherwise the next
        # full run would skip everything between historyIds.
        save_history_cursor(str(new_cursor))
        logging.info("Saved history cursor: %s", new_cursor)
    elif args.limit is not None:
        logging.info("Skipping cursor save (--limit was set)")

    if full_pull and args.limit is None:
        prev = previous_total(args.state)
        if prev is not None and prev > 0:
            drop = prev - counts["found"]
            if drop > 0 and drop / prev > COUNT_DROP_WARN_THRESHOLD:
                logging.warning(
                    "Found-count dropped from %d to %d (%.0f%% drop). Investigate.",
                    prev, counts["found"], 100 * drop / prev,
                )

    finished = datetime.now(timezone.utc)
    summary = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "full_pull": full_pull,
        "limited": args.limit is not None,
        "history_id": new_cursor,
        "totals": counts,
    }
    args.state.mkdir(parents=True, exist_ok=True)
    (args.state / "dump_gmail.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    logging.info(
        "Done. found=%d new=%d skipped=%d errors=%d full_pull=%s",
        counts["found"], counts["new"], counts["skipped"], counts["errors"], full_pull,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
