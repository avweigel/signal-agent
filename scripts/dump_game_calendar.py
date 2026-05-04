#!/usr/bin/env python3
"""
Game calendar dumper.

Pulls iCal feeds for each configured calendar and writes them as .ics files to
~/agent-data/calendar-game/. Also extracts each VEVENT into a per-event .json
file for easier downstream consumption by the agent.

Calendars are configured in scripts/calendars.local.json (gitignored). Copy
scripts/calendars.example.json and fill in the secret iCal URL ("Secret
address in iCal format") from each calendar's Google Calendar settings.
The dumper works without OAuth. Anyone with that URL can read the calendar,
so the file lives outside version control.

Usage:
    python3 dump_game_calendar.py
    python3 dump_game_calendar.py --config /custom/calendars.local.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

AGENT_DATA = Path(os.environ.get("AGENT_DATA", str(Path.home() / "workspace" / "agent-data")))
DEFAULT_OUTPUT = AGENT_DATA / "calendar-game"
DEFAULT_STATE = AGENT_DATA / "state"
DEFAULT_LOG = AGENT_DATA / "logs" / "dump_game_calendar.log"
DEFAULT_CONFIG = Path(__file__).parent / "calendars.local.json"

VEVENT_RE = re.compile(r"BEGIN:VEVENT.*?END:VEVENT", re.DOTALL)


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_config(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"No calendar config at {path}. Create one with this shape:\n"
            '[\n  {"name": "kingdom-23", "ical_url": "https://..."},\n'
            '  {"name": "leadership", "ical_url": "https://..."}\n]'
        )
    return json.loads(path.read_text(encoding="utf-8"))


def parse_vevent(block: str) -> dict:
    """Extract common fields from a VEVENT block. Best-effort, no full parse."""
    fields = {}
    # Lines may be folded with leading whitespace; unfold first.
    unfolded = re.sub(r"\r?\n[ \t]", "", block)
    for line in unfolded.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        # strip parameters like "DTSTART;TZID=America/New_York"
        base_key = key.split(";", 1)[0].strip()
        if base_key in {"UID", "SUMMARY", "DESCRIPTION", "LOCATION",
                        "DTSTART", "DTEND", "STATUS", "ORGANIZER",
                        "LAST-MODIFIED", "CREATED", "RRULE"}:
            fields[base_key] = value.strip()
    return fields


def fetch_calendar(name: str, ical_url: str, output: Path) -> dict:
    logging.info("Fetching calendar %s", name)
    resp = requests.get(ical_url, timeout=30)
    resp.raise_for_status()
    raw = resp.text

    cal_dir = output / name
    cal_dir.mkdir(parents=True, exist_ok=True)
    events_dir = cal_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    # Write the full ics file too, so we have raw source.
    ics_path = cal_dir / f"{name}.ics"
    ics_path.write_text(raw, encoding="utf-8")

    seen_uids = set()
    new_count = 0
    updated_count = 0
    skipped_count = 0

    for match in VEVENT_RE.finditer(raw):
        block = match.group(0)
        fields = parse_vevent(block)
        uid = fields.get("UID")
        if not uid:
            continue
        seen_uids.add(uid)

        event_hash = hashlib.sha256(block.encode("utf-8")).hexdigest()[:16]
        # Filename: short hash of UID for filesystem safety.
        fname = hashlib.sha1(uid.encode("utf-8")).hexdigest()[:16] + ".json"
        fpath = events_dir / fname

        record = {
            "uid": uid,
            "calendar": name,
            "summary": fields.get("SUMMARY", ""),
            "description": fields.get("DESCRIPTION", ""),
            "location": fields.get("LOCATION", ""),
            "dtstart": fields.get("DTSTART", ""),
            "dtend": fields.get("DTEND", ""),
            "status": fields.get("STATUS", ""),
            "rrule": fields.get("RRULE", ""),
            "last_modified": fields.get("LAST-MODIFIED", ""),
            "raw_hash": event_hash,
            "raw": block,
        }

        if fpath.exists():
            try:
                prev = json.loads(fpath.read_text(encoding="utf-8"))
                if prev.get("raw_hash") == event_hash:
                    skipped_count += 1
                    continue
                updated_count += 1
            except Exception:
                updated_count += 1
        else:
            new_count += 1

        fpath.write_text(json.dumps(record, indent=2), encoding="utf-8")

    # Optional: garbage collect events that no longer exist in the feed.
    removed_count = 0
    for existing in events_dir.glob("*.json"):
        try:
            data = json.loads(existing.read_text(encoding="utf-8"))
            if data.get("uid") not in seen_uids:
                existing.unlink()
                removed_count += 1
        except Exception:
            pass

    return {
        "calendar": name,
        "fetched_events": len(seen_uids),
        "new": new_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "removed": removed_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    args = parser.parse_args()

    setup_logging(args.log)
    args.output.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    logging.info("Starting calendar dump")

    try:
        calendars = load_config(args.config)
    except Exception as e:
        logging.error("Config error: %s", e)
        return 1

    results = []
    for cal in calendars:
        try:
            r = fetch_calendar(cal["name"], cal["ical_url"], args.output)
            results.append(r)
            logging.info(
                "  %s: total=%d new=%d updated=%d skipped=%d removed=%d",
                r["calendar"], r["fetched_events"], r["new"],
                r["updated"], r["skipped"], r["removed"],
            )
        except Exception as e:
            logging.error("Failed calendar %s: %s", cal.get("name"), e)
            results.append({"calendar": cal.get("name"), "error": str(e)})

    finished = datetime.now(timezone.utc)
    summary = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "calendars": results,
    }
    args.state.mkdir(parents=True, exist_ok=True)
    (args.state / "dump_game_calendar.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    logging.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
