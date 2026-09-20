#!/usr/bin/env python3
"""
One-shot exploration script for the CellMap project-tracker Google Sheet.

Authenticates with `spreadsheets.readonly` against the same OAuth client used
for Gmail (gmail-oauth-client.json), but stores a *separate* token at
sheets-token.json so the existing Gmail token isn't disturbed.

Prerequisites (one-time):
  1. In Google Cloud Console > project `aubrey-mail-archive`, enable the
     "Google Sheets API". https://console.cloud.google.com/apis/library/sheets.googleapis.com
  2. Run this script once. A browser window will open for OAuth consent on
     the personal Gmail account. Click through; token is cached at
     ~/workspace/agent-data/state/sheets-token.json (mode 0600).

Output: tab inventory + headers + first 3 rows of the configured tab.
Once we see the column shape, we'll build dump_cellmap_tracker.py against it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

AGENT_DATA = Path(os.environ.get("AGENT_DATA", str(Path.home() / "workspace" / "agent-data")))
STATE_DIR = AGENT_DATA / "state"
CLIENT_SECRETS_PATH = STATE_DIR / "gmail-oauth-client.json"
TOKEN_PATH = STATE_DIR / "sheets-token.json"

SHEET_ID = "1A9SEwaSSES2rYZhOl_LkhG8ZLCUqz3vu9_JKaqCSW3M"
TARGET_GID = 1489512252


def load_credentials() -> Credentials:
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
        except Exception:
            pass
    if not CLIENT_SECRETS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {CLIENT_SECRETS_PATH}. Place the OAuth client secret JSON "
            "from Google Cloud Console there (mode 0600)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)
    save_credentials(creds)
    return creds


def save_credentials(creds: Credentials) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def main() -> int:
    creds = load_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    print(f"Spreadsheet: {meta.get('properties', {}).get('title')}")
    print()
    print("Tabs:")
    target_tab = None
    for s in meta.get("sheets", []):
        p = s["properties"]
        marker = "  <-- TARGET" if p["sheetId"] == TARGET_GID else ""
        rows = p.get("gridProperties", {}).get("rowCount")
        cols = p.get("gridProperties", {}).get("columnCount")
        print(f"  gid={p['sheetId']:<12} title={p['title']!r:<40} {rows}r x {cols}c{marker}")
        if p["sheetId"] == TARGET_GID:
            target_tab = p["title"]

    if not target_tab:
        print(f"\nERROR: gid {TARGET_GID} not found.", file=sys.stderr)
        return 1

    print()
    print(f"Reading tab {target_tab!r} (gid={TARGET_GID})")
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID,
        range=f"'{target_tab}'!A1:ZZ4",
        valueRenderOption="FORMATTED_VALUE",
        dateTimeRenderOption="FORMATTED_STRING",
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        print("(empty)")
        return 0

    header = rows[0]
    print()
    print(f"Headers ({len(header)} columns):")
    for i, h in enumerate(header):
        print(f"  [{i}] {h!r}")
    print()
    print("First 3 data rows:")
    for r in rows[1:4]:
        print(f"  {json.dumps(r, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
