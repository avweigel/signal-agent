#!/usr/bin/env python3
"""
Migration runner for signal-agent.

Two modes, picked automatically based on what's configured:

1. Direct apply via psycopg2 if `~/workspace/agent-data/state/supabase-postgres-url`
   exists. The file should contain a Postgres connection URL — find it in the
   Supabase dashboard under Project Settings → Database → Connection string
   (use the `Session pooler` or `Transaction pooler` variant, not Direct,
   for IPv4 compatibility).

2. Otherwise: prints the SQL with copy-paste instructions for the Supabase
   dashboard SQL editor. After you paste-and-run there, re-run this script
   and it will verify all expected tables exist.

Idempotent. Safe to re-run.

Usage:
    python3 scripts/migrate.py
    python3 scripts/migrate.py --verify-only
    python3 scripts/migrate.py --print-sql
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from agent_common import (
    AGENT_DATA,
    SCHEMA,
    STATE_DIR,
    get_supabase_client,
    setup_logging,
)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
POSTGRES_URL_PATH = STATE_DIR / "supabase-postgres-url"
EXPECTED_TABLES = [
    "tasks", "cursors", "categories", "digests",
    "entities", "mentions", "relationships",
]


def collect_migrations() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def apply_via_psycopg2(connection_url: str, sql: str) -> None:
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError:
        print(
            "psycopg2 not installed. Install it with:\n"
            "    python3 -m pip install --user psycopg2-binary\n"
            "Or skip the postgres URL and use the dashboard SQL editor "
            "(re-run this script without --apply for instructions).",
            file=sys.stderr,
        )
        sys.exit(1)
    logging.info("Connecting to Postgres via psycopg2")
    conn = psycopg2.connect(connection_url)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        logging.info("SQL applied successfully")
    finally:
        conn.close()


def print_dashboard_instructions(sql_files: list[Path]) -> None:
    sql_combined = "\n\n".join(f.read_text(encoding="utf-8") for f in sql_files)
    print()
    print("=" * 70)
    print("MANUAL MIGRATION (Supabase dashboard SQL editor)")
    print("=" * 70)
    print()
    print("No Postgres connection URL configured at:")
    print(f"  {POSTGRES_URL_PATH}")
    print()
    print("Apply the migration manually via the dashboard:")
    print()
    print("  1. Open https://supabase.com/dashboard")
    print("  2. Select the data-hub project")
    print("  3. Sidebar → SQL Editor → New query")
    print("  4. Paste the SQL below")
    print("  5. Click Run")
    print()
    print("  6. Re-run this script (`python3 scripts/migrate.py`) to verify")
    print()
    print("To automate future migrations, paste your Postgres connection")
    print(f"URL into {POSTGRES_URL_PATH} (mode 0600).")
    print()
    print("-" * 70)
    print("SQL TO PASTE:")
    print("-" * 70)
    print()
    print(sql_combined)
    print()
    print("-" * 70)


def verify_tables() -> tuple[list[str], list[str]]:
    """Return (existing, missing) tables in the signal_agent schema."""
    client = get_supabase_client()
    existing: list[str] = []
    missing: list[str] = []
    for table in EXPECTED_TABLES:
        try:
            # head=True returns no rows; just probes existence + count.
            client.schema(SCHEMA).table(table).select("*", count="exact", head=True).execute()
            existing.append(table)
        except Exception as e:
            err = str(e).lower()
            if "could not find" in err or "does not exist" in err or "pgrst205" in err:
                missing.append(table)
            else:
                # Different error — surface as missing but log the detail.
                logging.warning("Probe for %s.%s: %s", SCHEMA, table, e)
                missing.append(table)
    return existing, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true",
                        help="Skip apply step; only check table existence.")
    parser.add_argument("--print-sql", action="store_true",
                        help="Print the combined SQL and exit.")
    args = parser.parse_args()

    setup_logging("migrate")

    sql_files = collect_migrations()
    if not sql_files:
        logging.error("No migrations found in %s", MIGRATIONS_DIR)
        return 1

    if args.print_sql:
        for f in sql_files:
            print(f.read_text(encoding="utf-8"))
        return 0

    # Attempt direct apply if configured and not verify-only.
    if not args.verify_only and POSTGRES_URL_PATH.exists():
        connection_url = POSTGRES_URL_PATH.read_text(encoding="utf-8").strip()
        if connection_url:
            sql = "\n\n".join(f.read_text(encoding="utf-8") for f in sql_files)
            apply_via_psycopg2(connection_url, sql)

    # Verify via the Supabase Python client.
    logging.info("Verifying tables in schema %r", SCHEMA)
    existing, missing = verify_tables()

    print()
    print(f"Schema: {SCHEMA}")
    for t in EXPECTED_TABLES:
        marker = "✓" if t in existing else "✗"
        print(f"  [{marker}] {t}")
    print()

    if missing:
        if not args.verify_only and not POSTGRES_URL_PATH.exists():
            print_dashboard_instructions(sql_files)
            return 2
        logging.error("Missing tables: %s", ", ".join(missing))
        return 2

    logging.info("All tables present. Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
