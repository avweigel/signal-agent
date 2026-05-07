#!/usr/bin/env python3
"""
One-time cleanup pass over the existing entities table.

Finds short-form / long-form pairs in the same entity_type where the
shorter name's tokens are a strict subset of the longer name's tokens AND
the shorter name has *exactly one* longer-form match in the table. For
each such pair, merges the short row into the long row:
  - mentions are reassigned (UPDATE mentions SET entity_id = long_id WHERE
    entity_id = short_id)
  - aliases of long row gain (short canonical_name + short's aliases)
  - metadata merged (short fills missing keys; long wins on conflict)
  - short row deleted

Idempotent: safe to re-run. Conservative: never merges across entity types
or when ambiguous (≥2 long-form candidates exist for the same short).

Usage:
    python3 scripts/agent_dedup_cleanup.py            # apply
    python3 scripts/agent_dedup_cleanup.py --dry-run  # preview only
"""

from __future__ import annotations

import argparse
import logging
import sys

from agent_common import SCHEMA, get_supabase_client, setup_logging
from agent_index import _tokenize_name


def find_merge_pairs(entities: list[dict]) -> list[tuple[dict, dict]]:
    """Return list of (short_ent, long_ent) pairs to merge."""
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        by_type.setdefault(e.get("type", ""), []).append(e)

    pairs: list[tuple[dict, dict]] = []
    for ent_type, ents in by_type.items():
        names_to_ent: dict[str, dict] = {e["canonical_name"]: e for e in ents}
        token_cache = {n: _tokenize_name(n) for n in names_to_ent}
        for short_name, short_ent in names_to_ent.items():
            short_toks = token_cache[short_name]
            if not short_toks:
                continue
            candidates: list[str] = []
            for long_name, _long_ent in names_to_ent.items():
                if long_name == short_name:
                    continue
                long_toks = token_cache[long_name]
                if len(long_toks) <= len(short_toks):
                    continue
                if all(t in long_toks for t in short_toks):
                    candidates.append(long_name)
            if len(candidates) == 1:
                pairs.append((short_ent, names_to_ent[candidates[0]]))
    return pairs


def apply_merge(client, short_ent: dict, long_ent: dict) -> None:
    short_id = int(short_ent["id"])
    long_id = int(long_ent["id"])
    # 1. Reassign all mentions from short to long
    client.schema(SCHEMA).table("mentions").update(
        {"entity_id": long_id}
    ).eq("entity_id", short_id).execute()
    # 2. Merge aliases (preserve old short-form canonical as alias) + metadata
    merged_aliases = sorted(set(
        (long_ent.get("aliases") or []) +
        (short_ent.get("aliases") or []) +
        [short_ent["canonical_name"]]
    ))
    merged_metadata = {**(short_ent.get("metadata") or {}), **(long_ent.get("metadata") or {})}
    client.schema(SCHEMA).table("entities").update({
        "aliases": merged_aliases,
        "metadata": merged_metadata,
    }).eq("id", long_id).execute()
    # 3. Delete the short row
    client.schema(SCHEMA).table("entities").delete().eq("id", short_id).execute()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be merged; don't modify the DB")
    args = parser.parse_args()

    setup_logging("agent_dedup_cleanup")

    client = get_supabase_client()
    entities = client.schema(SCHEMA).table("entities").select("*").execute().data or []
    logging.info("Fetched %d entities", len(entities))

    pairs = find_merge_pairs(entities)
    logging.info("Found %d short→long merge candidates", len(pairs))

    if not pairs:
        print("Nothing to merge.")
        return 0

    print()
    print("=" * 70)
    print(f"{'PREVIEW' if args.dry_run else 'APPLYING'} {len(pairs)} merges")
    print("=" * 70)
    for short_ent, long_ent in pairs:
        print(f"  [{short_ent.get('type', '')}] '{short_ent['canonical_name']}' "
              f"→ '{long_ent['canonical_name']}'")
    print()

    if args.dry_run:
        print("(Dry run; no DB changes.)")
        return 0

    applied = 0
    for short_ent, long_ent in pairs:
        try:
            apply_merge(client, short_ent, long_ent)
            applied += 1
        except Exception as e:
            logging.error(
                "Merge failed: '%s' → '%s': %s",
                short_ent["canonical_name"], long_ent["canonical_name"], e,
            )
    logging.info("Applied %d / %d merges", applied, len(pairs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
