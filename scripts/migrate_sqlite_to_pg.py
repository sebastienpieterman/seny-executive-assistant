#!/usr/bin/env python3
"""
SQLite → PostgreSQL data migration script for Seny.

Run via: railway run python scripts/migrate_sqlite_to_pg.py

Reads from SQLite (DATABASE_PATH or default data/seny.db).
Writes to PostgreSQL (DATABASE_URL environment variable).
"""
import os
import sys
import sqlite3
import psycopg2
import psycopg2.extras
from pathlib import Path
from datetime import datetime

# Source: SQLite
SQLITE_PATH = os.environ.get("DATABASE_PATH", str(Path(__file__).parent.parent / "data" / "seny.db"))

# Destination: PostgreSQL
PG_URL = os.environ.get("DATABASE_URL")
if not PG_URL:
    print("ERROR: DATABASE_URL not set. Run via: railway run python scripts/migrate_sqlite_to_pg.py")
    sys.exit(1)

print(f"Source: {SQLITE_PATH}")
print(f"Target: PostgreSQL (URL set)")

# Tables to migrate in FK-safe order
# Format: (table_name, id_column_or_None)
# id_column = None means no BIGSERIAL sequence to reset
TABLES_IN_ORDER = [
    # Tier 1
    ("users", "id"),
    ("conversations", "id"),
    # Tier 2
    ("google_tokens", None),  # composite key
    ("microsoft_tokens", None),
    ("slack_tokens", None),
    ("telegram_sessions", "id"),
    ("telegram_bot_user_links", "id"),
    ("user_settings", "id"),
    ("push_subscriptions", "id"),
    ("usage_logs", "id"),
    ("user_memories", "id"),
    ("user_pattern_preferences", "id"),
    ("user_feedback", "id"),
    ("email_feedback_tokens", "id"),
    # Tier 3
    ("messages", "id"),
    # Tier 4
    ("notes", "id"),
    ("tasks", "id"),
    ("task_reminders", "id"),
    ("people", "id"),
    ("people_followups", "id"),
    ("projects", "id"),
    ("ideas", "id"),
    # Tier 5
    ("nudges", "id"),
    ("scanned_items", "id"),
    ("item_classifications", "id"),
    ("detected_actions", "id"),
    ("cross_references", "id"),
    ("priority_context", "id"),
    # Tier 6 - all remaining tables
    ("activity_log", "id"),
    ("note_links", "id"),
    ("note_tags", "id"),
    ("contacts_sync_status", "id"),
    ("google_contacts", "id"),
    ("drive_files", "id"),
    ("drive_sync_status", "id"),
    ("location_history", "id"),
    ("location_import_log", "id"),
    ("browser_history", "id"),
    ("local_files", "id"),
    ("youtube_liked_videos", "id"),
    ("youtube_playlists", "id"),
    ("youtube_subscriptions", "id"),
    ("youtube_sync_status", "id"),
    ("voice_sessions", "id"),
    ("scanner_runs", "id"),
    ("slack_bot_conversations", "id"),
    ("telegram_bot_conversations", "id"),
    ("calendar_preferences", "id"),
    ("scheduled_notifications", "id"),
    ("multichannel_chat_settings", "id"),
    ("embedding_tracking", "id"),
    ("entity_mappings", "id"),
    ("ignored_senders", "id"),
    ("inbox_log", "id"),
    ("sync_status", "id"),
    ("slack_channel_cursors", "id"),
]

BATCH_SIZE = 500  # rows per INSERT batch


def get_sqlite_tables(sqlite_conn):
    """Get list of all tables in SQLite database."""
    cursor = sqlite_conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return [row[0] for row in cursor.fetchall()]


def get_table_columns(sqlite_conn, table_name):
    """Get column names for a table."""
    cursor = sqlite_conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def migrate_table(sqlite_conn, pg_conn, table_name, id_column):
    """Migrate all rows from a SQLite table to PostgreSQL."""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()

    # Get row count
    sqlite_cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    total = sqlite_cursor.fetchone()[0]

    if total == 0:
        print(f"  {table_name}: 0 rows (skipping)")
        return 0

    # Get columns
    columns = get_table_columns(sqlite_conn, table_name)
    col_list = ", ".join(columns)
    placeholder_list = ", ".join(["%s"] * len(columns))

    # Fetch and insert in batches
    sqlite_cursor.execute(f"SELECT {col_list} FROM {table_name}")
    inserted = 0

    while True:
        batch = sqlite_cursor.fetchmany(BATCH_SIZE)
        if not batch:
            break

        # Convert rows to list of tuples
        rows = [tuple(row) for row in batch]

        pg_cursor.executemany(
            f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholder_list}) ON CONFLICT DO NOTHING",
            rows
        )
        inserted += len(rows)

    pg_conn.commit()
    print(f"  {table_name}: {inserted}/{total} rows migrated")
    return inserted


def reset_sequence(pg_conn, table_name, id_column):
    """Reset BIGSERIAL sequence to max(id) + 1 to prevent conflicts."""
    cursor = pg_conn.cursor()
    try:
        cursor.execute(f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', '{id_column}'),
                COALESCE((SELECT MAX({id_column}) FROM {table_name}), 1)
            )
        """)
        pg_conn.commit()
        print(f"  {table_name}.{id_column}: sequence reset")
    except Exception as e:
        print(f"  Warning: Could not reset sequence for {table_name}.{id_column}: {e}")
        pg_conn.rollback()


def main():
    start = datetime.now()
    print(f"\n{'='*60}")
    print(f"Seny Data Migration: SQLite → PostgreSQL")
    print(f"Started: {start.isoformat()}")
    print(f"{'='*60}\n")

    # Connect to both databases
    print("Connecting to databases...")
    if not os.path.exists(SQLITE_PATH):
        print(f"ERROR: SQLite database not found at: {SQLITE_PATH}")
        sys.exit(1)

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(PG_URL)
    pg_conn.autocommit = False

    # Verify SQLite has data
    sqlite_cursor = sqlite_conn.cursor()
    sqlite_cursor.execute("SELECT COUNT(*) FROM users")
    user_count = sqlite_cursor.fetchone()[0]
    print(f"SQLite has {user_count} users\n")

    # Disable FK constraints for migration
    pg_cursor = pg_conn.cursor()
    pg_cursor.execute("SET session_replication_role = 'replica'")
    pg_conn.commit()

    # Get actual SQLite tables (to skip tables that don't exist in this DB)
    sqlite_tables = set(get_sqlite_tables(sqlite_conn))

    # Migrate each table
    total_rows = 0
    print("Migrating tables...")
    for entry in TABLES_IN_ORDER:
        if entry is None:
            continue
        table_name, id_column = entry
        if table_name not in sqlite_tables:
            print(f"  {table_name}: not in SQLite (skipping)")
            continue
        try:
            rows = migrate_table(sqlite_conn, pg_conn, table_name, id_column)
            total_rows += rows
        except Exception as e:
            print(f"  ERROR migrating {table_name}: {e}")
            pg_conn.rollback()
            raise

    # Also migrate any SQLite tables not in our explicit list
    explicit_tables = {entry[0] for entry in TABLES_IN_ORDER if entry}
    remaining = sqlite_tables - explicit_tables
    if remaining:
        print(f"\nMigrating {len(remaining)} additional tables not in explicit order...")
        for table_name in sorted(remaining):
            # Skip FTS tables (they no longer exist in PostgreSQL)
            if (table_name.endswith('_fts') or table_name.endswith('_fts_config') or
                    table_name.endswith('_fts_data') or table_name.endswith('_fts_idx') or
                    table_name.endswith('_fts_docsize')):
                print(f"  {table_name}: FTS table (skipping — not in PostgreSQL)")
                continue
            try:
                rows = migrate_table(sqlite_conn, pg_conn, table_name, 'id')
                total_rows += rows
            except Exception as e:
                print(f"  Warning: {table_name}: {e}")

    # Re-enable FK constraints
    pg_cursor.execute("SET session_replication_role = 'origin'")
    pg_conn.commit()

    # Reset all sequences
    print("\nResetting BIGSERIAL sequences...")
    for entry in TABLES_IN_ORDER:
        if entry is None:
            continue
        table_name, id_column = entry
        if id_column and table_name in sqlite_tables:
            reset_sequence(pg_conn, table_name, id_column)

    # Validation: row count comparison
    print("\nValidating row counts...")
    mismatches = []
    sqlite_cursor2 = sqlite_conn.cursor()
    pg_cursor2 = pg_conn.cursor()
    for entry in TABLES_IN_ORDER:
        if entry is None:
            continue
        table_name, _ = entry
        if table_name not in sqlite_tables:
            continue
        sqlite_cursor2.execute(f"SELECT COUNT(*) FROM {table_name}")
        sq_count = sqlite_cursor2.fetchone()[0]
        pg_cursor2.execute(f"SELECT COUNT(*) FROM {table_name}")
        pg_count = pg_cursor2.fetchone()[0]
        if sq_count != pg_count:
            mismatches.append(f"  MISMATCH: {table_name} — SQLite: {sq_count}, PostgreSQL: {pg_count}")
        else:
            print(f"  ✓ {table_name}: {pg_count} rows")

    if mismatches:
        print("\n⚠️  ROW COUNT MISMATCHES:")
        for m in mismatches:
            print(m)
    else:
        print("\n✓ All row counts match!")

    duration = (datetime.now() - start).total_seconds()
    print(f"\n{'='*60}")
    print(f"Migration complete: {total_rows} total rows")
    print(f"Duration: {duration:.1f}s")
    print(f"{'='*60}\n")

    sqlite_conn.close()
    pg_conn.close()


if __name__ == "__main__":
    main()
