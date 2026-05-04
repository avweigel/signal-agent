#!/usr/bin/env python3
"""
Apple Notes dumper.

Runs the JXA script, parses the JSON output, converts each note body from HTML
to markdown, and writes one .md file per note to ~/agent-data/notes/.

Idempotent: skips notes whose content hash matches the last dump. Filenames are
stable (slug + short id hash) so re-runs overwrite the same file.

Usage:
    python3 dump_apple_notes.py
    python3 dump_apple_notes.py --output /custom/path
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import html2text

AGENT_DATA = Path(os.environ.get("AGENT_DATA", str(Path.home() / "workspace" / "agent-data")))
DEFAULT_OUTPUT = AGENT_DATA / "notes"
DEFAULT_STATE = AGENT_DATA / "state"
DEFAULT_LOG = AGENT_DATA / "logs" / "dump_apple_notes.log"
RAW_DUMP_PATH = AGENT_DATA / "state" / "notes-dump-raw.json"
COUNT_DROP_WARN_THRESHOLD = 0.10
MIN_DUMP_BYTES_IF_NOTES_EXIST = 1024

SCRIPT_DIR = Path(__file__).parent.resolve()
JXA_SCRIPT = SCRIPT_DIR / "dump_apple_notes.js"

INVALID_FILENAME = re.compile(r"[^\w\s-]")
WHITESPACE = re.compile(r"\s+")


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


def slugify(s: str, max_len: int = 60) -> str:
    s = s.strip() or "untitled"
    s = INVALID_FILENAME.sub("", s)
    s = WHITESPACE.sub("-", s).strip("-")
    return s[:max_len].lower() or "untitled"


def short_id(note_id: str) -> str:
    return hashlib.sha1(note_id.encode("utf-8")).hexdigest()[:8]


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def html_to_md(html: str) -> str:
    h = html2text.HTML2Text()
    h.body_width = 0  # don't wrap
    h.ignore_images = False
    h.ignore_links = False
    h.unicode_snob = True
    return h.handle(html or "").strip()


def run_jxa() -> list[dict]:
    """Invoke the JXA dumper, validate its output, return parsed notes.

    Validates aggressively to catch silent failures (the failure mode that
    motivated the file-based handoff in the first place).
    """
    if not JXA_SCRIPT.exists():
        raise FileNotFoundError(f"JXA script missing: {JXA_SCRIPT}")
    RAW_DUMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    if RAW_DUMP_PATH.exists():
        RAW_DUMP_PATH.unlink()

    result = subprocess.run(
        ["osascript", "-l", "JavaScript", str(JXA_SCRIPT), str(RAW_DUMP_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"osascript failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(
            "osascript produced no stdout. With file-based output this should "
            "not happen — investigate (permission, JXA crash, etc.)."
        )
    try:
        meta = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"could not parse JXA stdout as JSON: {e}; stdout was: {stdout[:500]!r}"
        )

    expected_path = Path(meta["path"])
    expected_bytes = int(meta["byteCount"])
    expected_notes = int(meta["noteCount"])
    available = int(meta.get("totalAvail", -1))
    err_count = int(meta.get("errCount", 0))

    if not expected_path.exists():
        raise RuntimeError(f"JXA reported success but file not found: {expected_path}")

    actual_bytes = expected_path.stat().st_size
    if actual_bytes != expected_bytes:
        raise RuntimeError(
            f"Byte-count mismatch: JXA reported {expected_bytes}, file is {actual_bytes}"
        )
    if available > 0 and expected_bytes < MIN_DUMP_BYTES_IF_NOTES_EXIST:
        raise RuntimeError(
            f"Suspiciously small dump: {expected_bytes} B for {available} available notes"
        )
    if expected_notes == 0 and available > 0:
        raise RuntimeError(
            f"JXA returned 0 notes but {available} are available — silent per-note failure"
        )

    notes = json.loads(expected_path.read_text(encoding="utf-8"))
    if len(notes) != expected_notes:
        raise RuntimeError(
            f"Parsed note count mismatch: JXA reported {expected_notes}, parsed {len(notes)}"
        )
    if err_count:
        logging.warning(
            "JXA skipped %d unreadable notes (encrypted/locked, etc.)", err_count
        )
    return notes


def previous_total(state_dir: Path) -> int | None:
    state_path = state_dir / "dump_apple_notes.json"
    if not state_path.exists():
        return None
    try:
        return int(json.loads(state_path.read_text(encoding="utf-8"))["total_notes"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def existing_hash(filepath: Path) -> str | None:
    """Read just enough of an existing file to find the hash frontmatter line."""
    if not filepath.exists():
        return None
    try:
        with filepath.open("r", encoding="utf-8") as f:
            for _ in range(20):  # frontmatter is short
                line = f.readline()
                if not line:
                    return None
                if line.startswith("hash:"):
                    return line.split(":", 1)[1].strip()
                if line.strip() == "---" and f.tell() > 4:
                    return None
    except OSError:
        return None
    return None


def write_note(out_dir: Path, note: dict) -> tuple[str, Path]:
    """
    Returns (status, path). status in {"new", "updated", "skipped"}.
    """
    name = note.get("name") or "untitled"
    note_id = note["id"]
    body_html = note.get("body") or ""
    folder = note.get("folder") or ""
    created = note.get("created") or ""
    modified = note.get("modified") or ""

    h = content_hash(body_html)
    filename = f"{slugify(name)}-{short_id(note_id)}.md"
    filepath = out_dir / filename

    prev = existing_hash(filepath)
    if prev == h:
        return ("skipped", filepath)

    body_md = html_to_md(body_html)
    # YAML frontmatter values must be quoted if they could contain colons.
    frontmatter = (
        "---\n"
        f"id: {json.dumps(note_id)}\n"
        f"title: {json.dumps(name)}\n"
        f"folder: {json.dumps(folder)}\n"
        f"created: {created}\n"
        f"modified: {modified}\n"
        f"hash: {h}\n"
        "---\n\n"
    )
    filepath.write_text(frontmatter + body_md + "\n", encoding="utf-8")
    return ("updated" if prev else "new", filepath)


def write_run_state(state_dir: Path, summary: dict) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "dump_apple_notes.json"
    state_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT, help="Output directory"
    )
    parser.add_argument(
        "--state", type=Path, default=DEFAULT_STATE, help="State directory"
    )
    parser.add_argument(
        "--log", type=Path, default=DEFAULT_LOG, help="Log file path"
    )
    args = parser.parse_args()

    setup_logging(args.log)
    args.output.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    logging.info("Starting Apple Notes dump to %s", args.output)

    try:
        notes = run_jxa()
    except Exception as e:
        logging.error("JXA dump failed: %s", e)
        return 1

    logging.info("JXA returned %d notes", len(notes))

    prev = previous_total(args.state)
    if prev is not None and prev > 0:
        drop = prev - len(notes)
        if drop > 0 and drop / prev > COUNT_DROP_WARN_THRESHOLD:
            logging.warning(
                "Note count dropped from %d to %d (%.0f%% drop). Investigate.",
                prev, len(notes), 100 * drop / prev,
            )

    counts = {"new": 0, "updated": 0, "skipped": 0}
    for note in notes:
        try:
            status, _ = write_note(args.output, note)
            counts[status] += 1
        except Exception as e:
            logging.error("Failed to write note %s: %s", note.get("id"), e)

    finished = datetime.now(timezone.utc)
    summary = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "total_notes": len(notes),
        "new": counts["new"],
        "updated": counts["updated"],
        "skipped": counts["skipped"],
    }
    write_run_state(args.state, summary)
    logging.info(
        "Done. new=%d updated=%d skipped=%d total=%d",
        counts["new"], counts["updated"], counts["skipped"], len(notes),
    )
    try:
        RAW_DUMP_PATH.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
