#!/usr/bin/env python3
"""
Apple Mail (.emlx) dumper.

Walks ~/Library/Mail/V10/<account_uuid>/ and converts each .emlx file to a
clean RFC 822 .eml under ~/workspace/agent-data/mail-work/<year>/<month>/.

Idempotent. Keyed on Message-ID — when the same message appears in multiple
mailbox folders, only one .eml is written but its sidecar's `folders` array
records every source folder.

Partial messages (.partial.emlx) are processed too: a stub .eml is written
with whatever Apple gave us (typically headers only), AND any external
attachments from the sibling Attachments/<msgId>/ directory are copied to
mail-work/attachments/<safe_id>/ to preserve attachment binaries that
are missing from the .eml's MIME content.

When a later run encounters a fully-synced .emlx for a message previously
seen as partial, the stub .eml is overwritten with the full body and the
sidecar's is_partial flag flips to false; attachments[] is preserved.

Usage:
    python3 dump_apple_mail.py
    python3 dump_apple_mail.py --skip-partials
    python3 dump_apple_mail.py --include-sync-issues
    python3 dump_apple_mail.py --include-trash
    python3 dump_apple_mail.py --account-uuid OTHER-UUID
"""

from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import logging
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.parser import BytesParser
from pathlib import Path

MAIL_ROOT = Path.home() / "Library" / "Mail" / "V10"
ACCOUNT_CONFIG_PATH = Path(__file__).parent / "apple_mail_config.local.json"

AGENT_DATA = Path(os.environ.get("AGENT_DATA", str(Path.home() / "workspace" / "agent-data")))
DEFAULT_OUTPUT = AGENT_DATA / "mail-work"
DEFAULT_STATE = AGENT_DATA / "state"
DEFAULT_LOG = AGENT_DATA / "logs" / "dump_apple_mail.log"

TRASH_FOLDERS = {"Deleted Items", "Junk", "Trash", "Spam"}
SYNC_FOLDERS = {"Sync Issues"}
PARTIAL_WARN_THRESHOLD = 100
COUNT_DROP_WARN_THRESHOLD = 0.10


def setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )


def parse_emlx(data: bytes) -> bytes:
    """Strip Apple's length-prefix line and trailing plist; return RFC 822."""
    nl = data.find(b"\n")
    if nl == -1:
        raise ValueError("no newline in .emlx — malformed")
    try:
        length = int(data[:nl].strip())
    except ValueError:
        raise ValueError(f"first line is not a length: {data[:nl][:40]!r}")
    body = data[nl + 1 : nl + 1 + length]
    if len(body) != length:
        raise ValueError(
            f"length mismatch: header says {length}, only {len(body)} bytes available"
        )
    return body


def safe_id(message_id: str | None, source_path: Path) -> str:
    """Stable filename component. Prefer Message-ID, fall back to path hash."""
    if message_id:
        mid = message_id.strip().lstrip("<").rstrip(">").lower()
        return hashlib.sha1(mid.encode("utf-8")).hexdigest()[:16]
    return "pp" + hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:14]


def parse_received_dt(msg) -> datetime:
    """Best-effort timestamp for path bucketing. 1970 sentinel = unknown."""
    raw = msg.get("Date")
    if raw:
        try:
            dt = email.utils.parsedate_to_datetime(raw)
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


def read_sidecar(json_path: Path) -> dict:
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def merge_into_sidecar(
    json_path: Path,
    source_folder: str,
    new_attachments: list,
    is_now_full: bool,
    refreshed_headers: dict | None = None,
) -> bool:
    """Update existing sidecar: add folder, dedup-merge attachments,
    optionally promote partial->full and refresh headers.
    Returns True if source_folder was newly added."""
    meta = read_sidecar(json_path)
    if not meta:
        return False

    folders = meta.get("folders") or []
    folder_added = source_folder not in folders
    if folder_added:
        folders.append(source_folder)
    meta["folders"] = folders

    existing = meta.get("attachments") or []
    seen_rels = {a.get("rel_path") for a in existing if isinstance(a, dict)}
    for a in new_attachments:
        if a.get("rel_path") not in seen_rels:
            existing.append(a)
            seen_rels.add(a.get("rel_path"))
    meta["attachments"] = existing

    if is_now_full:
        meta["is_partial"] = False
        if refreshed_headers is not None:
            for hdr in ("subject", "from", "to", "cc", "date"):
                v = refreshed_headers.get(hdr)
                if v:
                    meta[hdr] = v

    meta["has_attachments"] = bool(existing) or meta.get("has_attachments", False)
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return folder_added


def has_attachments(rfc822: bytes) -> bool:
    """Cheap header sniff. Avoids walking the full multipart tree."""
    return (
        b"\nContent-Disposition: attachment" in rfc822
        or b"\nContent-Type: multipart/mixed" in rfc822
    )


def find_attachments_dir(emlx_path: Path) -> Path | None:
    """Locate the sibling Attachments/<msgId>/ directory for an .emlx file."""
    msgid = emlx_path.stem  # 'foo.emlx' -> 'foo'; 'foo.partial.emlx' -> 'foo.partial'
    if msgid.endswith(".partial"):
        msgid = msgid[: -len(".partial")]
    attach_root = emlx_path.parent.parent / "Attachments" / msgid
    return attach_root if attach_root.is_dir() else None


def copy_attachments(attach_dir: Path, dest_dir: Path) -> list[dict]:
    """Mirror attach_dir into dest_dir, idempotent on size match.
    Returns list of {part, filename, size_bytes, rel_path}."""
    out = []
    for src in attach_dir.rglob("*"):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(attach_dir)
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_size = src.stat().st_size
            if not dest.exists() or dest.stat().st_size != src_size:
                shutil.copy2(src, dest)
            out.append({
                "part": rel.parts[0] if len(rel.parts) > 1 else "",
                "filename": rel.parts[-1],
                "size_bytes": src_size,
                "rel_path": str(rel),
            })
        except OSError as e:
            logging.error("Attachment copy failed %s: %s", src, e)
    return out


def write_message(
    raw: bytes,
    source_path: Path,
    source_folder: str,
    output: Path,
    is_partial_file: bool,
    seen_message_ids: dict[str, Path],
) -> tuple[str, int, int]:
    """Returns (status, n_new_attachments, n_new_attachment_bytes).

    Statuses: new | new_partial | upgraded | dup_in_run | dup_existing |
    skipped_existing | error.
    """
    try:
        rfc822 = parse_emlx(raw)
    except ValueError as e:
        logging.error("Failed to parse %s: %s", source_path, e)
        return ("error", 0, 0)

    msg = BytesParser().parsebytes(rfc822, headersonly=True)
    message_id = msg.get("Message-ID") or msg.get("Message-Id")
    sid = safe_id(message_id, source_path)
    dt = parse_received_dt(msg)
    eml_path = message_dest(output, dt, sid)
    json_path = eml_path.with_suffix(".json")

    new_attachments: list[dict] = []
    if is_partial_file:
        attach_dir = find_attachments_dir(source_path)
        if attach_dir:
            new_attachments = copy_attachments(attach_dir, output / "attachments" / sid)
    n_new = len(new_attachments)
    n_bytes = sum(a.get("size_bytes", 0) for a in new_attachments)

    refreshed = {
        "subject": msg.get("Subject") or "",
        "from": msg.get("From") or "",
        "to": msg.get("To") or "",
        "cc": msg.get("Cc") or "",
        "date": msg.get("Date") or "",
    }

    if message_id and sid in seen_message_ids:
        merge_into_sidecar(
            seen_message_ids[sid].with_suffix(".json"),
            source_folder, new_attachments,
            is_now_full=not is_partial_file,
            refreshed_headers=refreshed if not is_partial_file else None,
        )
        return ("dup_in_run", n_new, n_bytes)

    if eml_path.exists() and json_path.exists():
        prev = read_sidecar(json_path)
        prev_partial = bool(prev.get("is_partial", False))
        if prev_partial and not is_partial_file:
            eml_path.write_bytes(rfc822)
            merge_into_sidecar(json_path, source_folder, new_attachments,
                               is_now_full=True, refreshed_headers=refreshed)
            return ("upgraded", n_new, n_bytes)
        added = merge_into_sidecar(json_path, source_folder, new_attachments,
                                   is_now_full=not is_partial_file)
        return ("dup_existing" if added else "skipped_existing", n_new, n_bytes)

    eml_path.write_bytes(rfc822)
    metadata = {
        "message_id": (message_id or "").strip(),
        **refreshed,
        "has_attachments": has_attachments(rfc822) or bool(new_attachments),
        "is_partial": is_partial_file,
        "folders": [source_folder],
        "source_path": str(source_path),
        "attachments": new_attachments,
    }
    json_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if message_id:
        seen_message_ids[sid] = eml_path
    return ("new_partial" if is_partial_file else "new", n_new, n_bytes)


def is_partial(path: Path) -> bool:
    return path.name.endswith(".partial.emlx")


def folder_name_from_path(emlx_path: Path, account_root: Path) -> str:
    """Extract human-readable mailbox folder name(s) from path.

    Handles nested mailboxes (Folder.mbox/Sub.mbox/...) by joining with '/'.
    """
    parts = emlx_path.relative_to(account_root).parts
    folder_parts = []
    for p in parts:
        if p.endswith(".mbox"):
            folder_parts.append(p[:-5])
        else:
            break
    return "/".join(folder_parts) if folder_parts else "(unknown)"


def previous_total(state_dir: Path) -> int | None:
    """Source-side message count = found - skipped_folder - partial_skipped
    - errors. Stable across runs even when most messages already exist on
    disk and skip the write path."""
    state_path = state_dir / "dump_apple_mail.json"
    if not state_path.exists():
        return None
    try:
        t = json.loads(state_path.read_text(encoding="utf-8")).get("totals", {})
        return (int(t.get("found", 0))
                - int(t.get("skipped_folder", 0))
                - int(t.get("partial_skipped", 0))
                - int(t.get("errors", 0)))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def load_default_account_uuid() -> str | None:
    """Read account_uuid from apple_mail_config.local.json if present."""
    if not ACCOUNT_CONFIG_PATH.exists():
        return None
    try:
        return json.loads(ACCOUNT_CONFIG_PATH.read_text(encoding="utf-8")).get("account_uuid")
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-uuid", default=None,
                        help="Apple Mail account UUID (find via "
                             "`ls ~/Library/Mail/V10/`). Defaults to value in "
                             "scripts/apple_mail_config.local.json.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--skip-partials", action="store_true",
                        help="Skip .partial.emlx files entirely (default: process them, "
                             "writing a stub .eml + capturing external attachments)")
    parser.add_argument("--include-trash", action="store_true",
                        help="Include Deleted Items / Junk / Trash / Spam folders")
    parser.add_argument("--include-sync-issues", action="store_true",
                        help="Include Sync Issues folder (sometimes holds real send failures)")
    args = parser.parse_args()

    setup_logging(args.log)
    args.output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc)

    account_uuid = args.account_uuid or load_default_account_uuid()
    if not account_uuid:
        logging.error(
            "No account UUID provided. Pass --account-uuid <UUID> or create "
            "scripts/apple_mail_config.local.json (see "
            "apple_mail_config.example.json)."
        )
        return 1

    account_root = MAIL_ROOT / account_uuid
    if not account_root.is_dir():
        logging.error("Account root not found: %s", account_root)
        return 1
    logging.info("Walking %s", account_root)

    all_emlx = list(account_root.rglob("*.emlx"))
    if not all_emlx:
        logging.error(
            "No .emlx files found under %s — Full Disk Access revoked, "
            "or wrong account UUID.", account_root,
        )
        return 1
    logging.info("Discovered %d .emlx files", len(all_emlx))

    skipped_folders = set(TRASH_FOLDERS) if not args.include_trash else set()
    if not args.include_sync_issues:
        skipped_folders |= SYNC_FOLDERS

    per_folder = defaultdict(lambda: {
        "found": 0, "written": 0, "written_partial": 0, "upgraded": 0,
        "duplicates": 0, "partial_skipped": 0, "skipped_folder": 0,
        "attachments_copied": 0, "attachment_bytes": 0, "errors": 0,
    })
    totals = {"found": 0, "written": 0, "written_partial": 0, "upgraded": 0,
              "duplicates": 0, "partial_skipped": 0, "skipped_folder": 0,
              "attachments_copied": 0, "attachment_bytes": 0, "errors": 0}
    seen_message_ids: dict[str, Path] = {}

    for emlx in all_emlx:
        folder = folder_name_from_path(emlx, account_root)
        leaf = folder.split("/")[-1]
        per_folder[folder]["found"] += 1
        totals["found"] += 1

        if leaf in skipped_folders:
            per_folder[folder]["skipped_folder"] += 1
            totals["skipped_folder"] += 1
            continue

        is_part = is_partial(emlx)
        if is_part and args.skip_partials:
            per_folder[folder]["partial_skipped"] += 1
            totals["partial_skipped"] += 1
            continue

        try:
            raw = emlx.read_bytes()
        except OSError as e:
            logging.error("Read failed %s: %s", emlx, e)
            per_folder[folder]["errors"] += 1
            totals["errors"] += 1
            continue

        status, n_new, n_bytes = write_message(
            raw, emlx, folder, args.output, is_part, seen_message_ids,
        )
        per_folder[folder]["attachments_copied"] += n_new
        per_folder[folder]["attachment_bytes"] += n_bytes
        totals["attachments_copied"] += n_new
        totals["attachment_bytes"] += n_bytes
        if status == "new":
            per_folder[folder]["written"] += 1
            totals["written"] += 1
        elif status == "new_partial":
            per_folder[folder]["written_partial"] += 1
            totals["written_partial"] += 1
        elif status == "upgraded":
            per_folder[folder]["upgraded"] += 1
            totals["upgraded"] += 1
        elif status in ("dup_in_run", "dup_existing"):
            per_folder[folder]["duplicates"] += 1
            totals["duplicates"] += 1
        elif status == "error":
            per_folder[folder]["errors"] += 1
            totals["errors"] += 1
        # skipped_existing: silently counted on the prior run

    high_partial_folders = sorted(
        ((f, c["written_partial"]) for f, c in per_folder.items()
         if c["written_partial"] >= PARTIAL_WARN_THRESHOLD),
        key=lambda x: -x[1],
    )
    for f, n in high_partial_folders:
        logging.warning(
            "Folder %r has %d body-missing messages — open it in Apple Mail "
            "to force body sync before access to the source mail account expires. Their attachments "
            "were captured locally already.", f, n,
        )

    prev = previous_total(args.state)
    # Compare on "messages encountered minus deliberately ignored" — stable
    # across runs even when most messages skip the write path due to existing
    # output. Catches genuine source-side data loss (FDA revoked, mailbox
    # deleted, etc.) without false-alarming on incremental no-op runs.
    current = (totals["found"] - totals["skipped_folder"]
               - totals["partial_skipped"] - totals["errors"])
    if prev is not None and prev > 0:
        drop = prev - current
        if drop > 0 and drop / prev > COUNT_DROP_WARN_THRESHOLD:
            logging.warning(
                "Source-message count dropped from %d to %d (%.0f%% drop). Investigate.",
                prev, current, 100 * drop / prev,
            )

    finished = datetime.now(timezone.utc)
    summary = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "duration_seconds": (finished - started).total_seconds(),
        "account_uuid": account_uuid,
        "totals": totals,
        "per_folder": {f: dict(c) for f, c in per_folder.items()},
        "high_partial_folders": [{"folder": f, "partial": n} for f, n in high_partial_folders],
    }
    args.state.mkdir(parents=True, exist_ok=True)
    (args.state / "dump_apple_mail.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    logging.info(
        "Done. found=%d written=%d written_partial=%d upgraded=%d duplicates=%d "
        "partial_skipped=%d folder_skipped=%d errors=%d attachments=%d (%.2f GB)",
        totals["found"], totals["written"], totals["written_partial"],
        totals["upgraded"], totals["duplicates"], totals["partial_skipped"],
        totals["skipped_folder"], totals["errors"], totals["attachments_copied"],
        totals["attachment_bytes"] / 1024 / 1024 / 1024,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
