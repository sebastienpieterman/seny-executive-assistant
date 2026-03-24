"""
PostgreSQL database management for Seny web application.

Handles database initialization and connection management.
Uses psycopg2 without ORM for simplicity.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
import psycopg2.extras


# PostgreSQL connection pool (initialized on first use)
_pg_pool: Optional[psycopg2.pool.SimpleConnectionPool] = None

# Register psycopg2 type casters that return ISO strings for timestamp columns.
# All existing code expects strings (from SQLite TEXT columns); this preserves
# that behaviour on the PostgreSQL path without changing any query or model code.
def _cast_ts(value, cur):
    # psycopg2 passes the raw PostgreSQL text (already a string like "2026-03-03 12:00:00")
    # Return as-is so all existing code that expects str dates continues to work.
    return value

_TS    = psycopg2.extensions.new_type((1114,),  "TIMESTAMP_AS_STR",   _cast_ts)
_TSTZ  = psycopg2.extensions.new_type((1184,),  "TIMESTAMPTZ_AS_STR", _cast_ts)
_DATE  = psycopg2.extensions.new_type((1082,),  "DATE_AS_STR",        _cast_ts)
psycopg2.extensions.register_type(_TS)
psycopg2.extensions.register_type(_TSTZ)
psycopg2.extensions.register_type(_DATE)


def _get_pg_pool() -> psycopg2.pool.SimpleConnectionPool:
    """Get or create the PostgreSQL connection pool."""
    global _pg_pool
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required. "
            "SQLite is no longer supported.\n"
            "For local development, use: docker-compose up -d postgres\n"
            "For Railway deployment, add a PostgreSQL plugin.\n"
            "To migrate existing SQLite data: python scripts/migrate_sqlite_to_pg.py"
        )
    if _pg_pool is None:
        _pg_pool = psycopg2.pool.SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
            options="-c timezone=UTC"
        )
    return _pg_pool


def extract_snippet(text: str, query: str, context_chars: int = 150, bold_start: str = '**', bold_end: str = '**') -> str:
    """
    Extract a snippet of text around the first occurrence of any query word.
    Mimics SQLite FTS5 snippet() behavior.
    Returns empty string if text is None or query not found.
    """
    if not text or not query:
        return (text or '')[:context_chars]

    query_words = [w.strip().lower() for w in query.split() if w.strip()]
    text_lower = text.lower()

    # Find the earliest match position
    best_pos = len(text)
    best_word = None
    for word in query_words:
        pos = text_lower.find(word)
        if pos != -1 and pos < best_pos:
            best_pos = pos
            best_word = word

    if best_word is None:
        return text[:context_chars] + ('...' if len(text) > context_chars else '')

    # Extract context window around match
    start = max(0, best_pos - context_chars // 2)
    end = min(len(text), best_pos + len(best_word) + context_chars // 2)
    snippet = text[start:end]

    # Bold all query word occurrences in snippet
    for word in query_words:
        snippet = re.sub(re.escape(word), f'{bold_start}{word}{bold_end}', snippet, flags=re.IGNORECASE)

    prefix = '...' if start > 0 else ''
    suffix = '...' if end < len(text) else ''
    return f'{prefix}{snippet}{suffix}'


def init_db() -> None:
    """
    Initialize the PostgreSQL database and create tables if they don't exist.

    Creates:
        - users table: For user authentication and data association

    This function is idempotent - safe to call multiple times.
    Uses IF NOT EXISTS to avoid errors on repeated calls.
    """
    # Connect to PostgreSQL
    _pool = _get_pg_pool()
    conn = _pool.getconn()
    conn.autocommit = True  # each statement is its own transaction — no cascade failures
    cursor = conn.cursor()

    # Timing diagnostics for Railway deploy investigation (Issue #13)
    init_start = time.time()
    timings = {}
    print(f"[INIT_DB] Starting database initialization...")

    try:
        # Enable pg_trgm for fast text search
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        conn.commit()

        # ============================================================================
        # SECTION: Core Tables (users, conversations, messages)
        # ============================================================================
        section_start = time.time()

        # Create users table
        # SECURITY: email is UNIQUE to prevent duplicate registrations
        # SECURITY: created_at uses CURRENT_TIMESTAMP for audit trail
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                hashed_password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create index on email for faster lookups during login
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)
        """)

        # Create token_blocklist table for JWT revocation
        # Stores JTI (JWT ID) of revoked tokens until they expire
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS token_blocklist (
                id BIGSERIAL PRIMARY KEY,
                jti TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # Create index on jti for fast blocklist lookups during token verification
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_blocklist_jti ON token_blocklist(jti)
        """)

        # Clean up expired blocklist entries on startup
        # For a single-user app with 7-day tokens, startup cleanup is sufficient
        cursor.execute("""
            DELETE FROM token_blocklist WHERE expires_at < CURRENT_TIMESTAMP
        """)

        # Create conversations table
        # Stores conversation metadata with foreign key to users
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
            )
        """)

        # Create index on user_id for faster conversation lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)
        """)

        # Create messages table
        # Stores individual messages within conversations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations (id) ON DELETE CASCADE
            )
        """)

        # Create index on conversation_id for faster message retrieval
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id)
        """)

        timings['core_tables'] = time.time() - section_start
        print(f"[INIT_DB] Core tables: {timings['core_tables']:.2f}s")

        # ============================================================================
        # SECTION: Migrations (ALTER TABLE operations)
        # ============================================================================
        section_start = time.time()

        # Migration: Add title column to conversations table if it doesn't exist
        # This is a safe migration that won't fail if column already exists
        try:
            cursor.execute("ALTER TABLE conversations ADD COLUMN title TEXT DEFAULT NULL")
        except Exception as e:
            # Column already exists - this is expected after first migration
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add model column to conversations table for per-conversation model selection
        # When NULL, uses user's default model preference
        try:
            cursor.execute("ALTER TABLE conversations ADD COLUMN model TEXT DEFAULT NULL")
        except Exception as e:
            # Column already exists - this is expected after first migration
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add type column to tasks table for task/errand distinction
        # Merges Admin items into Tasks - errands become tasks with type='errand'
        try:
            cursor.execute("ALTER TABLE tasks ADD COLUMN type TEXT DEFAULT 'task'")
            print("[INIT_DB] Added 'type' column to tasks table")
        except Exception as e:
            # Column already exists or table doesn't exist yet (created later with column included)
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Clean up orphaned inbox_log entries that reference deleted admin_items table (Phase 80)
        try:
            cursor.execute("""
                UPDATE inbox_log
                SET routed_to_table = NULL, routed_to_id = NULL
                WHERE routed_to_table = 'admin_items'
            """)
            updated = cursor.rowcount
            if updated:
                print(f"[INIT_DB] Cleared {updated} orphaned inbox_log entries referencing admin_items")
        except Exception:
            pass

        # Migration: Add digest preferences columns to user_settings
        # These control the daily digest feature
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN digest_enabled INTEGER DEFAULT 1")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN digest_time TEXT DEFAULT '07:00'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN digest_email INTEGER DEFAULT 1")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN digest_push INTEGER DEFAULT 1")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN digest_timezone TEXT DEFAULT 'America/Chicago'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add weekly review preferences columns
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN weekly_review_enabled INTEGER DEFAULT 1")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN weekly_review_day TEXT DEFAULT 'sunday'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN weekly_review_time TEXT DEFAULT '18:00'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add classification model preference
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN classification_model TEXT DEFAULT 'claude-haiku-4-5-20251001'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add classification_attempts column for retry limiting
        try:
            cursor.execute("ALTER TABLE scanned_items ADD COLUMN classification_attempts INTEGER DEFAULT 0")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add direction column for outbound communications tracking (HF-03)
        try:
            cursor.execute("ALTER TABLE scanned_items ADD COLUMN direction TEXT DEFAULT 'inbound'")
        except Exception:
            pass  # Column already exists

        # Migration: Add nudge preferences columns to user_settings
        # nudge_enabled: Master toggle for proactive nudges
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_enabled INTEGER DEFAULT 1")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_quiet_start: Start of quiet hours (HH:MM format, user's timezone)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_quiet_start TEXT DEFAULT '22:00'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_quiet_end: End of quiet hours (HH:MM format, user's timezone)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_quiet_end TEXT DEFAULT '08:00'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_max_urgent_per_hour: Rate limit for urgent nudges
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_max_urgent_per_hour INTEGER DEFAULT 3")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_batch_interval_minutes: How often to send batched normal-priority nudges
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_batch_interval_minutes INTEGER DEFAULT 60")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_channels: JSON array of enabled channels (e.g., '["push", "slack"]')
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_channels TEXT DEFAULT '[\"push\"]'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_last_batch_at: Timestamp of last batch send (for interval enforcement)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_last_batch_at TIMESTAMP")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_batch_channel: Channel for batch nudges (defaults to 'push')
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_batch_channel TEXT DEFAULT 'push'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # nudge_drip_interval_minutes: How often to send drip nudges
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_drip_interval_minutes INTEGER DEFAULT 15")
        except Exception:
            pass

        # nudge_last_drip_at: Timestamp of last drip send
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN nudge_last_drip_at TIMESTAMP")
        except Exception:
            pass

        # nudge_quiet_skip_weekend: Skip nudges on Saturdays and Sundays
        try:
            cursor.execute(
                "ALTER TABLE user_settings ADD COLUMN nudge_quiet_skip_weekend INTEGER DEFAULT 0"
            )
            print("[INIT_DB] Migration: added nudge_quiet_skip_weekend column")
        except Exception:
            pass  # Already exists

        # nudge_smart_dedup: Use Haiku to detect topic-level duplicates when dismissing nudges
        try:
            cursor.execute(
                "ALTER TABLE user_settings ADD COLUMN nudge_smart_dedup INTEGER DEFAULT 1"
            )
            print("[INIT_DB] Migration: added nudge_smart_dedup column")
        except Exception:
            pass  # Already exists

        # Migration: Add channel exclusion columns
        # slack_excluded_channels: JSON array of Slack channel IDs to exclude from scanner
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN slack_excluded_channels TEXT")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # telegram_excluded_chats: JSON array of Telegram chat IDs to exclude from scanner
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN telegram_excluded_chats TEXT")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add user_response column to nudges table
        # Tracks user's response to nudges: 'helpful', 'dismissed', 'snoozed'
        try:
            cursor.execute("ALTER TABLE nudges ADD COLUMN user_response TEXT")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add scanner interval preferences
        # Per-source scan frequency overrides (in minutes)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN scanner_gmail_interval_minutes INTEGER DEFAULT 15")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN scanner_slack_interval_minutes INTEGER DEFAULT 120")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN scanner_telegram_interval_minutes INTEGER DEFAULT 5")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN scanner_calendar_interval_minutes INTEGER DEFAULT 60")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add classification tier preference
        # 'haiku' (fast, economical) or 'full' (sonnet, more thorough)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN classification_tier TEXT DEFAULT 'haiku'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add bot_token and bot_user_id to slack_tokens
        # Required for Slack bot DM chat functionality
        try:
            cursor.execute("ALTER TABLE slack_tokens ADD COLUMN bot_token TEXT")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE slack_tokens ADD COLUMN bot_user_id TEXT")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add reason + item_context columns to user_feedback
        # reason: Stores WHY the user gave feedback (e.g., "wrong date", "not relevant")
        # item_context: Stores a short quote of the item text being rated
        try:
            cursor.execute("ALTER TABLE user_feedback ADD COLUMN reason TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        try:
            cursor.execute("ALTER TABLE user_feedback ADD COLUMN item_context TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Migration: Add lessons_learned column to user_pattern_preferences
        # Stores JSON aggregation of non-null feedback reasons grouped by feedback_type
        try:
            cursor.execute("ALTER TABLE user_pattern_preferences ADD COLUMN lessons_learned TEXT DEFAULT NULL")
        except Exception:
            pass  # Column already exists

        # Migration: Add relationship_type to people table
        try:
            cursor.execute("ALTER TABLE people ADD COLUMN relationship_type TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: Add slack_user_id to slack_bot_conversations (ISS-005)
        # Table was created before this column was added; existing DBs never got it
        try:
            cursor.execute("ALTER TABLE slack_bot_conversations ADD COLUMN slack_user_id TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN day_start_hour INTEGER DEFAULT 15")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: Add delivery message ID columns to nudges
        # Enables reply threading by cross-referencing incoming messages to original nudge
        try:
            cursor.execute("ALTER TABLE nudges ADD COLUMN telegram_message_id TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        try:
            cursor.execute("ALTER TABLE nudges ADD COLUMN slack_message_ts TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: Add dismiss_reason column to nudges
        # Stores the staleness reason when a drip nudge is auto-dismissed before delivery
        try:
            cursor.execute("ALTER TABLE nudges ADD COLUMN dismiss_reason TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: Add screen agent API key to user_settings
        # Static non-expiring key for external screen awareness agent
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN screen_agent_api_key TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: HF-14 screen agent cooldown state (shared across workers)
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN screen_cooldown_until DOUBLE PRECISION")
            conn.commit()
            print("[INIT_DB] Migration HF-14: added screen_cooldown_until column")
        except Exception as _e:
            print(f"[INIT_DB] Migration HF-14: screen_cooldown_until already exists or error: {_e!r}")

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN screen_last_nudge_at DOUBLE PRECISION")
            conn.commit()
            print("[INIT_DB] Migration HF-14: added screen_last_nudge_at column")
        except Exception as _e:
            print(f"[INIT_DB] Migration HF-14: screen_last_nudge_at already exists or error: {_e!r}")

        # Migration: Add pending action notification channel to user_settings
        # Controls which channel (telegram, slack, none) receives pending action notifications
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN pending_action_notification_channel TEXT DEFAULT 'none'")
            conn.commit()
            print("[INIT_DB] Migration: added pending_action_notification_channel to user_settings")
        except Exception:
            pass  # Column already exists

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN default_calendar_email TEXT DEFAULT NULL")
            conn.commit()
            print("[INIT_DB] Migration: added default_calendar_email to user_settings")
        except Exception:
            pass  # Column already exists

        try:
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_nudges_telegram_message_id
                ON nudges(user_id, telegram_message_id)
                WHERE telegram_message_id IS NOT NULL
            """)
        except Exception:
            pass  # nudges table may not exist yet on fresh DB (created later)

        # Migration: Add suppression_overrides column to user_pattern_preferences
        # Stores per-item-type override flags that prevent suppression regardless of computed score.
        # DEFAULT '{}'::jsonb sets all existing rows to an empty JSON object immediately.
        try:
            cursor.execute("""
                ALTER TABLE user_pattern_preferences
                ADD COLUMN suppression_overrides JSONB DEFAULT '{}'::jsonb
            """)
            conn.commit()
            print("[INIT_DB] Migration: added suppression_overrides to user_pattern_preferences")
        except Exception:
            pass  # Column already exists

        # Migration: Add research_audit_runs table
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS research_audit_runs (
                    id BIGSERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    run_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    fidelity_score FLOAT,
                    negative_unabsorbed_count INTEGER DEFAULT 0,
                    suppression_gap_count INTEGER DEFAULT 0,
                    signals_json TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_research_audit_runs_user
                ON research_audit_runs(user_id, run_at DESC)
            """)
        except Exception:
            pass

        # Migration: Add system_heartbeats table
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_heartbeats (
                    subsystem TEXT PRIMARY KEY,
                    last_run_at TIMESTAMP,
                    last_error TEXT,
                    last_alerted_at TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
        except Exception:
            pass

        # Migration: Add loop closure delay columns to nudges
        # closure_delay_count: tracks how many times this nudge was delayed by closure check.
        #   Capped at 1 — once delayed once, it sends on the second pass regardless.
        # closure_hold_until: when set, drip queue skips this nudge until this timestamp.
        #   NULL = eligible to send now.
        try:
            cursor.execute("""
                ALTER TABLE nudges
                ADD COLUMN closure_delay_count SMALLINT NOT NULL DEFAULT 0
            """)
            conn.commit()
            print("[INIT_DB] Migration 70.1-01: added closure_delay_count to nudges")
        except Exception:
            pass  # Column already exists

        try:
            cursor.execute("""
                ALTER TABLE nudges
                ADD COLUMN closure_hold_until TIMESTAMP DEFAULT NULL
            """)
            conn.commit()
            print("[INIT_DB] Migration 70.1-01: added closure_hold_until to nudges")
        except Exception:
            pass  # Column already exists

        # Migration: Add user profile columns to user_settings
        # These power the dynamic system prompt template variables
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN user_name TEXT DEFAULT NULL")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN user_pronouns_subject TEXT DEFAULT 'they'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN user_pronouns_object TEXT DEFAULT 'them'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN user_pronouns_possessive TEXT DEFAULT 'their'")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN user_context TEXT DEFAULT NULL")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN key_people TEXT DEFAULT NULL")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN key_projects TEXT DEFAULT NULL")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN priorities TEXT DEFAULT NULL")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN setup_complete INTEGER DEFAULT 0")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        # Migration: Add feature flag columns to user_settings
        # These control optional agent features in the setup wizard
        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN screen_agent_enabled INTEGER DEFAULT 0")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN browser_agent_enabled INTEGER DEFAULT 0")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        try:
            cursor.execute("ALTER TABLE user_settings ADD COLUMN personality_casual INTEGER DEFAULT 0")
        except Exception as e:
            if "already exists" not in str(e).lower() \
                    and "does not exist" not in str(e).lower() \
                    and "undefined" not in str(e).lower():
                raise

        timings['migrations'] = time.time() - section_start
        print(f"[INIT_DB] Migrations: {timings['migrations']:.2f}s")
        # ============================================================================
        # SECTION: Messages FTS (virtual table, triggers, backfill)
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # SECTION: Google OAuth Tables
        # ============================================================================
        section_start = time.time()

        # Create google_tokens table for OAuth token storage
        # Stores OAuth 2.0 credentials - supports multiple Google accounts per user
        # Renamed from gmail_tokens in to support Gmail + Calendar combined OAuth
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS google_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                email TEXT NOT NULL UNIQUE,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                token_uri TEXT NOT NULL,
                scopes TEXT NOT NULL,
                expiry TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create index on user_id for listing user's connected accounts
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_google_tokens_user_id ON google_tokens(user_id)
        """)

        # Migration: Rename gmail_tokens to google_tokens if old table exists
        # This handles existing databases from before the rename
        try:
            cursor.execute("SELECT to_regclass('gmail_tokens')")
            result = cursor.fetchone()
            if result and result[0] is not None:
                # Old table exists - migrate data to new table
                cursor.execute("""
                    INSERT INTO google_tokens
                    (user_id, email, access_token, refresh_token, token_uri, scopes, expiry, created_at, updated_at)
                    SELECT user_id, email, access_token, refresh_token, token_uri, scopes, expiry, created_at, updated_at
                    FROM gmail_tokens
                    ON CONFLICT DO NOTHING
                """)
                # Drop old table after migration
                cursor.execute("DROP TABLE gmail_tokens")
                print("✓ Migrated gmail_tokens to google_tokens")
        except Exception:
            pass  # Table doesn't exist or migration already done

        timings['google_oauth'] = time.time() - section_start
        print(f"[INIT_DB] Google OAuth tables: {timings['google_oauth']:.2f}s")

        # ============================================================================
        # SECTION: Microsoft OAuth Tables
        # ============================================================================
        section_start = time.time()

        # Create microsoft_tokens table for Microsoft Graph API OAuth tokens
        # Supports both personal (Outlook.com) and work (Microsoft 365) accounts
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS microsoft_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                token_uri TEXT NOT NULL DEFAULT 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
                scopes TEXT NOT NULL,
                expiry TEXT,
                account_type TEXT DEFAULT 'unknown',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, email)
            )
        """)

        # Create index on user_id for listing user's connected Microsoft accounts
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_microsoft_tokens_user_id ON microsoft_tokens(user_id)
        """)

        timings['microsoft_oauth'] = time.time() - section_start
        print(f"[INIT_DB] Microsoft OAuth tables: {timings['microsoft_oauth']:.2f}s")

        # ============================================================================
        # SECTION: Notes (tables + FTS)
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Notes System Tables
        # ============================================================================

        # Create notes table - stores user notes with wiki-link and tag support
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create index on user_id for listing user's notes
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_user_id ON notes(user_id)
        """)

        # Create note_tags table - many-to-many for #tag extraction
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS note_tags (
                id BIGSERIAL PRIMARY KEY,
                note_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
                UNIQUE(note_id, tag)
            )
        """)

        # Create index on tag for filtering by tag
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags(tag)
        """)

        # Create note_links table - bi-directional [[wiki-links]] between notes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS note_links (
                id BIGSERIAL PRIMARY KEY,
                source_note_id INTEGER NOT NULL,
                target_note_id INTEGER NOT NULL,
                FOREIGN KEY (source_note_id) REFERENCES notes(id) ON DELETE CASCADE,
                FOREIGN KEY (target_note_id) REFERENCES notes(id) ON DELETE CASCADE,
                UNIQUE(source_note_id, target_note_id)
            )
        """)

        # Create indexes for link traversal (both directions)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_note_links_source ON note_links(source_note_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_note_links_target ON note_links(target_note_id)
        """)

        timings['notes'] = time.time() - section_start
        print(f"[INIT_DB] Phase 5 Notes: {timings['notes']:.2f}s")

        # ============================================================================
        # SECTION: Tasks
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Tasks System Tables
        # ============================================================================

        # Create tasks table - stores user tasks with priorities, due dates, and recurrence
        # type column distinguishes 'task' (work/complex) from 'errand' (simple life admin)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                due_date TIMESTAMP,
                completed_at TIMESTAMP,
                category TEXT,
                project TEXT,
                type TEXT DEFAULT 'task',
                is_recurring INTEGER DEFAULT 0,
                recurrence_pattern TEXT,
                recurrence_interval INTEGER DEFAULT 1,
                recurrence_end_date TIMESTAMP,
                parent_task_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_task_id) REFERENCES tasks(id) ON DELETE SET NULL
            )
        """)

        # Create indexes for common task queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_user_due ON tasks(user_id, due_date)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_user_category ON tasks(user_id, category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_user_project ON tasks(user_id, project)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_user_type ON tasks(user_id, type)
        """)

        # Create task_reminders table - stores reminders for tasks
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_reminders (
                id BIGSERIAL PRIMARY KEY,
                task_id INTEGER NOT NULL,
                remind_at TIMESTAMP NOT NULL,
                reminder_type TEXT DEFAULT 'notification',
                is_sent INTEGER DEFAULT 0,
                sent_at TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        """)

        # Create index for finding pending reminders
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_pending ON task_reminders(is_sent, remind_at)
        """)

        timings['tasks'] = time.time() - section_start
        print(f"[INIT_DB] Phase 5 Tasks: {timings['tasks']:.2f}s")

        # ============================================================================
        # SECTION: Notifications
        # ============================================================================
        section_start = time.time()

        # Push notification subscriptions - stores Web Push subscription data
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                p256dh_key TEXT NOT NULL,
                auth_key TEXT NOT NULL,
                user_agent TEXT,
                device_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, endpoint)
            )
        """)

        # Scheduled notifications - timers, alarms, reminders
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_notifications (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                url TEXT,
                type TEXT NOT NULL,
                scheduled_for TIMESTAMP NOT NULL,
                timezone TEXT DEFAULT 'UTC',
                repeat_pattern TEXT,
                repeat_until TIMESTAMP,
                status TEXT DEFAULT 'pending',
                sent_at TIMESTAMP,
                error_message TEXT,
                task_id INTEGER,
                conversation_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
            )
        """)

        # Indexes for efficient notification queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notif_scheduled
            ON scheduled_notifications(scheduled_for, status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notif_user
            ON scheduled_notifications(user_id, status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_push_sub_user
            ON push_subscriptions(user_id)
        """)

        timings['notifications'] = time.time() - section_start
        print(f"[INIT_DB] Notifications: {timings['notifications']:.2f}s")

        # ============================================================================
        # SECTION: Autonomous Nudges
        # ============================================================================
        section_start = time.time()

        # nudges table - Tracks proactive notifications sent to users
        # Supports multiple channels (push, slack, telegram) with delivery tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nudges (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                nudge_type TEXT NOT NULL,
                channel TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                urgency TEXT DEFAULT 'normal',
                source_type TEXT,
                source_id INTEGER,
                status TEXT DEFAULT 'pending',
                sent_at TIMESTAMP,
                delivered_at TIMESTAMP,
                acted_at TIMESTAMP,
                batch_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Indexes for nudges queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_nudges_user_status
            ON nudges(user_id, status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_nudges_user_created
            ON nudges(user_id, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_nudges_source
            ON nudges(user_id, source_type, source_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_nudges_batch
            ON nudges(batch_id) WHERE batch_id IS NOT NULL
        """)

        timings['nudges'] = time.time() - section_start
        print(f"[INIT_DB] Phase 16 Nudges: {timings['nudges']:.2f}s")

        # ============================================================================
        # SECTION: User Pattern Learning
        # ============================================================================
        section_start = time.time()

        # user_feedback table - Stores user feedback reactions on intelligence items
        # Enables learning user preferences from explicit feedback
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                item_id INTEGER,
                feedback_type TEXT NOT NULL,
                feedback_context TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Index on (user_id, item_type) for querying feedback by category
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_feedback_type
            ON user_feedback(user_id, item_type)
        """)

        # Index on (user_id, created_at) for recent feedback queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_feedback_recent
            ON user_feedback(user_id, created_at DESC)
        """)

        # user_pattern_preferences table - Stores computed user pattern preferences
        # Aggregates learned patterns from feedback for quick access
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_pattern_preferences (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL,
                responsive_hours TEXT,
                preferred_channels_by_time TEXT,
                item_type_preferences TEXT,
                last_computed_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        timings['user_patterns'] = time.time() - section_start
        print(f"[INIT_DB] Phase 17 User Patterns: {timings['user_patterns']:.2f}s")

        # ============================================================================
        # SECTION: Email Digest Feedback
        # ============================================================================
        section_start = time.time()

        # email_feedback_tokens table - Stores secure tokens for one-click feedback from digest emails
        # Token is HMAC-SHA256 of (user_id + item_id + secret + timestamp) - expires after 7 days
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS email_feedback_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                token TEXT NOT NULL UNIQUE,
                item_type TEXT NOT NULL,
                item_id INTEGER,
                scanned_item_id INTEGER,
                sender_identifier TEXT,
                source_type TEXT,
                feedback_action TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                used_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Index on token for fast lookup
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_feedback_token
            ON email_feedback_tokens(token)
        """)

        # Index on expires_at for cleanup queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_email_feedback_expires
            ON email_feedback_tokens(expires_at)
        """)

        # ignored_senders table - Stores senders the user wants to ignore in digests
        # Populated when user clicks "Ignore sender" in digest email
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ignored_senders (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                sender_identifier TEXT NOT NULL,
                ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, source_type, sender_identifier)
            )
        """)

        # Index on user_id for listing ignored senders
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ignored_senders_user
            ON ignored_senders(user_id)
        """)

        # nudge_suppressed_senders table - Senders whose nudges are auto-suppressed.
        # Unlike ignored_senders (which suppresses scanning entirely), this only
        # prevents nudge creation for a given sender while still scanning their messages.
        # Populated automatically when a user repeatedly dismisses nudges from the same sender.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nudge_suppressed_senders (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                sender_identifier TEXT NOT NULL,
                reason TEXT,
                suppressed_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, source_type, sender_identifier),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_nudge_suppressed_senders_user
            ON nudge_suppressed_senders(user_id)
        """)

        timings['email_feedback'] = time.time() - section_start
        print(f"[INIT_DB] Phase 18 Email Feedback: {timings['email_feedback']:.2f}s")

        # ============================================================================
        # SECTION: Usage Tracking
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Usage Tracking Tables (Cost Monitoring)
        # ============================================================================

        # Create usage_logs table - tracks token usage and costs per request
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                conversation_id TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cache_creation_tokens INTEGER DEFAULT 0,
                cache_read_tokens INTEGER DEFAULT 0,
                model TEXT,
                estimated_cost_usd REAL DEFAULT 0.0,
                tools_used TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
            )
        """)

        # Create indexes for usage analytics
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_usage_user_date ON usage_logs(user_id, created_at)
        """)

        timings['usage_tracking'] = time.time() - section_start
        print(f"[INIT_DB] Usage tracking: {timings['usage_tracking']:.2f}s")

        # ============================================================================
        # SECTION: Slack + Telegram
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Slack Integration Tables
        # ============================================================================

        # Create slack_tokens table - stores Slack OAuth user tokens
        # Supports multiple workspaces per user
        # Added bot_token and bot_user_id for DM chat functionality
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS slack_tokens (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                team_id TEXT NOT NULL,
                team_name TEXT NOT NULL,
                access_token TEXT NOT NULL,
                token_type TEXT DEFAULT 'user',
                scope TEXT NOT NULL,
                authed_user_id TEXT NOT NULL,
                authed_user_name TEXT,
                bot_token TEXT,
                bot_user_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, team_id)
            )
        """)

        # Create index on user_id for listing user's connected workspaces
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_slack_tokens_user ON slack_tokens(user_id)
        """)

        # ============================================================================
        # Telegram Integration Tables
        # ============================================================================

        # Create telegram_sessions table - stores Telegram MTProto session strings
        # Session strings are equivalent to being logged in - store securely
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_sessions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                phone_number TEXT NOT NULL,
                session_string TEXT NOT NULL,
                user_name TEXT,
                display_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, phone_number)
            )
        """)

        # Create index on user_id for listing user's connected Telegram accounts
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_sessions_user ON telegram_sessions(user_id)
        """)

        timings['slack_telegram'] = time.time() - section_start
        print(f"[INIT_DB] Phase 6 Slack+Telegram: {timings['slack_telegram']:.2f}s")

        # ============================================================================
        # SECTION: Browser History + Sync
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Browser History & Sync Tables
        # ============================================================================

        # Create browser_history table - stores synced browser history from local agents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS browser_history (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                machine_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                visit_time TIMESTAMP NOT NULL,
                visit_count INTEGER DEFAULT 1,
                domain TEXT,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, machine_id, url, visit_time)
            )
        """)

        # Create indexes for browser history queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_browser_history_user ON browser_history(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_browser_history_time ON browser_history(visit_time)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_browser_history_domain ON browser_history(domain)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_browser_history_user_time ON browser_history(user_id, visit_time DESC)
        """)

        # Create sync_status table - tracks sync state for local agents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                machine_id TEXT NOT NULL,
                sync_type TEXT NOT NULL,
                last_sync_time TIMESTAMP,
                last_sync_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, machine_id, sync_type)
            )
        """)

        timings['browser_sync'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 Browser+Sync: {timings['browser_sync']:.2f}s")

        # ============================================================================
        # SECTION: Local Files (tables + FTS)
        # ============================================================================
        section_start = time.time()
        subsection_start = time.time()

        # ============================================================================
        # Local Files Tables
        # ============================================================================

        # Create local_files table - stores indexed file metadata from desktop agents
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS local_files (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                machine_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_extension TEXT,
                file_size BIGINT,
                file_created TIMESTAMP,
                file_modified TIMESTAMP,
                content_preview TEXT,
                drive_letter TEXT,
                parent_folder TEXT,
                indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, machine_id, file_path)
            )
        """)
        print(f"[INIT_DB]   - local_files table: {time.time() - subsection_start:.2f}s")
        subsection_start = time.time()

        # Create indexes for local_files queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_local_files_user ON local_files(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_local_files_name ON local_files(file_name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_local_files_ext ON local_files(file_extension)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_local_files_modified ON local_files(file_modified)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_local_files_machine ON local_files(user_id, machine_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_local_files_deleted ON local_files(user_id, is_deleted)
        """)
        print(f"[INIT_DB]   - local_files indexes (6): {time.time() - subsection_start:.2f}s")
        subsection_start = time.time()

        timings['local_files'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 Local Files: {timings['local_files']:.2f}s")

        # ============================================================================
        # SECTION: Location History
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Location History Tables (07-05)
        # ============================================================================

        # Create location_history table - stores Google Takeout location data
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS location_history (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                accuracy INTEGER,
                timestamp TIMESTAMP NOT NULL,
                place_id TEXT,
                place_name TEXT,
                address TEXT,
                duration_minutes INTEGER,
                source TEXT,
                import_batch TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, timestamp, latitude, longitude)
            )
        """)

        # Create indexes for location queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_location_user ON location_history(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_location_time ON location_history(timestamp)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_location_place ON location_history(place_name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_location_user_time ON location_history(user_id, timestamp DESC)
        """)

        # Create location_import_log table - tracks imports to avoid duplicates
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS location_import_log (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                import_batch TEXT NOT NULL,
                file_name TEXT,
                records_imported INTEGER,
                date_range_start TIMESTAMP,
                date_range_end TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create index on user_id for listing user's imports
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_location_import_user ON location_import_log(user_id)
        """)

        timings['location'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 Location: {timings['location']:.2f}s")

        # ============================================================================
        # SECTION: Google Drive (tables + FTS)
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Google Drive Tables (07-06)
        # ============================================================================

        # Create drive_files table - index of Google Drive files
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS drive_files (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,
                file_id TEXT NOT NULL,
                name TEXT NOT NULL,
                mime_type TEXT,
                file_extension TEXT,
                size_bytes BIGINT,
                parent_id TEXT,
                path TEXT,
                created_time TIMESTAMP,
                modified_time TIMESTAMP,
                content_snippet TEXT,
                web_view_link TEXT,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email, file_id)
            )
        """)

        # Create indexes for Drive queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_user ON drive_files(user_id, google_email)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_name ON drive_files(name)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_modified ON drive_files(modified_time DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_drive_mime ON drive_files(mime_type)
        """)

        # Create drive_sync_status table - tracks sync progress
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS drive_sync_status (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,
                last_sync_at TIMESTAMP,
                files_synced INTEGER DEFAULT 0,
                change_token TEXT,
                sync_in_progress INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email)
            )
        """)

        timings['drive'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 Google Drive: {timings['drive']:.2f}s")

        # ============================================================================
        # SECTION: Google Contacts
        # ============================================================================
        section_start = time.time()

        # Create google_contacts table - cached contacts from Google People API
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS google_contacts (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,

                -- Google identifiers
                resource_name TEXT NOT NULL,
                etag TEXT,

                -- Name
                display_name TEXT,
                given_name TEXT,
                family_name TEXT,

                -- Contact info (JSON arrays)
                emails TEXT,
                phones TEXT,
                addresses TEXT,

                -- Organization
                company TEXT,
                job_title TEXT,

                -- Other
                notes TEXT,
                birthday TEXT,
                photo_url TEXT,

                -- Sync tracking
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email, resource_name)
            )
        """)

        # Indexes for contacts
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contacts_user ON google_contacts(user_id, google_email)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contacts_name ON google_contacts(display_name)
        """)

        # Create contacts_sync_status table - tracks sync progress
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts_sync_status (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,
                last_sync_at TIMESTAMP,
                contacts_synced INTEGER DEFAULT 0,
                sync_token TEXT,
                sync_in_progress INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email)
            )
        """)

        timings['contacts'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 Google Contacts: {timings['contacts']:.2f}s")

        # ============================================================================
        # SECTION: YouTube
        # ============================================================================
        section_start = time.time()

        # Create youtube_subscriptions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS youtube_subscriptions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,

                subscription_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_title TEXT,
                channel_description TEXT,
                thumbnail_url TEXT,

                subscribed_at TIMESTAMP,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email, subscription_id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_yt_subs_user ON youtube_subscriptions(user_id, google_email)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_yt_subs_channel ON youtube_subscriptions(channel_title)
        """)

        # Create youtube_playlists table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS youtube_playlists (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,

                playlist_id TEXT NOT NULL,
                title TEXT,
                description TEXT,
                thumbnail_url TEXT,
                item_count INTEGER,
                privacy_status TEXT,

                created_at TIMESTAMP,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email, playlist_id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_yt_playlists_user ON youtube_playlists(user_id, google_email)
        """)

        # Create youtube_liked_videos table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS youtube_liked_videos (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,

                video_id TEXT NOT NULL,
                title TEXT,
                description TEXT,
                channel_title TEXT,
                thumbnail_url TEXT,
                duration TEXT,

                published_at TIMESTAMP,
                liked_at TIMESTAMP,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email, video_id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_yt_liked_user ON youtube_liked_videos(user_id, google_email)
        """)

        # Create youtube_sync_status table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS youtube_sync_status (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,
                last_sync_at TIMESTAMP,
                subscriptions_synced INTEGER DEFAULT 0,
                playlists_synced INTEGER DEFAULT 0,
                liked_videos_synced INTEGER DEFAULT 0,
                sync_in_progress INTEGER DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email)
            )
        """)

        timings['youtube'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 YouTube: {timings['youtube']:.2f}s")

        # ============================================================================
        # SECTION: User Settings
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # User Settings Table
        # ============================================================================

        # Create user_settings table - stores user preferences (e.g., Claude model)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL UNIQUE,
                claude_model TEXT DEFAULT 'claude-sonnet-4-5-20250929',
                digest_enabled INTEGER DEFAULT 1,
                digest_time TEXT DEFAULT '07:00',
                digest_email INTEGER DEFAULT 1,
                digest_push INTEGER DEFAULT 1,
                digest_timezone TEXT DEFAULT 'America/Chicago',
                weekly_review_enabled INTEGER DEFAULT 1,
                weekly_review_day TEXT DEFAULT 'sunday',
                weekly_review_time TEXT DEFAULT '18:00',
                classification_model TEXT DEFAULT 'claude-haiku-4-5-20251001',
                nudge_enabled INTEGER DEFAULT 1,
                nudge_quiet_start TEXT DEFAULT '22:00',
                nudge_quiet_end TEXT DEFAULT '08:00',
                nudge_max_urgent_per_hour INTEGER DEFAULT 3,
                nudge_batch_interval_minutes INTEGER DEFAULT 60,
                nudge_channels TEXT DEFAULT '["push"]',
                nudge_last_batch_at TIMESTAMP,
                nudge_batch_channel TEXT DEFAULT 'push',
                nudge_drip_interval_minutes INTEGER DEFAULT 15,
                nudge_last_drip_at TIMESTAMP,
                nudge_quiet_skip_weekend INTEGER DEFAULT 0,
                nudge_smart_dedup INTEGER DEFAULT 1,
                slack_excluded_channels TEXT,
                telegram_excluded_chats TEXT,
                scanner_gmail_interval_minutes INTEGER DEFAULT 15,
                scanner_slack_interval_minutes INTEGER DEFAULT 120,
                scanner_telegram_interval_minutes INTEGER DEFAULT 5,
                scanner_calendar_interval_minutes INTEGER DEFAULT 60,
                classification_tier TEXT DEFAULT 'haiku',
                day_start_hour INTEGER DEFAULT 15,
                screen_agent_api_key TEXT,
                screen_cooldown_until DOUBLE PRECISION,
                screen_last_nudge_at DOUBLE PRECISION,
                pending_action_notification_channel TEXT DEFAULT 'none',
                default_calendar_email TEXT DEFAULT NULL,
                user_name TEXT DEFAULT NULL,
                user_pronouns_subject TEXT DEFAULT 'they',
                user_pronouns_object TEXT DEFAULT 'them',
                user_pronouns_possessive TEXT DEFAULT 'their',
                user_context TEXT DEFAULT NULL,
                key_people TEXT DEFAULT NULL,
                key_projects TEXT DEFAULT NULL,
                priorities TEXT DEFAULT NULL,
                setup_complete INTEGER DEFAULT 0,
                screen_agent_enabled INTEGER DEFAULT 0,
                browser_agent_enabled INTEGER DEFAULT 0,
                personality_casual INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create index on user_id for fast lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_settings_user ON user_settings(user_id)
        """)

        timings['user_settings'] = time.time() - section_start
        print(f"[INIT_DB] User settings: {timings['user_settings']:.2f}s")

        # ============================================================================
        # SECTION: Calendar Preferences (07-08)
        # ============================================================================
        section_start = time.time()

        # Create calendar_preferences table - stores per-calendar visibility settings
        # Enables multi-calendar support: show/hide calendars, remember preferences
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS calendar_preferences (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                google_email TEXT NOT NULL,

                -- Calendar identifiers
                calendar_id TEXT NOT NULL,
                calendar_name TEXT,

                -- Preferences
                is_visible INTEGER DEFAULT 1,
                color_override TEXT,

                -- Metadata (cached from Google)
                is_primary INTEGER DEFAULT 0,
                access_role TEXT,
                background_color TEXT,

                -- Timestamps
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(user_id, google_email, calendar_id)
            )
        """)

        # Create indexes for calendar preferences queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cal_prefs_user ON calendar_preferences(user_id, google_email)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cal_prefs_visible ON calendar_preferences(user_id, is_visible)
        """)

        timings['calendar_prefs'] = time.time() - section_start
        print(f"[INIT_DB] Phase 7 Calendar Preferences: {timings['calendar_prefs']:.2f}s")

        # ============================================================================
        # SECTION: Second Brain Tables (08-01)
        # ============================================================================
        section_start = time.time()

        # ============================================================================
        # Second Brain - People Database
        # ============================================================================

        # Create people table - relationship tracking (enhanced layer on Google Contacts)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS people (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                google_contact_id TEXT,
                context TEXT,
                notes TEXT,
                last_contact_date TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create index on user_id for listing user's people
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_people_user_id ON people(user_id)
        """)

        # Create people_followups table - follow-up items for people
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS people_followups (
                id BIGSERIAL PRIMARY KEY,
                person_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
            )
        """)

        # Create index on person_id for listing followups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_people_followups_person ON people_followups(person_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_people_followups_status ON people_followups(person_id, status)
        """)

        # ============================================================================
        # Second Brain - Projects Database
        # ============================================================================

        # Create projects table - active work with next actions (GTD style)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                next_action TEXT,
                notes TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create indexes for projects
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_projects_user_status ON projects(user_id, status)
        """)

        # ============================================================================
        # Second Brain - Ideas Database
        # ============================================================================

        # Create ideas table - captured insights
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ideas (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                summary TEXT,
                notes TEXT,
                tags TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create indexes for ideas
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_ideas_user_id ON ideas(user_id)
        """)

        # ============================================================================
        # Second Brain - Inbox Log (Audit Trail)
        # ============================================================================

        # Create inbox_log table - audit trail for all captures
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inbox_log (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                original_text TEXT NOT NULL,
                classification TEXT NOT NULL,
                confidence REAL,
                routed_to_table TEXT,
                routed_to_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Create indexes for inbox_log
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_inbox_log_user_id ON inbox_log(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_inbox_log_classification ON inbox_log(user_id, classification)
        """)

        timings['second_brain_tables'] = time.time() - section_start
        print(f"[INIT_DB] Phase 8 Second Brain Tables: {timings['second_brain_tables']:.2f}s")

        # ============================================================================
        # SECTION: Second Brain FTS (08-01)
        # ============================================================================
        section_start = time.time()

        timings['second_brain_fts'] = time.time() - section_start
        print(f"[INIT_DB] Phase 8 Second Brain FTS (removed — GIN indexes added at end): {timings['second_brain_fts']:.2f}s")

        # ============================================================================
        # SECTION: Scanner Engine & Entity Resolution
        # ============================================================================
        section_start = time.time()

        # scanner_runs - Tracks each scan execution
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scanner_runs (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                status TEXT DEFAULT 'running',
                items_found INTEGER DEFAULT 0,
                items_new INTEGER DEFAULT 0,
                error_message TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # scanned_items - Individual items discovered by scanners
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scanned_items (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                scanner_run_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_metadata TEXT,
                item_type TEXT NOT NULL,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                classification TEXT,
                classification_confidence REAL,
                processed INTEGER DEFAULT 0,
                UNIQUE(user_id, source, source_id),
                FOREIGN KEY (scanner_run_id) REFERENCES scanner_runs(id)
            )
        """)

        # entity_mappings - Cross-source identity resolution
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS entity_mappings (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                person_id INTEGER,
                source TEXT NOT NULL,
                source_identifier TEXT NOT NULL,
                display_name TEXT,
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, source, source_identifier),
                FOREIGN KEY (person_id) REFERENCES people(id)
            )
        """)

        # Indexes for scanner tables
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scanner_runs_user_source
            ON scanner_runs(user_id, source)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scanned_items_user_source
            ON scanned_items(user_id, source, detected_at)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_scanned_items_unprocessed
            ON scanned_items(processed) WHERE processed = 0
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_entity_mappings_person
            ON entity_mappings(person_id) WHERE person_id IS NOT NULL
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_entity_mappings_lookup
            ON entity_mappings(user_id, source, source_identifier)
        """)

        # Slack channel drip cursors — per-channel scan state and circuit breaker
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS slack_channel_cursors (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                team_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                channel_name TEXT,
                channel_type TEXT DEFAULT 'channel',
                is_excluded INTEGER DEFAULT 0,
                last_scan_ts TEXT,
                last_scan_at TIMESTAMP,
                last_error TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                circuit_state TEXT DEFAULT 'closed',
                circuit_opened_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, team_id, channel_id)
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_slack_channel_cursors_user_team
            ON slack_channel_cursors(user_id, team_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_slack_channel_cursors_last_scan
            ON slack_channel_cursors(user_id, last_scan_at ASC)
        """)
        print(f"[INIT_DB] Slack channel cursors: OK", flush=True)

        timings['scanner_tables'] = time.time() - section_start
        print(f"[INIT_DB] Phase 13 Scanner Tables: {timings['scanner_tables']:.2f}s")

        # ============================================================================
        # SECTION: Classification Tables
        # ============================================================================
        section_start = time.time()

        # item_classifications - AI classification output for scanned items
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS item_classifications (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                scanned_item_id INTEGER NOT NULL UNIQUE,
                relevance TEXT NOT NULL,
                urgency TEXT DEFAULT 'normal',
                summary TEXT,
                extracted_entities TEXT,
                extracted_actions TEXT,
                model_used TEXT,
                classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (scanned_item_id) REFERENCES scanned_items(id) ON DELETE CASCADE
            )
        """)

        # migration: thread context columns
        for col_def in [
            "ALTER TABLE item_classifications ADD COLUMN thread_context TEXT",
            "ALTER TABLE item_classifications ADD COLUMN thread_summary TEXT",
            "ALTER TABLE item_classifications ADD COLUMN thread_id TEXT",
        ]:
            try:
                cursor.execute(col_def)
            except Exception:
                pass  # column already exists

        # cross_references - Links scanned items to Second Brain entities
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cross_references (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                scanned_item_id INTEGER NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                relationship TEXT,
                confidence REAL DEFAULT 1.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scanned_item_id, entity_type, entity_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (scanned_item_id) REFERENCES scanned_items(id) ON DELETE CASCADE
            )
        """)

        # detected_actions - Potential actions surfaced by classification
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS detected_actions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                scanned_item_id INTEGER NOT NULL,
                action_text TEXT NOT NULL,
                action_type TEXT DEFAULT 'follow_up',
                person_name TEXT,
                person_id INTEGER,
                deadline TEXT,
                status TEXT DEFAULT 'pending',
                promoted_task_id INTEGER,
                detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (scanned_item_id) REFERENCES scanned_items(id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE SET NULL,
                FOREIGN KEY (promoted_task_id) REFERENCES tasks(id) ON DELETE SET NULL
            )
        """)

        # embedding_tracking - Tracks which entities have been embedded in ChromaDB
        # entity_id is TEXT (not INTEGER) to accommodate conversations.id which is TEXT.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS embedding_tracking (
                id BIGSERIAL PRIMARY KEY,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                embedded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                content_hash TEXT NOT NULL,
                UNIQUE(entity_type, entity_id, user_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_embedding_tracking_user
            ON embedding_tracking(user_id, entity_type)
        """)

        # Indexes for classification tables
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_item_classifications_user
            ON item_classifications(user_id, relevance)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_item_classifications_actionable
            ON item_classifications(user_id, classified_at) WHERE relevance = 'actionable'
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cross_references_entity
            ON cross_references(user_id, entity_type, entity_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_detected_actions_pending
            ON detected_actions(user_id, status) WHERE status = 'pending'
        """)

        timings['classification_tables'] = time.time() - section_start
        print(f"[INIT_DB] Phase 14 Classification Tables: {timings['classification_tables']:.2f}s")

        # ============================================================================
        # SECTION: Voice Interface
        # ============================================================================
        section_start = time.time()

        # voice_sessions - Tracks active voice conversations for context continuity
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS voice_sessions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                satellite_id TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                active INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Index for fast active session lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_voice_sessions_active
            ON voice_sessions(user_id, satellite_id, active)
            WHERE active = 1
        """)

        timings['voice_tables'] = time.time() - section_start
        print(f"[INIT_DB] Phase 9 Voice Tables: {timings['voice_tables']:.2f}s")

        # ============================================================================
        # SECTION: Activity Log (People Auto-Tracker)
        # ============================================================================
        section_start = time.time()

        # activity_log - Tracks automated changes to People tracker
        # Enables transparency, undo capability, and user oversight
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                context_added TEXT,
                source TEXT NOT NULL,
                source_context TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                deleted_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people(id) ON DELETE CASCADE
            )
        """)

        # Indexes for activity_log queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_log_user
            ON activity_log(user_id, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_log_person
            ON activity_log(person_id, created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_log_active
            ON activity_log(user_id, deleted_at)
            WHERE deleted_at IS NULL
        """)

        timings['activity_log'] = time.time() - section_start
        print(f"[INIT_DB] Phase 19 Activity Log: {timings['activity_log']:.2f}s")

        # ============================================================================
        # SECTION: Multi-Channel Chat
        # ============================================================================
        section_start = time.time()

        # telegram_bot_conversations - Links Telegram chat IDs to Seny conversations
        # Enables persistent conversation context across Telegram bot DMs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_bot_conversations (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                telegram_chat_id INTEGER NOT NULL,
                conversation_id TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                UNIQUE(user_id, telegram_chat_id)
            )
        """)

        # Indexes for telegram_bot_conversations queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_bot_conv_user
            ON telegram_bot_conversations(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_bot_conv_chat
            ON telegram_bot_conversations(telegram_chat_id)
        """)

        # telegram_bot_user_links - Links Telegram chat IDs to Seny user accounts
        # Used to identify users who message the bot
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_bot_user_links (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                telegram_chat_id INTEGER NOT NULL,
                telegram_username TEXT,
                telegram_first_name TEXT,
                linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(telegram_chat_id)
            )
        """)

        # Index for user link lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_telegram_bot_user_links_chat
            ON telegram_bot_user_links(telegram_chat_id)
        """)

        # slack_bot_conversations - Links Slack DM channels to Seny conversations
        # Enables persistent conversation context across Slack bot DMs
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS slack_bot_conversations (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                slack_channel_id TEXT NOT NULL,
                slack_user_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                last_message_ts TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                UNIQUE(user_id, slack_channel_id)
            )
        """)

        # Indexes for slack_bot_conversations queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_slack_bot_conv_user
            ON slack_bot_conversations(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_slack_bot_conv_channel
            ON slack_bot_conversations(slack_channel_id)
        """)

        # multichannel_chat_settings - User preferences for multi-channel chat
        # Controls whether Telegram/Slack bot chat is enabled for each user
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS multichannel_chat_settings (
                user_id INTEGER PRIMARY KEY,
                telegram_chat_enabled INTEGER DEFAULT 1,
                slack_chat_enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        timings['multichannel_chat'] = time.time() - section_start
        print(f"[INIT_DB] Phase 20 Multi-Channel Chat: {timings['multichannel_chat']:.2f}s")

        # ============================================================================
        # SECTION: Seny Memory System
        # ============================================================================
        section_start = time.time()

        # user_memories table - Stores persistent memories that Seny learns across conversations
        # Memories are loaded on every chat and injected into the system prompt
        # Categories: 'behavior' (how Seny should act), 'preference' (user likes/dislikes),
        #             'fact' (factual info about the user), 'general' (catch-all)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_memories (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                memory TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                created_at TEXT DEFAULT NOW(),
                conversation_id TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Index on user_id for fast memory retrieval on every chat
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_memories_user_id ON user_memories(user_id)
        """)

        timings['user_memories'] = time.time() - section_start
        print(f"[INIT_DB] Phase 02-04-01 Seny Memory System: {timings['user_memories']:.2f}s")

        # ============================================================================
        # SECTION: Priority Context Layer
        # ============================================================================
        section_start = time.time()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS priority_context (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                source TEXT DEFAULT 'chat',
                source_id TEXT,
                priority_level INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                due_at TEXT,
                resolved_at TEXT,
                created_at TEXT DEFAULT NOW(),
                updated_at TEXT DEFAULT NOW(),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_priority_context_user_status
            ON priority_context(user_id, status)
        """)

        timings['priority_context'] = time.time() - section_start
        print(f"[INIT_DB] Phase 38 Priority Context Layer: {timings['priority_context']:.2f}s")

        # ============================================================================
        # SECTION: Calendar Event Nudges
        # ============================================================================
        section_start = time.time()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS calendar_event_nudges (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                event_title TEXT NOT NULL,
                event_start TEXT NOT NULL,
                event_end TEXT,
                event_attendees TEXT,
                event_description TEXT,
                is_all_day INTEGER DEFAULT 0,
                offset_minutes INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                nudge_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (nudge_id) REFERENCES nudges(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cal_nudges_user_status
            ON calendar_event_nudges(user_id, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cal_nudges_scheduled
            ON calendar_event_nudges(scheduled_for) WHERE status = 'pending'
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_cal_nudges_event
            ON calendar_event_nudges(user_id, event_id)
        """)

        timings['calendar_event_nudges'] = time.time() - section_start
        print(f"[INIT_DB] Phase 39 Calendar Event Nudges: {timings['calendar_event_nudges']:.2f}s")

        # Reset sequences that may have drifted after SQLite→PG migration
        # (rows were inserted with explicit IDs; PG sequences don't auto-advance)
        _seq_tables = [
            'calendar_event_nudges',
        ]
        for _tbl in _seq_tables:
            try:
                cursor.execute(f"""
                    SELECT setval(
                        pg_get_serial_sequence('{_tbl}', 'id'),
                        COALESCE((SELECT MAX(id) FROM {_tbl}), 1)
                    )
                """)
                print(f"[INIT_DB] Sequence reset: {_tbl} OK")
            except Exception as _seq_err:
                print(f"[INIT_DB] Sequence reset: {_tbl} skipped ({repr(_seq_err)})")

        # ============================================================================
        # SECTION: Pending Actions Queue
        # ============================================================================
        #
        # content_json shapes by action_type:
        #   email_draft:       { "to": str, "cc": str|null, "subject": str, "body": str,
        #                        "thread_id": str|null, "gmail_account": str|null }
        #   calendar_proposal: { "title": str, "start_datetime": str, "end_datetime": str,
        #                        "location": str|null, "description": str|null, "calendar_id": str|null }
        #   task_proposal:     { "title": str, "description": str|null,
        #                        "due_date": str|null, "priority": str|null }
        #
        section_start = time.time()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content_json TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                source TEXT DEFAULT 'proactive',
                source_ref TEXT,
                notification_sent INTEGER DEFAULT 0,
                notification_channel TEXT,
                created_at TEXT DEFAULT NOW(),
                updated_at TEXT DEFAULT NOW(),
                resolved_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_actions_user_status
            ON pending_actions(user_id, status)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_actions_unnotified
            ON pending_actions(user_id, notification_sent) WHERE status = 'pending'
        """)

        timings['pending_actions'] = time.time() - section_start
        print(f"[INIT_DB] Phase 44 Pending Actions Queue: {timings['pending_actions']:.2f}s")

        # ============================================================================
        # SECTION: Screen Agent Dismissals (HF-13)
        # ============================================================================
        section_start = time.time()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS screen_agent_dismissals (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                dismissed_at TIMESTAMP NOT NULL DEFAULT NOW(),
                vision_status TEXT NOT NULL,
                user_reason TEXT,
                calendar_context TEXT,
                accepted BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_screen_dismissals_user_date
            ON screen_agent_dismissals(user_id, dismissed_at)
        """)

        timings['screen_agent_dismissals'] = time.time() - section_start
        print(f"[INIT_DB] HF-13 Screen Agent Dismissals: {timings['screen_agent_dismissals']:.2f}s")

        # ============================================================================
        # SECTION: Screen Nudge Messages (HF-14)
        # ============================================================================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS screen_nudge_messages (
                telegram_message_id BIGINT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                sent_at DOUBLE PRECISION NOT NULL
            )
        """)
        print("[INIT_DB] HF-14 Screen Nudge Messages: OK")

        # ============================================================================
        # SECTION: User Status
        # ============================================================================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_status (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL,
                status_text TEXT NOT NULL,
                set_at TIMESTAMPTZ DEFAULT NOW(),
                expires_at TIMESTAMPTZ
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_status_user_id ON user_status(user_id)
        """)
        print("[INIT_DB] User Status table: OK")

        # ============================================================================
        # SECTION: LCD (Living Context Document) tables
        # ============================================================================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lcd_layer1 (
                id SERIAL PRIMARY KEY,
                user_id INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content TEXT NOT NULL DEFAULT '',
                layer2_synthesis TEXT NOT NULL DEFAULT '',
                layer2_synthesized_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lcd_observation_log (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_lcd_observation_log_user_created
                ON lcd_observation_log(user_id, created_at DESC)
        """)
        print("[INIT_DB] LCD tables: OK")

        # ============================================================================
        # SECTION: NightlyResearchService Audit History
        # ============================================================================
        section_start = time.time()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS research_audit_runs (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                run_at TIMESTAMP NOT NULL DEFAULT NOW(),
                fidelity_score FLOAT,
                negative_unabsorbed_count INTEGER DEFAULT 0,
                suppression_gap_count INTEGER DEFAULT 0,
                signals_json TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_audit_runs_user
            ON research_audit_runs(user_id, run_at DESC)
        """)

        timings['research_audit_runs'] = time.time() - section_start
        print(f"[INIT_DB] Phase 60 Research Audit Runs: {timings['research_audit_runs']:.2f}s")

        # ============================================================================
        # SECTION: Dismissed Duplicates (Phase 78)
        # ============================================================================
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS dismissed_duplicates (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                id_a INTEGER NOT NULL,
                id_b INTEGER NOT NULL,
                dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, category, id_a, id_b),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # ============================================================================
        # SECTION: GIN Indexes (pg_trgm text search)
        # ============================================================================
        gin_indexes = [
            "CREATE INDEX IF NOT EXISTS idx_messages_content_trgm ON messages USING GIN (content gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_notes_title_trgm ON notes USING GIN (title gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_notes_content_trgm ON notes USING GIN (content gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_local_files_path_trgm ON local_files USING GIN (file_path gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_local_files_content_trgm ON local_files USING GIN (content_preview gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_people_name_trgm ON people USING GIN (name gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_projects_name_trgm ON projects USING GIN (name gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_ideas_title_trgm ON ideas USING GIN (title gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_google_contacts_name_trgm ON google_contacts USING GIN (display_name gin_trgm_ops)",
            "CREATE INDEX IF NOT EXISTS idx_drive_files_name_trgm ON drive_files USING GIN (name gin_trgm_ops)",
        ]
        for idx_sql in gin_indexes:
            try:
                cursor.execute(idx_sql)
            except Exception as e:
                print(f"[INIT_DB] GIN index skipped: {e}")

        # ============================================================================
        # SECTION: Commit
        # ============================================================================
        section_start = time.time()

        # Commit changes
        conn.commit()

        timings['commit'] = time.time() - section_start
        print(f"[INIT_DB] Commit: {timings['commit']:.2f}s")

        # ============================================================================
        # Timing Summary
        # ============================================================================
        total_time = time.time() - init_start
        print(f"\n[INIT_DB] === TIMING SUMMARY ===")
        for section, duration in sorted(timings.items(), key=lambda x: -x[1]):
            pct = (duration / total_time) * 100 if total_time > 0 else 0
            print(f"[INIT_DB]   {section}: {duration:.2f}s ({pct:.1f}%)")
        print(f"[INIT_DB] TOTAL: {total_time:.2f}s")
        print(f"[INIT_DB] ======================\n")

        print("✓ Database initialized (PostgreSQL)")

    except Exception as e:
        print(f"✗ Database initialization error: {e}")
        conn.rollback()
        raise

    finally:
        _pool.putconn(conn)


@contextmanager
def get_db():
    """
    Context manager for PostgreSQL database connections.
    Automatically handles connection lifecycle and commits/rollbacks.

    Usage:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users")
            users = cursor.fetchall()
    """
    pool = _get_pg_pool()
    conn = pool.getconn()
    conn.autocommit = False
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def update_heartbeat(subsystem: str, error: str = None) -> None:
    """Record that a background job just ran. Called at the end of each monitored job."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO system_heartbeats (subsystem, last_run_at, last_error, updated_at)
                VALUES (%s, NOW(), %s, NOW())
                ON CONFLICT (subsystem) DO UPDATE
                SET last_run_at = NOW(), last_error = %s, updated_at = NOW()
            """, (subsystem, error, error))
    except Exception:
        pass  # Never let heartbeat failure propagate


def get_system_health() -> list:
    """Return last heartbeat for all monitored subsystems."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT subsystem, last_run_at, last_error, last_alerted_at, updated_at
                FROM system_heartbeats
                WHERE subsystem != '_alert_dedup'
                ORDER BY subsystem
            """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def create_user(email: str, hashed_password: str) -> Optional[int]:
    """
    Create a new user in the database.

    Args:
        email: User's email address (must be unique)
        hashed_password: Bcrypt-hashed password (never store plaintext!)

    Returns:
        User ID if successful, None if email already exists

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Email uniqueness enforced by database constraint
        - Only accepts pre-hashed passwords (never hashes here)
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # SECURITY: Use parameterized query to prevent SQL injection
            # NEVER use string concatenation for SQL queries!
            cursor.execute(
                "INSERT INTO users (email, hashed_password) VALUES (%s, %s) RETURNING id",
                (email, hashed_password)
            )

            # Return the ID of the newly created user
            row = cursor.fetchone()
            user_id = row['id'] if row else None

            # Create user_settings row with defaults so setup wizard works immediately
            if user_id is not None:
                cursor.execute(
                    "INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
                    (user_id,)
                )

            return user_id

    except Exception as e:
        if "unique" not in str(e).lower() and "duplicate" not in str(e).lower() and "already exists" not in str(e).lower():
            raise
        # Email already exists (UNIQUE constraint violation)
        return None


def get_user_by_email(email: str) -> Optional[dict]:
    """
    Retrieve a user by email address.

    Args:
        email: User's email address

    Returns:
        User dictionary with keys: id, email, hashed_password, created_at
        None if user not found

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "SELECT id, email, hashed_password, created_at FROM users WHERE email = %s",
            (email,)
        )

        row = cursor.fetchone()

        if row is None:
            return None

        # Convert Row object to dictionary
        return {
            "id": row["id"],
            "email": row["email"],
            "hashed_password": row["hashed_password"],
            "created_at": row["created_at"]
        }


def get_user_by_id(user_id: int) -> Optional[dict]:
    """
    Retrieve a user by ID.

    Args:
        user_id: User's unique identifier

    Returns:
        User dictionary with keys: id, email, hashed_password, created_at
        None if user not found

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "SELECT id, email, hashed_password, created_at FROM users WHERE id = %s",
            (user_id,)
        )

        row = cursor.fetchone()

        if row is None:
            return None

        # Convert Row object to dictionary
        return {
            "id": row["id"],
            "email": row["email"],
            "hashed_password": row["hashed_password"],
            "created_at": row["created_at"]
        }


def create_conversation(user_id: int, conversation_id: str, title: Optional[str] = None) -> str:
    """
    Create a new conversation for a user.

    Args:
        user_id: User's unique identifier
        conversation_id: UUID for the conversation
        title: Optional title for the conversation

    Returns:
        The conversation ID

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, %s)",
            (conversation_id, user_id, title)
        )

        return conversation_id


def get_conversation(conversation_id: str) -> Optional[dict]:
    """
    Retrieve a conversation by ID.

    Args:
        conversation_id: The conversation's unique identifier

    Returns:
        Conversation dictionary with keys: id, user_id, model, created_at, updated_at
        None if conversation not found

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "SELECT id, user_id, model, created_at, updated_at FROM conversations WHERE id = %s",
            (conversation_id,)
        )

        row = cursor.fetchone()

        if row is None:
            return None

        # Convert Row object to dictionary
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "model": row["model"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"]
        }


def get_user_conversations(user_id: int) -> list[dict]:
    """
    Retrieve all conversations for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of conversation dictionaries with id, title, model, created_at, updated_at

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "SELECT id, user_id, title, model, created_at, updated_at FROM conversations WHERE user_id = %s ORDER BY updated_at DESC",
            (user_id,)
        )

        rows = cursor.fetchall()

        # Convert Row objects to dictionaries
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "model": row["model"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
            for row in rows
        ]


def save_message(conversation_id: str, role: str, content: str) -> None:
    """
    Save a message to a conversation.

    Args:
        conversation_id: The conversation's unique identifier
        role: Message role ('user' or 'assistant')
        content: Message content

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            (conversation_id, role, content)
        )

        # Update the conversation's updated_at timestamp
        cursor.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (conversation_id,)
        )


def get_conversation_messages(conversation_id: str) -> list[dict]:
    """
    Retrieve all messages for a conversation.

    Args:
        conversation_id: The conversation's unique identifier

    Returns:
        List of message dictionaries with keys: id, conversation_id, role, content, created_at

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "SELECT id, conversation_id, role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY created_at ASC",
            (conversation_id,)
        )

        rows = cursor.fetchall()

        # Convert Row objects to dictionaries
        # Return messages in Claude API format (role and content only)
        return [
            {
                "role": row["role"],
                "content": row["content"]
            }
            for row in rows
        ]


def update_conversation_title(conversation_id: str, title: str) -> bool:
    """
    Update the title of a conversation.

    Args:
        conversation_id: The conversation's unique identifier
        title: The new title for the conversation

    Returns:
        True if updated, False if conversation not found

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query to prevent SQL injection
        cursor.execute(
            "UPDATE conversations SET title = %s WHERE id = %s",
            (title, conversation_id)
        )

        return cursor.rowcount > 0


def update_conversation_model(conversation_id: str, model: str, user_id: int) -> bool:
    """
    Update the model of a conversation.

    Args:
        conversation_id: The conversation's unique identifier
        model: The Claude model ID to use for this conversation
        user_id: User's unique identifier (for ownership verification)

    Returns:
        True if updated, False if conversation not found or not owned by user

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Verifies user ownership before update
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Use parameterized query and verify ownership
        cursor.execute(
            "UPDATE conversations SET model = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND user_id = %s",
            (model, conversation_id, user_id)
        )

        return cursor.rowcount > 0


def delete_conversation(conversation_id: str, user_id: int) -> bool:
    """
    Delete a conversation and all its messages.

    Args:
        conversation_id: The conversation's unique identifier
        user_id: User's unique identifier (for ownership verification)

    Returns:
        True if deleted, False if not found or not owned by user

    Security:
        - Uses parameterized queries to prevent SQL injection
        - MUST verify user_id ownership before delete
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Verify ownership before delete - include user_id in WHERE clause
        # This prevents users from deleting other users' conversations
        cursor.execute(
            "DELETE FROM conversations WHERE id = %s AND user_id = %s",
            (conversation_id, user_id)
        )

        # rowcount > 0 means conversation existed and was owned by user
        return cursor.rowcount > 0


def get_conversation_with_messages(conversation_id: str, user_id: int) -> Optional[dict]:
    """
    Retrieve a conversation with all its messages.

    Args:
        conversation_id: The conversation's unique identifier
        user_id: User's unique identifier (for ownership verification)

    Returns:
        Conversation dictionary with messages and model, or None if not found/not owned

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Verifies user_id ownership
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # SECURITY: Verify ownership - include user_id in WHERE clause
        cursor.execute(
            "SELECT id, user_id, title, model, created_at, updated_at FROM conversations WHERE id = %s AND user_id = %s",
            (conversation_id, user_id)
        )

        conv_row = cursor.fetchone()

        if conv_row is None:
            return None

        # Get all messages for this conversation
        cursor.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY created_at ASC",
            (conversation_id,)
        )

        message_rows = cursor.fetchall()

        return {
            "id": conv_row["id"],
            "user_id": conv_row["user_id"],
            "title": conv_row["title"],
            "model": conv_row["model"],
            "created_at": conv_row["created_at"],
            "updated_at": conv_row["updated_at"],
            "messages": [
                {
                    "role": row["role"],
                    "content": row["content"],
                    "created_at": row["created_at"]
                }
                for row in message_rows
            ]
        }


def search_user_conversations(user_id: int, query: str, limit: int = 10) -> list[dict]:
    """
    Search a user's conversations using full-text search.

    Uses FTS5 MATCH for fast, stemmed search. Returns conversations with
    matching snippets showing the context of the match.

    Args:
        user_id: User's unique identifier (SECURITY: mandatory filter)
        query: Search query string
        limit: Maximum number of results (default 10)

    Returns:
        List of matching conversation dicts with:
        - conversation_id: The conversation's UUID
        - title: Conversation title
        - snippet: Highlighted excerpt showing match context
        - updated_at: When conversation was last active

    Security:
        - Uses parameterized queries to prevent SQL injection
        - ALWAYS filters by user_id - users can only search their own conversations
    """
    if not query or not query.strip():
        return []

    original_query = query.strip()
    search_pattern = f'%{original_query}%'

    with get_db() as conn:
        cursor = conn.cursor()

        try:
            # SECURITY: Always include user_id filter - mandatory
            # Uses ILIKE for case-insensitive search (pg_trgm compatible)
            # DISTINCT because multiple messages in same conversation may match
            cursor.execute("""
                SELECT DISTINCT
                    c.id as conversation_id,
                    c.title,
                    m.content,
                    c.updated_at
                FROM messages m
                JOIN conversations c ON c.id = m.conversation_id
                WHERE m.content ILIKE %s
                AND c.user_id = %s
                ORDER BY c.updated_at DESC
                LIMIT %s
            """, (search_pattern, user_id, limit))
        except Exception:
            # If query fails, return empty results rather than crashing
            return []

        rows = cursor.fetchall()

        results = []
        for row in rows:
            row_dict = dict(row)
            row_dict['snippet'] = extract_snippet(row_dict.get('content', '') or '', original_query)
            results.append({
                "conversation_id": row_dict["conversation_id"],
                "title": row_dict["title"] or "Untitled conversation",
                "snippet": row_dict["snippet"],
                "updated_at": row_dict["updated_at"]
            })
        return results


# ============================================================================
# Google Token Management - Multi-Account Support
# Supports Gmail + Calendar combined OAuth
# ============================================================================

def save_google_token(user_id: int, email: str, credentials) -> None:
    """
    Save or update Google OAuth credentials for a specific email account.

    Args:
        user_id: User's unique identifier
        email: Google account email being connected
        credentials: google.oauth2.credentials.Credentials object

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Tokens are stored per-user per-email, ensuring data isolation
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Convert expiry datetime to string if present
        expiry_str = None
        if credentials.expiry:
            expiry_str = credentials.expiry.isoformat()

        # Convert scopes list to comma-separated string
        scopes_str = ','.join(credentials.scopes) if credentials.scopes else ''

        # Check if this email already exists (update) or is new (insert)
        cursor.execute("SELECT id FROM google_tokens WHERE email = %s", (email,))
        existing = cursor.fetchone()

        if existing:
            # Update existing token
            cursor.execute("""
                UPDATE google_tokens
                SET access_token = %s, refresh_token = %s, token_uri = %s,
                    scopes = %s, expiry = %s, updated_at = CURRENT_TIMESTAMP
                WHERE email = %s AND user_id = %s
            """, (
                credentials.token,
                credentials.refresh_token,
                credentials.token_uri,
                scopes_str,
                expiry_str,
                email,
                user_id
            ))
        else:
            # Insert new token
            cursor.execute("""
                INSERT INTO google_tokens
                (user_id, email, access_token, refresh_token, token_uri, scopes, expiry)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id,
                email,
                credentials.token,
                credentials.refresh_token,
                credentials.token_uri,
                scopes_str,
                expiry_str
            ))


def get_google_token(user_id: int, email: str) -> Optional[dict]:
    """
    Retrieve Google OAuth token data for a specific email account.

    Args:
        user_id: User's unique identifier
        email: Google account email to get token for

    Returns:
        Dictionary with token fields, or None if no token exists

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and email for access control
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT email, access_token, refresh_token, token_uri, scopes, expiry
            FROM google_tokens
            WHERE user_id = %s AND email = %s
        """, (user_id, email))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "email": row["email"],
            "access_token": row["access_token"],
            "refresh_token": row["refresh_token"],
            "token_uri": row["token_uri"],
            "scopes": row["scopes"],
            "expiry": row["expiry"]
        }


def list_google_tokens(user_id: int) -> list[dict]:
    """
    List all connected Google accounts for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dictionaries with email, scopes, and created_at for each connected account

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT email, scopes, created_at
            FROM google_tokens
            WHERE user_id = %s
            ORDER BY created_at ASC
        """, (user_id,))

        rows = cursor.fetchall()

        return [
            {
                "email": row["email"],
                "scopes": row["scopes"],
                "created_at": row["created_at"]
            }
            for row in rows
        ]


def list_all_google_accounts() -> list[dict]:
    """
    List all Google accounts across all users.

    Used for background sync tasks.

    Returns:
        List of dictionaries with user_id and email for each connected account
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT DISTINCT user_id, email
            FROM google_tokens
            ORDER BY user_id, email
        """)

        rows = cursor.fetchall()

        return [
            {
                "user_id": row["user_id"],
                "email": row["email"]
            }
            for row in rows
        ]


def delete_google_token(user_id: int, email: str) -> bool:
    """
    Remove Google OAuth token for a specific email account.

    Args:
        user_id: User's unique identifier
        email: Google account email to disconnect

    Returns:
        True if token was deleted, False if no token existed

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and email to prevent unauthorized deletion
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM google_tokens WHERE user_id = %s AND email = %s
        """, (user_id, email))

        return cursor.rowcount > 0


# Backward compatibility aliases (for existing Gmail code)
save_gmail_token = save_google_token
get_gmail_token = get_google_token
list_gmail_tokens = list_google_tokens
delete_gmail_token = delete_google_token


# ============================================================================
# Calendar Preferences Functions (07-08: Multi-Calendar Support)
# ============================================================================

def get_calendar_preferences(user_id: int, google_email: str) -> list[dict]:
    """
    Get calendar preferences for a user's Google account.

    Args:
        user_id: User's unique identifier
        google_email: Google account email

    Returns:
        List of calendar preference dicts with visibility, color, etc.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT calendar_id, calendar_name, is_visible, color_override,
                   is_primary, access_role, background_color,
                   created_at, updated_at
            FROM calendar_preferences
            WHERE user_id = %s AND google_email = %s
            ORDER BY is_primary DESC, calendar_name ASC
        """, (user_id, google_email))

        rows = cursor.fetchall()

        return [
            {
                "calendar_id": row["calendar_id"],
                "calendar_name": row["calendar_name"],
                "is_visible": bool(row["is_visible"]),
                "color_override": row["color_override"],
                "is_primary": bool(row["is_primary"]),
                "access_role": row["access_role"],
                "background_color": row["background_color"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
            for row in rows
        ]


def save_calendar_preference(
    user_id: int,
    google_email: str,
    calendar_id: str,
    calendar_name: str = None,
    is_visible: bool = True,
    is_primary: bool = False,
    access_role: str = None,
    background_color: str = None,
    color_override: str = None
) -> None:
    """
    Save or update a calendar preference.

    Args:
        user_id: User's unique identifier
        google_email: Google account email
        calendar_id: Google calendar ID
        calendar_name: Display name of the calendar
        is_visible: Whether calendar is visible in UI and queries
        is_primary: Whether this is the primary calendar
        access_role: User's access role (owner, writer, reader)
        background_color: Google's default color for this calendar
        color_override: User's custom color override
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO calendar_preferences
                (user_id, google_email, calendar_id, calendar_name, is_visible,
                 is_primary, access_role, background_color, color_override)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(user_id, google_email, calendar_id) DO UPDATE SET
                calendar_name = excluded.calendar_name,
                is_visible = excluded.is_visible,
                is_primary = excluded.is_primary,
                access_role = excluded.access_role,
                background_color = excluded.background_color,
                color_override = COALESCE(calendar_preferences.color_override, excluded.color_override),
                updated_at = CURRENT_TIMESTAMP
        """, (
            user_id, google_email, calendar_id, calendar_name,
            1 if is_visible else 0,
            1 if is_primary else 0,
            access_role, background_color, color_override
        ))


def update_calendar_visibility(
    user_id: int,
    google_email: str,
    calendar_id: str,
    is_visible: bool
) -> bool:
    """
    Toggle calendar visibility.

    Args:
        user_id: User's unique identifier
        google_email: Google account email
        calendar_id: Google calendar ID
        is_visible: Whether calendar should be visible

    Returns:
        True if preference was updated, False if calendar not found
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE calendar_preferences
            SET is_visible = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND google_email = %s AND calendar_id = %s
        """, (1 if is_visible else 0, user_id, google_email, calendar_id))

        return cursor.rowcount > 0


def update_calendar_visibility_by_id(
    user_id: int,
    calendar_id: str,
    is_visible: bool
) -> bool:
    """
    Toggle calendar visibility by calendar_id only (finds the account automatically).

    Args:
        user_id: User's unique identifier
        calendar_id: Google calendar ID
        is_visible: Whether calendar should be visible

    Returns:
        True if preference was updated, False if calendar not found
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE calendar_preferences
            SET is_visible = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND calendar_id = %s
        """, (1 if is_visible else 0, user_id, calendar_id))

        return cursor.rowcount > 0


def get_visible_calendar_ids(user_id: int, google_email: str) -> list[str]:
    """
    Get list of visible calendar IDs for a user.

    Args:
        user_id: User's unique identifier
        google_email: Google account email

    Returns:
        List of calendar IDs that are marked visible
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT calendar_id
            FROM calendar_preferences
            WHERE user_id = %s AND google_email = %s AND is_visible = 1
        """, (user_id, google_email))

        return [row["calendar_id"] for row in cursor.fetchall()]


def delete_calendar_preferences(user_id: int, google_email: str) -> int:
    """
    Delete all calendar preferences for a Google account.

    Used when disconnecting a Google account.

    Args:
        user_id: User's unique identifier
        google_email: Google account email

    Returns:
        Number of preferences deleted
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM calendar_preferences
            WHERE user_id = %s AND google_email = %s
        """, (user_id, google_email))

        return cursor.rowcount


# ============================================================================
# Microsoft OAuth Token Functions
# ============================================================================

def save_microsoft_token(user_id: int, email: str, token_data: dict) -> None:
    """
    Save or update Microsoft OAuth credentials for a specific email account.

    Args:
        user_id: User's unique identifier
        email: Microsoft account email being connected
        token_data: Dictionary with access_token, refresh_token, scopes, expiry, account_type

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Tokens are stored per-user per-email, ensuring data isolation
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Convert scopes list to space-separated string (Microsoft uses spaces)
        scopes_str = token_data.get('scopes', '')
        if isinstance(scopes_str, list):
            scopes_str = ' '.join(scopes_str)

        # Check if this email already exists for this user (update) or is new (insert)
        cursor.execute(
            "SELECT id FROM microsoft_tokens WHERE user_id = %s AND email = %s",
            (user_id, email)
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing token
            cursor.execute("""
                UPDATE microsoft_tokens
                SET access_token = %s, refresh_token = %s, scopes = %s, expiry = %s,
                    account_type = %s, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND email = %s
            """, (
                token_data['access_token'],
                token_data['refresh_token'],
                scopes_str,
                token_data.get('expiry'),
                token_data.get('account_type', 'unknown'),
                user_id,
                email
            ))
        else:
            # Insert new token
            cursor.execute("""
                INSERT INTO microsoft_tokens
                (user_id, email, access_token, refresh_token, scopes, expiry, account_type)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id,
                email,
                token_data['access_token'],
                token_data['refresh_token'],
                scopes_str,
                token_data.get('expiry'),
                token_data.get('account_type', 'unknown')
            ))


def get_microsoft_token(user_id: int, email: str = None) -> Optional[dict]:
    """
    Retrieve Microsoft OAuth token data for a specific email account.

    Args:
        user_id: User's unique identifier
        email: Microsoft account email to get token for (if None, returns first account)

    Returns:
        Dictionary with token fields, or None if no token exists

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires user_id for access control
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if email:
            cursor.execute("""
                SELECT email, access_token, refresh_token, token_uri, scopes, expiry, account_type
                FROM microsoft_tokens
                WHERE user_id = %s AND email = %s
            """, (user_id, email))
        else:
            # Get first connected account
            cursor.execute("""
                SELECT email, access_token, refresh_token, token_uri, scopes, expiry, account_type
                FROM microsoft_tokens
                WHERE user_id = %s
                ORDER BY created_at ASC
                LIMIT 1
            """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "email": row["email"],
            "access_token": row["access_token"],
            "refresh_token": row["refresh_token"],
            "token_uri": row["token_uri"],
            "scopes": row["scopes"],
            "expiry": row["expiry"],
            "account_type": row["account_type"]
        }


def list_microsoft_tokens(user_id: int) -> list[dict]:
    """
    List all connected Microsoft accounts for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dictionaries with email, scopes, account_type, and created_at
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT email, scopes, account_type, created_at
            FROM microsoft_tokens
            WHERE user_id = %s
            ORDER BY created_at ASC
        """, (user_id,))

        rows = cursor.fetchall()

        return [
            {
                "email": row["email"],
                "scopes": row["scopes"],
                "account_type": row["account_type"],
                "created_at": row["created_at"]
            }
            for row in rows
        ]


def delete_microsoft_token(user_id: int, email: str) -> bool:
    """
    Remove Microsoft OAuth token for a specific email account.

    Args:
        user_id: User's unique identifier
        email: Microsoft account email to disconnect

    Returns:
        True if token was deleted, False if no token existed

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and email to prevent unauthorized deletion
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM microsoft_tokens WHERE user_id = %s AND email = %s
        """, (user_id, email))

        return cursor.rowcount > 0


def update_microsoft_token(user_id: int, email: str, access_token: str, expiry: str = None) -> bool:
    """
    Update just the access token and expiry for a Microsoft account.
    Used when refreshing tokens.

    Args:
        user_id: User's unique identifier
        email: Microsoft account email
        access_token: New access token
        expiry: New expiry datetime (ISO 8601)

    Returns:
        True if token was updated, False if no token existed
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE microsoft_tokens
            SET access_token = %s, expiry = %s, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = %s AND email = %s
        """, (access_token, expiry, user_id, email))

        return cursor.rowcount > 0


# ============================================================================
# Usage Tracking Functions
# ============================================================================

# Pricing per million tokens (as of January 2026)
# Model: Claude Sonnet 3.5/4
PRICING = {
    "input": 3.00,           # $3 per million input tokens
    "output": 15.00,         # $15 per million output tokens
    "cache_write": 3.75,     # $3.75 per million cache write tokens (1.25x input)
    "cache_read": 0.30,      # $0.30 per million cache read tokens (0.1x input)
}


def log_usage(
    user_id: int,
    conversation_id: str = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    model: str = None,
    tools_used: list = None
) -> int:
    """
    Log token usage for a request and calculate estimated cost.

    Args:
        user_id: User's unique identifier
        conversation_id: Optional conversation ID
        input_tokens: Non-cached input tokens
        output_tokens: Output tokens generated
        cache_creation_tokens: Tokens written to cache
        cache_read_tokens: Tokens read from cache
        model: Model name used
        tools_used: List of tool names used

    Returns:
        The ID of the created usage log entry
    """
    # Calculate estimated cost
    cost = (
        (input_tokens / 1_000_000) * PRICING["input"] +
        (output_tokens / 1_000_000) * PRICING["output"] +
        (cache_creation_tokens / 1_000_000) * PRICING["cache_write"] +
        (cache_read_tokens / 1_000_000) * PRICING["cache_read"]
    )

    tools_str = ",".join(tools_used) if tools_used else None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO usage_logs
            (user_id, conversation_id, input_tokens, output_tokens,
             cache_creation_tokens, cache_read_tokens, model,
             estimated_cost_usd, tools_used)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (user_id, conversation_id, input_tokens, output_tokens,
              cache_creation_tokens, cache_read_tokens, model,
              cost, tools_str))
        row = cursor.fetchone()

        return row['id'] if row else None


def get_usage_summary(user_id: int, days: int = 7) -> dict:
    """
    Get usage summary for a user over the specified number of days.

    Args:
        user_id: User's unique identifier
        days: Number of days to look back (default 7)

    Returns:
        Dictionary with usage statistics:
        - total_requests: Number of API calls
        - total_input_tokens: Sum of input tokens
        - total_output_tokens: Sum of output tokens
        - total_cache_read_tokens: Sum of cache read tokens
        - total_cache_write_tokens: Sum of cache write tokens
        - total_cost_usd: Estimated total cost
        - cache_hit_rate: Percentage of tokens that were cache reads
        - daily_breakdown: List of daily usage
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Get totals
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cursor.execute("""
            SELECT
                COUNT(*) as total_requests,
                COALESCE(SUM(input_tokens), 0) as total_input,
                COALESCE(SUM(output_tokens), 0) as total_output,
                COALESCE(SUM(cache_creation_tokens), 0) as total_cache_write,
                COALESCE(SUM(cache_read_tokens), 0) as total_cache_read,
                COALESCE(SUM(estimated_cost_usd), 0) as total_cost
            FROM usage_logs
            WHERE user_id = %s
            AND created_at >= %s
        """, (user_id, cutoff))

        row = cursor.fetchone()
        totals = {
            "total_requests": row["total_requests"],
            "total_input_tokens": row["total_input"],
            "total_output_tokens": row["total_output"],
            "total_cache_write_tokens": row["total_cache_write"],
            "total_cache_read_tokens": row["total_cache_read"],
            "total_cost_usd": round(row["total_cost"], 4)
        }

        # Calculate cache hit rate
        total_input = totals["total_input_tokens"] + totals["total_cache_read_tokens"] + totals["total_cache_write_tokens"]
        if total_input > 0:
            totals["cache_hit_rate"] = round(totals["total_cache_read_tokens"] / total_input * 100, 1)
        else:
            totals["cache_hit_rate"] = 0.0

        # Get daily breakdown
        cursor.execute("""
            SELECT
                DATE(created_at) as date,
                COUNT(*) as requests,
                COALESCE(SUM(input_tokens + cache_creation_tokens + cache_read_tokens), 0) as input_tokens,
                COALESCE(SUM(output_tokens), 0) as output_tokens,
                COALESCE(SUM(estimated_cost_usd), 0) as cost
            FROM usage_logs
            WHERE user_id = %s
            AND created_at >= %s
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """, (user_id, cutoff))

        daily = []
        for row in cursor.fetchall():
            daily.append({
                "date": str(row["date"]),
                "requests": row["requests"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cost_usd": round(row["cost"], 4)
            })

        totals["daily_breakdown"] = daily
        return totals


def get_recent_usage(user_id: int, limit: int = 20) -> list:
    """
    Get recent usage log entries for a user.

    Args:
        user_id: User's unique identifier
        limit: Maximum entries to return

    Returns:
        List of recent usage entries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                id, conversation_id, input_tokens, output_tokens,
                cache_creation_tokens, cache_read_tokens, model,
                estimated_cost_usd, tools_used, created_at
            FROM usage_logs
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, limit))

        results = []
        for row in cursor.fetchall():
            results.append({
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "cache_creation_tokens": row["cache_creation_tokens"],
                "cache_read_tokens": row["cache_read_tokens"],
                "model": row["model"],
                "estimated_cost_usd": row["estimated_cost_usd"],
                "tools_used": row["tools_used"].split(",") if row["tools_used"] else [],
                "created_at": row["created_at"]
            })
        return results


# ============================================================================
# Slack Token Management - Multi-Workspace Support
# ============================================================================

def save_slack_token(
    user_id: int,
    team_id: str,
    team_name: str,
    access_token: str,
    scope: str,
    authed_user_id: str,
    authed_user_name: str = None,
    token_type: str = "user",
    bot_token: str = None,
    bot_user_id: str = None
) -> None:
    """
    Save or update Slack OAuth token for a specific workspace.

    Args:
        user_id: User's unique identifier
        team_id: Slack workspace ID
        team_name: Slack workspace name
        access_token: OAuth user token (xoxp-...)
        scope: Comma-separated scopes
        authed_user_id: Slack user ID
        authed_user_name: Slack username (optional)
        token_type: Token type ('user' or 'bot')
        bot_token: Bot OAuth token (xoxb-...) for DM chat (Phase 20-02)
        bot_user_id: Bot's Slack user ID (Phase 20-02)

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Tokens are stored per-user per-workspace, ensuring data isolation
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if this workspace already exists (update) or is new (insert)
        cursor.execute(
            "SELECT id FROM slack_tokens WHERE user_id = %s AND team_id = %s",
            (user_id, team_id)
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing token
            cursor.execute("""
                UPDATE slack_tokens
                SET team_name = %s, access_token = %s, scope = %s,
                    authed_user_id = %s, authed_user_name = %s, token_type = %s,
                    bot_token = %s, bot_user_id = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND team_id = %s
            """, (
                team_name, access_token, scope,
                authed_user_id, authed_user_name, token_type,
                bot_token, bot_user_id,
                user_id, team_id
            ))
        else:
            # Insert new token
            cursor.execute("""
                INSERT INTO slack_tokens
                (user_id, team_id, team_name, access_token, token_type, scope,
                 authed_user_id, authed_user_name, bot_token, bot_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id, team_id, team_name, access_token, token_type, scope,
                authed_user_id, authed_user_name, bot_token, bot_user_id
            ))


def get_slack_token(user_id: int, team_id: str) -> Optional[dict]:
    """
    Retrieve Slack OAuth token for a specific workspace.

    Args:
        user_id: User's unique identifier
        team_id: Slack workspace ID

    Returns:
        Dictionary with token fields, or None if no token exists

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and team_id for access control
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT team_id, team_name, access_token, token_type, scope,
                   authed_user_id, authed_user_name, bot_token, bot_user_id, created_at
            FROM slack_tokens
            WHERE user_id = %s AND team_id = %s
        """, (user_id, team_id))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "access_token": row["access_token"],
            "token_type": row["token_type"],
            "scope": row["scope"],
            "authed_user_id": row["authed_user_id"],
            "authed_user_name": row["authed_user_name"],
            "bot_token": row["bot_token"],
            "bot_user_id": row["bot_user_id"],
            "created_at": row["created_at"]
        }


def list_slack_tokens(user_id: int) -> list[dict]:
    """
    List all connected Slack workspaces for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dictionaries with team_id, team_name, authed_user_name, created_at

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT team_id, team_name, authed_user_name, scope, created_at
            FROM slack_tokens
            WHERE user_id = %s
            ORDER BY created_at ASC
        """, (user_id,))

        rows = cursor.fetchall()

        return [
            {
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "authed_user_name": row["authed_user_name"],
                "scope": row["scope"],
                "created_at": row["created_at"]
            }
            for row in rows
        ]


def delete_slack_token(user_id: int, team_id: str) -> bool:
    """
    Remove Slack OAuth token for a specific workspace.

    Args:
        user_id: User's unique identifier
        team_id: Slack workspace ID to disconnect

    Returns:
        True if token was deleted, False if no token existed

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and team_id to prevent unauthorized deletion
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM slack_tokens WHERE user_id = %s AND team_id = %s
        """, (user_id, team_id))

        return cursor.rowcount > 0


def get_first_slack_token(user_id: int) -> Optional[dict]:
    """
    Get the first connected Slack workspace for a user.

    Useful when no specific team_id is provided.

    Args:
        user_id: User's unique identifier

    Returns:
        Token dict for first workspace, or None if no workspaces connected
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT team_id, team_name, access_token, token_type, scope,
                   authed_user_id, authed_user_name, bot_token, bot_user_id, created_at
            FROM slack_tokens
            WHERE user_id = %s
            ORDER BY created_at ASC
            LIMIT 1
        """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "access_token": row["access_token"],
            "token_type": row["token_type"],
            "scope": row["scope"],
            "authed_user_id": row["authed_user_id"],
            "authed_user_name": row["authed_user_name"],
            "bot_token": row["bot_token"],
            "bot_user_id": row["bot_user_id"],
            "created_at": row["created_at"]
        }


def get_slack_bot_token(user_id: int, team_id: str = None) -> Optional[dict]:
    """
    Get Slack bot token for a user's workspace (Phase 20-02).

    Args:
        user_id: User's unique identifier
        team_id: Slack workspace ID (optional - uses first workspace if not specified)

    Returns:
        Dictionary with bot_token and bot_user_id, or None if not available

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if team_id:
            cursor.execute("""
                SELECT team_id, team_name, bot_token, bot_user_id
                FROM slack_tokens
                WHERE user_id = %s AND team_id = %s AND bot_token IS NOT NULL
            """, (user_id, team_id))
        else:
            cursor.execute("""
                SELECT team_id, team_name, bot_token, bot_user_id
                FROM slack_tokens
                WHERE user_id = %s AND bot_token IS NOT NULL
                ORDER BY created_at ASC
                LIMIT 1
            """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "team_id": row["team_id"],
            "team_name": row["team_name"],
            "bot_token": row["bot_token"],
            "bot_user_id": row["bot_user_id"]
        }


def list_users_with_slack_bot_token() -> list[dict]:
    """
    List all users who have a Slack bot token configured (Phase 20-02).

    Returns:
        List of dicts with user_id, team_id, bot_token, bot_user_id

    Used by the Slack bot polling worker to check for new messages.
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Only return valid bot tokens (xoxb-...), not user tokens (xoxp-...)
        cursor.execute("""
            SELECT user_id, team_id, team_name, bot_token, bot_user_id
            FROM slack_tokens
            WHERE bot_token IS NOT NULL AND bot_token LIKE 'xoxb-%%'
        """)

        rows = cursor.fetchall()

        return [
            {
                "user_id": row["user_id"],
                "team_id": row["team_id"],
                "team_name": row["team_name"],
                "bot_token": row["bot_token"],
                "bot_user_id": row["bot_user_id"]
            }
            for row in rows
        ]


def get_seny_user_by_slack_team(team_id: str) -> Optional[int]:
    """
    Get Seny user_id by Slack workspace team_id (Phase 21-02).

    Used by Slack Events handler to identify which Seny user owns
    a given Slack workspace when an event arrives via webhook.

    Args:
        team_id: Slack workspace team ID (e.g. "T01ABCDEF")

    Returns:
        Seny user_id or None if no user has a bot token for this workspace
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM slack_tokens WHERE team_id = %s AND bot_token LIKE 'xoxb-%%' LIMIT 1",
            (team_id,)
        )
        row = cursor.fetchone()
        return row["user_id"] if row else None


# ============================================================================
# Telegram Session Management - MTProto Client Sessions
# ============================================================================

def save_telegram_session(
    user_id: int,
    phone_number: str,
    session_string: str,
    user_name: str = None,
    display_name: str = None
) -> None:
    """
    Save or update Telegram session for a phone number.

    Args:
        user_id: User's unique identifier
        phone_number: Phone number with country code
        session_string: Telethon StringSession (SENSITIVE - equivalent to being logged in)
        user_name: Telegram @username (optional)
        display_name: User's display name (optional)

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Session strings are stored per-user per-phone, ensuring data isolation
        - NEVER log session strings - they are equivalent to passwords
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if this phone already exists (update) or is new (insert)
        cursor.execute(
            "SELECT id FROM telegram_sessions WHERE user_id = %s AND phone_number = %s",
            (user_id, phone_number)
        )
        existing = cursor.fetchone()

        if existing:
            # Update existing session
            cursor.execute("""
                UPDATE telegram_sessions
                SET session_string = %s, user_name = %s, display_name = %s,
                    updated_at = CURRENT_TIMESTAMP, last_active = CURRENT_TIMESTAMP
                WHERE user_id = %s AND phone_number = %s
            """, (session_string, user_name, display_name, user_id, phone_number))
        else:
            # Insert new session
            cursor.execute("""
                INSERT INTO telegram_sessions
                (user_id, phone_number, session_string, user_name, display_name, last_active)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            """, (user_id, phone_number, session_string, user_name, display_name))


def get_telegram_session(user_id: int, phone_number: str) -> Optional[dict]:
    """
    Retrieve Telegram session for a specific phone number.

    Args:
        user_id: User's unique identifier
        phone_number: Phone number to get session for

    Returns:
        Dictionary with session fields, or None if no session exists

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and phone_number for access control
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT phone_number, session_string, user_name, display_name,
                   created_at, updated_at, last_active
            FROM telegram_sessions
            WHERE user_id = %s AND phone_number = %s
        """, (user_id, phone_number))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "phone_number": row["phone_number"],
            "session_string": row["session_string"],
            "user_name": row["user_name"],
            "display_name": row["display_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_active": row["last_active"]
        }


def list_telegram_sessions(user_id: int) -> list[dict]:
    """
    List all connected Telegram accounts for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dictionaries with phone, username, display_name, last_active
        (DOES NOT include session_string for security)

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Excludes session_string from results to minimize exposure
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT phone_number, user_name, display_name, created_at, last_active
            FROM telegram_sessions
            WHERE user_id = %s
            ORDER BY created_at ASC
        """, (user_id,))

        rows = cursor.fetchall()

        return [
            {
                "phone_number": row["phone_number"],
                "user_name": row["user_name"],
                "display_name": row["display_name"],
                "created_at": row["created_at"],
                "last_active": row["last_active"]
            }
            for row in rows
        ]


def delete_telegram_session(user_id: int, phone_number: str) -> bool:
    """
    Remove Telegram session for a specific phone number.

    Args:
        user_id: User's unique identifier
        phone_number: Phone number to disconnect

    Returns:
        True if session was deleted, False if no session existed

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Requires both user_id and phone_number to prevent unauthorized deletion
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            DELETE FROM telegram_sessions WHERE user_id = %s AND phone_number = %s
        """, (user_id, phone_number))

        return cursor.rowcount > 0


def get_first_telegram_session(user_id: int) -> Optional[dict]:
    """
    Get the first connected Telegram account for a user.

    Useful when no specific phone number is provided.

    Args:
        user_id: User's unique identifier

    Returns:
        Session dict for first account, or None if no accounts connected
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT phone_number, session_string, user_name, display_name,
                   created_at, updated_at, last_active
            FROM telegram_sessions
            WHERE user_id = %s
            ORDER BY created_at ASC
            LIMIT 1
        """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            return None

        return {
            "phone_number": row["phone_number"],
            "session_string": row["session_string"],
            "user_name": row["user_name"],
            "display_name": row["display_name"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_active": row["last_active"]
        }


def update_telegram_last_active(user_id: int, phone_number: str) -> None:
    """
    Update the last_active timestamp for a Telegram session.

    Called when the session is used to track activity.

    Args:
        user_id: User's unique identifier
        phone_number: Phone number to update
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE telegram_sessions
            SET last_active = CURRENT_TIMESTAMP
            WHERE user_id = %s AND phone_number = %s
        """, (user_id, phone_number))


# ============================================================================
# Browser History Functions - Local Data Sync
# ============================================================================

def save_browser_history_batch(
    user_id: int,
    machine_id: str,
    entries: list[dict]
) -> dict:
    """
    Save a batch of browser history entries from the local agent.

    Uses INSERT OR IGNORE to handle duplicates (based on unique constraint).

    Args:
        user_id: User's unique identifier
        machine_id: Identifier for the source machine
        entries: List of dicts with url, title, visit_time, visit_count, domain

    Returns:
        Dict with inserted_count and skipped_count

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Entries are stored per-user per-machine, ensuring data isolation
    """
    inserted = 0
    skipped = 0

    with get_db() as conn:
        cursor = conn.cursor()

        for entry in entries:
            try:
                cursor.execute("""
                    INSERT INTO browser_history
                    (user_id, machine_id, url, title, visit_time, visit_count, domain)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    user_id,
                    machine_id,
                    entry.get("url"),
                    entry.get("title"),
                    entry.get("visit_time"),
                    entry.get("visit_count", 1),
                    entry.get("domain")
                ))
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception:
                skipped += 1

    return {"inserted_count": inserted, "skipped_count": skipped}


def search_browser_history(
    user_id: int,
    query: str,
    limit: int = 20,
    since: str = None,
    domain: str = None
) -> list[dict]:
    """
    Search browser history by URL or title text.

    Args:
        user_id: User's unique identifier
        query: Search query (matches URL or title)
        limit: Maximum results to return
        since: ISO datetime string to filter from
        domain: Optional domain filter

    Returns:
        List of matching history entries

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Always filters by user_id
    """
    with get_db() as conn:
        cursor = conn.cursor()

        sql = """
            SELECT url, title, visit_time, visit_count, domain, machine_id
            FROM browser_history
            WHERE user_id = %s
            AND (url LIKE %s OR title LIKE %s)
        """
        params = [user_id, f"%{query}%", f"%{query}%"]

        if since:
            sql += " AND visit_time >= %s"
            params.append(since)

        if domain:
            sql += " AND domain = %s"
            params.append(domain)

        sql += " ORDER BY visit_time DESC LIMIT %s"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()

        return [
            {
                "url": row["url"],
                "title": row["title"],
                "visit_time": row["visit_time"],
                "visit_count": row["visit_count"],
                "domain": row["domain"],
                "machine_id": row["machine_id"]
            }
            for row in rows
        ]


def get_recent_browser_history(
    user_id: int,
    limit: int = 50,
    machine_id: str = None
) -> list[dict]:
    """
    Get most recent browser history entries.

    Args:
        user_id: User's unique identifier
        limit: Maximum entries to return
        machine_id: Optional filter by specific machine

    Returns:
        List of recent history entries
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if machine_id:
            cursor.execute("""
                SELECT url, title, visit_time, visit_count, domain, machine_id
                FROM browser_history
                WHERE user_id = %s AND machine_id = %s
                ORDER BY visit_time DESC
                LIMIT %s
            """, (user_id, machine_id, limit))
        else:
            cursor.execute("""
                SELECT url, title, visit_time, visit_count, domain, machine_id
                FROM browser_history
                WHERE user_id = %s
                ORDER BY visit_time DESC
                LIMIT %s
            """, (user_id, limit))

        rows = cursor.fetchall()

        return [
            {
                "url": row["url"],
                "title": row["title"],
                "visit_time": row["visit_time"],
                "visit_count": row["visit_count"],
                "domain": row["domain"],
                "machine_id": row["machine_id"]
            }
            for row in rows
        ]


def get_domain_stats(
    user_id: int,
    since: str = None,
    limit: int = 20
) -> list[dict]:
    """
    Get most visited domains with counts.

    Args:
        user_id: User's unique identifier
        since: Optional ISO datetime to filter from
        limit: Maximum domains to return

    Returns:
        List of domain stats with visit counts
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if since:
            cursor.execute("""
                SELECT domain, COUNT(*) as visit_count, MAX(visit_time) as last_visit
                FROM browser_history
                WHERE user_id = %s AND domain IS NOT NULL AND visit_time >= %s
                GROUP BY domain
                ORDER BY visit_count DESC
                LIMIT %s
            """, (user_id, since, limit))
        else:
            cursor.execute("""
                SELECT domain, COUNT(*) as visit_count, MAX(visit_time) as last_visit
                FROM browser_history
                WHERE user_id = %s AND domain IS NOT NULL
                GROUP BY domain
                ORDER BY visit_count DESC
                LIMIT %s
            """, (user_id, limit))

        rows = cursor.fetchall()

        return [
            {
                "domain": row["domain"],
                "visit_count": row["visit_count"],
                "last_visit": row["last_visit"]
            }
            for row in rows
        ]


def get_browser_history_by_date(user_id: int, date: str) -> list[dict]:
    """
    Get all browser history for a specific date.

    Args:
        user_id: User's unique identifier
        date: Date string in YYYY-MM-DD format

    Returns:
        List of history entries for that date
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT url, title, visit_time, visit_count, domain, machine_id
            FROM browser_history
            WHERE user_id = %s AND DATE(visit_time) = %s
            ORDER BY visit_time DESC
        """, (user_id, date))

        rows = cursor.fetchall()

        return [
            {
                "url": row["url"],
                "title": row["title"],
                "visit_time": row["visit_time"],
                "visit_count": row["visit_count"],
                "domain": row["domain"],
                "machine_id": row["machine_id"]
            }
            for row in rows
        ]


def delete_browser_history(
    user_id: int,
    before: str = None,
    domain: str = None
) -> int:
    """
    Delete browser history entries (privacy control).

    Args:
        user_id: User's unique identifier
        before: Optional ISO datetime - delete entries before this time
        domain: Optional domain - delete only entries from this domain

    Returns:
        Number of entries deleted

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Always requires user_id - users can only delete their own history
    """
    with get_db() as conn:
        cursor = conn.cursor()

        sql = "DELETE FROM browser_history WHERE user_id = %s"
        params = [user_id]

        if before:
            sql += " AND visit_time < %s"
            params.append(before)

        if domain:
            sql += " AND domain = %s"
            params.append(domain)

        cursor.execute(sql, params)
        return cursor.rowcount


# ============================================================================
# Sync Status Functions - Track Agent Sync State
# ============================================================================

def update_sync_status(
    user_id: int,
    machine_id: str,
    sync_type: str,
    sync_count: int = 0,
    status: str = "active",
    error_message: str = None
) -> None:
    """
    Update sync status for a machine/sync_type combination.

    Args:
        user_id: User's unique identifier
        machine_id: Identifier for the source machine
        sync_type: Type of sync ('browser_history', 'files', etc.)
        sync_count: Number of items synced in this batch
        status: Status string ('active', 'paused', 'error')
        error_message: Optional error message if status is 'error'

    Security:
        - Uses parameterized queries to prevent SQL injection
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Check if status exists
        cursor.execute("""
            SELECT id FROM sync_status
            WHERE user_id = %s AND machine_id = %s AND sync_type = %s
        """, (user_id, machine_id, sync_type))

        existing = cursor.fetchone()

        if existing:
            cursor.execute("""
                UPDATE sync_status
                SET last_sync_time = CURRENT_TIMESTAMP,
                    last_sync_count = %s,
                    status = %s,
                    error_message = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND machine_id = %s AND sync_type = %s
            """, (sync_count, status, error_message, user_id, machine_id, sync_type))
        else:
            cursor.execute("""
                INSERT INTO sync_status
                (user_id, machine_id, sync_type, last_sync_time, last_sync_count, status, error_message)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s)
            """, (user_id, machine_id, sync_type, sync_count, status, error_message))


def get_sync_status(user_id: int, machine_id: str = None) -> list[dict]:
    """
    Get sync status for user's machines.

    Args:
        user_id: User's unique identifier
        machine_id: Optional filter by specific machine

    Returns:
        List of sync status entries
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if machine_id:
            cursor.execute("""
                SELECT machine_id, sync_type, last_sync_time, last_sync_count,
                       status, error_message, created_at, updated_at
                FROM sync_status
                WHERE user_id = %s AND machine_id = %s
                ORDER BY updated_at DESC
            """, (user_id, machine_id))
        else:
            cursor.execute("""
                SELECT machine_id, sync_type, last_sync_time, last_sync_count,
                       status, error_message, created_at, updated_at
                FROM sync_status
                WHERE user_id = %s
                ORDER BY updated_at DESC
            """, (user_id,))

        rows = cursor.fetchall()

        return [
            {
                "machine_id": row["machine_id"],
                "sync_type": row["sync_type"],
                "last_sync_time": row["last_sync_time"],
                "last_sync_count": row["last_sync_count"],
                "status": row["status"],
                "error_message": row["error_message"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"]
            }
            for row in rows
        ]


# ============================================================================
# Local Files Functions - File Index Management
# ============================================================================

def save_local_files_batch(
    user_id: int,
    machine_id: str,
    files: list[dict]
) -> dict:
    """
    Save a batch of local file metadata from the desktop agent.

    Uses INSERT OR REPLACE to update existing files based on unique constraint.
    Marks files as seen (last_seen = now) to track which files still exist.

    Args:
        user_id: User's unique identifier
        machine_id: Identifier for the source machine
        files: List of dicts with file_path, file_name, file_extension, etc.

    Returns:
        Dict with inserted_count, updated_count

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Files are stored per-user per-machine, ensuring data isolation
    """
    inserted = 0
    updated = 0

    with get_db() as conn:
        cursor = conn.cursor()

        for f in files:
            # Check if file already exists
            cursor.execute("""
                SELECT id FROM local_files
                WHERE user_id = %s AND machine_id = %s AND file_path = %s
            """, (user_id, machine_id, f.get("file_path")))

            existing = cursor.fetchone()

            if existing:
                # Update existing file
                cursor.execute("""
                    UPDATE local_files
                    SET file_name = %s,
                        file_extension = %s,
                        file_size = %s,
                        file_created = %s,
                        file_modified = %s,
                        content_preview = %s,
                        drive_letter = %s,
                        parent_folder = %s,
                        last_seen = CURRENT_TIMESTAMP,
                        is_deleted = 0
                    WHERE user_id = %s AND machine_id = %s AND file_path = %s
                """, (
                    f.get("file_name"),
                    f.get("file_extension"),
                    f.get("file_size"),
                    f.get("file_created"),
                    f.get("file_modified"),
                    f.get("content_preview"),
                    f.get("drive_letter"),
                    f.get("parent_folder"),
                    user_id,
                    machine_id,
                    f.get("file_path")
                ))
                updated += 1
            else:
                # Insert new file
                cursor.execute("""
                    INSERT INTO local_files
                    (user_id, machine_id, file_path, file_name, file_extension,
                     file_size, file_created, file_modified, content_preview,
                     drive_letter, parent_folder, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """, (
                    user_id,
                    machine_id,
                    f.get("file_path"),
                    f.get("file_name"),
                    f.get("file_extension"),
                    f.get("file_size"),
                    f.get("file_created"),
                    f.get("file_modified"),
                    f.get("content_preview"),
                    f.get("drive_letter"),
                    f.get("parent_folder")
                ))
                inserted += 1

    return {"inserted_count": inserted, "updated_count": updated}


def search_local_files(
    user_id: int,
    query: str,
    file_type: str = None,
    folder: str = None,
    modified_since: str = None,
    include_deleted: bool = False,
    limit: int = 20
) -> list[dict]:
    """
    Search local files using FTS5 full-text search.

    Searches file names, paths, and content previews.

    Args:
        user_id: User's unique identifier
        query: Search query (FTS5 format)
        file_type: Optional filter by extension (e.g., '.mp4')
        folder: Optional filter by folder path prefix
        modified_since: Optional ISO datetime filter
        include_deleted: Whether to include soft-deleted files
        limit: Maximum results to return

    Returns:
        List of matching file dicts

    Security:
        - Uses parameterized queries to prevent SQL injection
        - Always filters by user_id
    """
    if not query or not query.strip():
        return []

    original_query = query.strip()
    search_pattern = f'%{original_query}%'

    with get_db() as conn:
        cursor = conn.cursor()

        try:
            # Build the SQL query with optional filters
            # Uses ILIKE for case-insensitive search (pg_trgm compatible)
            sql = """
                SELECT DISTINCT
                    lf.id, lf.file_path, lf.file_name, lf.file_extension,
                    lf.file_size, lf.file_created, lf.file_modified,
                    lf.content_preview, lf.drive_letter, lf.parent_folder,
                    lf.machine_id, lf.indexed_at, lf.is_deleted
                FROM local_files lf
                WHERE (lf.file_path ILIKE %s OR lf.content_text ILIKE %s)
                AND lf.user_id = %s
            """
            params = [search_pattern, search_pattern, user_id]

            if not include_deleted:
                sql += " AND lf.is_deleted = 0"

            if file_type:
                sql += " AND lf.file_extension = %s"
                params.append(file_type.lower())

            if folder:
                sql += " AND lf.file_path LIKE %s"
                params.append(f"{folder}%")

            if modified_since:
                sql += " AND lf.file_modified >= %s"
                params.append(modified_since)

            sql += " ORDER BY lf.file_modified DESC LIMIT %s"
            params.append(limit)

            cursor.execute(sql, params)
        except Exception:
            return []

        rows = cursor.fetchall()

        results = []
        for row in rows:
            row_dict = dict(row)
            snippet_text = row_dict.get('content_preview') or row_dict.get('file_path') or ''
            results.append({
                "id": row_dict["id"],
                "file_path": row_dict["file_path"],
                "file_name": row_dict["file_name"],
                "file_extension": row_dict["file_extension"],
                "file_size": row_dict["file_size"],
                "file_created": row_dict["file_created"],
                "file_modified": row_dict["file_modified"],
                "content_preview": row_dict["content_preview"],
                "drive_letter": row_dict["drive_letter"],
                "parent_folder": row_dict["parent_folder"],
                "machine_id": row_dict["machine_id"],
                "indexed_at": row_dict["indexed_at"],
                "is_deleted": bool(row_dict["is_deleted"]),
                "snippet": extract_snippet(snippet_text, original_query)
            })
        return results


def get_recent_local_files(
    user_id: int,
    days: int = 7,
    file_type: str = None,
    machine_id: str = None,
    limit: int = 20
) -> list[dict]:
    """
    Get recently modified files.

    Args:
        user_id: User's unique identifier
        days: Number of days back to look
        file_type: Optional filter by extension
        machine_id: Optional filter by machine
        limit: Maximum results to return

    Returns:
        List of recent file dicts
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        sql = """
            SELECT id, file_path, file_name, file_extension, file_size,
                   file_created, file_modified, content_preview,
                   drive_letter, parent_folder, machine_id, indexed_at
            FROM local_files
            WHERE user_id = %s
            AND is_deleted = 0
            AND file_modified >= %s
        """
        params = [user_id, cutoff]

        if file_type:
            sql += " AND file_extension = %s"
            params.append(file_type.lower())

        if machine_id:
            sql += " AND machine_id = %s"
            params.append(machine_id)

        sql += " ORDER BY file_modified DESC LIMIT %s"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "file_path": row["file_path"],
                "file_name": row["file_name"],
                "file_extension": row["file_extension"],
                "file_size": row["file_size"],
                "file_created": row["file_created"],
                "file_modified": row["file_modified"],
                "content_preview": row["content_preview"],
                "drive_letter": row["drive_letter"],
                "parent_folder": row["parent_folder"],
                "machine_id": row["machine_id"],
                "indexed_at": row["indexed_at"]
            }
            for row in rows
        ]


def get_local_files_by_extension(
    user_id: int,
    extension: str,
    folder: str = None,
    limit: int = 50
) -> list[dict]:
    """
    Get files by extension.

    Args:
        user_id: User's unique identifier
        extension: File extension (e.g., '.mp4', '.docx')
        folder: Optional folder path prefix filter
        limit: Maximum results to return

    Returns:
        List of matching file dicts
    """
    with get_db() as conn:
        cursor = conn.cursor()

        sql = """
            SELECT id, file_path, file_name, file_extension, file_size,
                   file_created, file_modified, drive_letter, parent_folder,
                   machine_id, indexed_at
            FROM local_files
            WHERE user_id = %s
            AND is_deleted = 0
            AND file_extension = %s
        """
        params = [user_id, extension.lower()]

        if folder:
            sql += " AND file_path LIKE %s"
            params.append(f"{folder}%")

        sql += " ORDER BY file_modified DESC LIMIT %s"
        params.append(limit)

        cursor.execute(sql, params)
        rows = cursor.fetchall()

        return [
            {
                "id": row["id"],
                "file_path": row["file_path"],
                "file_name": row["file_name"],
                "file_extension": row["file_extension"],
                "file_size": row["file_size"],
                "file_created": row["file_created"],
                "file_modified": row["file_modified"],
                "drive_letter": row["drive_letter"],
                "parent_folder": row["parent_folder"],
                "machine_id": row["machine_id"],
                "indexed_at": row["indexed_at"]
            }
            for row in rows
        ]


def mark_local_files_deleted(
    user_id: int,
    machine_id: str,
    file_paths: list[str]
) -> int:
    """
    Mark files as deleted (soft delete).

    Called when the desktop agent detects files have been removed.

    Args:
        user_id: User's unique identifier
        machine_id: Machine the files are from
        file_paths: List of file paths to mark as deleted

    Returns:
        Number of files marked as deleted
    """
    if not file_paths:
        return 0

    with get_db() as conn:
        cursor = conn.cursor()

        count = 0
        for path in file_paths:
            cursor.execute("""
                UPDATE local_files
                SET is_deleted = 1
                WHERE user_id = %s AND machine_id = %s AND file_path = %s
            """, (user_id, machine_id, path))
            count += cursor.rowcount

        return count


def get_local_file_stats(user_id: int) -> dict:
    """
    Get file statistics for a user.

    Returns counts by extension, drive, and machine.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with stats: total_files, by_extension, by_drive, by_machine
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Total files
        cursor.execute("""
            SELECT COUNT(*) FROM local_files
            WHERE user_id = %s AND is_deleted = 0
        """, (user_id,))
        total = cursor.fetchone()[0]

        # By extension (top 20)
        cursor.execute("""
            SELECT file_extension, COUNT(*) as count
            FROM local_files
            WHERE user_id = %s AND is_deleted = 0 AND file_extension IS NOT NULL
            GROUP BY file_extension
            ORDER BY count DESC
            LIMIT 20
        """, (user_id,))
        by_extension = [
            {"extension": row["file_extension"], "count": row["count"]}
            for row in cursor.fetchall()
        ]

        # By drive
        cursor.execute("""
            SELECT drive_letter, COUNT(*) as count
            FROM local_files
            WHERE user_id = %s AND is_deleted = 0 AND drive_letter IS NOT NULL
            GROUP BY drive_letter
            ORDER BY count DESC
        """, (user_id,))
        by_drive = [
            {"drive": row["drive_letter"], "count": row["count"]}
            for row in cursor.fetchall()
        ]

        # By machine
        cursor.execute("""
            SELECT machine_id, COUNT(*) as count
            FROM local_files
            WHERE user_id = %s AND is_deleted = 0
            GROUP BY machine_id
            ORDER BY count DESC
        """, (user_id,))
        by_machine = [
            {"machine_id": row["machine_id"], "count": row["count"]}
            for row in cursor.fetchall()
        ]

        return {
            "total_files": total,
            "by_extension": by_extension,
            "by_drive": by_drive,
            "by_machine": by_machine
        }


def get_local_file_paths(user_id: int, machine_id: str) -> list[str]:
    """
    Get all synced file paths for a machine.

    Used by the desktop agent to detect deleted files.

    Args:
        user_id: User's unique identifier
        machine_id: Machine identifier

    Returns:
        List of file paths that are currently synced (not deleted)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT file_path FROM local_files
            WHERE user_id = %s AND machine_id = %s AND is_deleted = 0
        """, (user_id, machine_id))

        return [row["file_path"] for row in cursor.fetchall()]


# ============================================================================
# Second Brain - People CRUD Functions
# ============================================================================

def create_person(
    user_id: int,
    name: str,
    context: str = None,
    google_contact_id: str = None,
    notes: str = None,
    relationship_type: str = None
) -> Optional[int]:
    """
    Create a new person in the Second Brain people database.

    Args:
        user_id: User's unique identifier
        name: Person's name
        context: Who they are, how you know them
        google_contact_id: Optional link to Google Contact
        notes: Freeform notes about them
        relationship_type: Relationship category (e.g., 'family', 'friend', 'colleague')

    Returns:
        Person ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO people (user_id, name, context, google_contact_id, notes, relationship_type)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, name, context, google_contact_id, notes, relationship_type))
            row = cursor.fetchone()

            return row['id'] if row else None
    except Exception as e:
        return None


def get_person(person_id: int) -> Optional[dict]:
    """
    Get a person by ID.

    Args:
        person_id: Person's unique identifier

    Returns:
        Person dictionary or None if not found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, name, google_contact_id, context, notes,
                   relationship_type, last_contact_date, created_at, updated_at
            FROM people WHERE id = %s
        """, (person_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


def get_people_by_user(user_id: int, limit: int = 100) -> list[dict]:
    """
    Get all people for a user.

    Args:
        user_id: User's unique identifier
        limit: Maximum results to return

    Returns:
        List of person dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, name, google_contact_id, context, notes,
                   relationship_type, last_contact_date, created_at, updated_at
            FROM people
            WHERE user_id = %s
            ORDER BY name ASC
            LIMIT %s
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_family_contacts(user_id: int) -> list[dict]:
    """Get all people tagged as family for a user."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, name, google_contact_id, context, notes,
                   relationship_type, last_contact_date, created_at, updated_at
            FROM people
            WHERE user_id = %s AND relationship_type = 'family'
            ORDER BY name ASC
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]


def update_person(person_id: int, **fields) -> bool:
    """
    Update a person's fields.

    Args:
        person_id: Person's unique identifier
        **fields: Fields to update (name, context, notes, google_contact_id, last_contact_date, relationship_type)

    Returns:
        True if updated, False otherwise
    """
    if not fields:
        return False

    allowed_fields = {'name', 'context', 'notes', 'google_contact_id', 'last_contact_date', 'relationship_type'}
    update_fields = {k: v for k, v in fields.items() if k in allowed_fields}

    if not update_fields:
        return False

    set_clause = ', '.join(f'{k} = %s' for k in update_fields.keys())
    set_clause += ', updated_at = CURRENT_TIMESTAMP'
    values = list(update_fields.values()) + [person_id]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE people SET {set_clause} WHERE id = %s
        """, values)
        return cursor.rowcount > 0


def delete_person(person_id: int) -> bool:
    """
    Delete a person.

    Args:
        person_id: Person's unique identifier

    Returns:
        True if deleted, False otherwise
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM people WHERE id = %s", (person_id,))
        return cursor.rowcount > 0


def delete_embedding_tracking(entity_type: str, entity_id, user_id: int) -> bool:
    """Delete an embedding_tracking record to force re-embedding on next scheduler run."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM embedding_tracking WHERE entity_type = %s AND entity_id = %s AND user_id = %s",
                (entity_type, str(entity_id), user_id)
            )
            return cursor.rowcount > 0
    except Exception as e:
        logger.error("delete_embedding_tracking error: %s", repr(e))
        return False


def merge_people(user_id: int, winner_id: int, loser_id: int) -> dict:
    """
    Merge two people records. Winner absorbs loser's data, references, and is kept.
    Loser is deleted after all FK dependencies are transferred.

    12-step transactional merge:
    1. Validate both exist and belong to user
    2. Merge text fields (context, notes, last_contact_date, relationship_type)
    3-6. Transfer FK references (people_followups, activity_log, detected_actions, entity_mappings)
    7. Transfer cross_references (handle UNIQUE conflicts)
    8. Delete embedding_tracking for loser
    9. Log merge to activity_log
    10. Delete loser
    11-12. Post-commit: ChromaDB cleanup

    Returns dict with merge details or raises on error.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Step 1: Validate both records exist and belong to user
            cursor.execute("SELECT * FROM people WHERE id = %s AND user_id = %s", (winner_id, user_id))
            winner = cursor.fetchone()
            if not winner:
                return {"success": False, "error": f"Winner person {winner_id} not found"}
            winner = dict(winner)

            cursor.execute("SELECT * FROM people WHERE id = %s AND user_id = %s", (loser_id, user_id))
            loser = cursor.fetchone()
            if not loser:
                return {"success": False, "error": f"Loser person {loser_id} not found"}
            loser = dict(loser)

            # Step 2: Merge text fields
            merged_context = winner.get('context') or ''
            loser_context = loser.get('context') or ''
            if loser_context:
                merged_context = f"{merged_context}\n---\n(Merged from: {loser['name']})\n{loser_context}" if merged_context else loser_context

            merged_notes = winner.get('notes') or ''
            loser_notes = loser.get('notes') or ''
            if loser_notes:
                merged_notes = f"{merged_notes}\n---\n(Merged from: {loser['name']})\n{loser_notes}" if merged_notes else loser_notes

            # Keep most recent last_contact_date
            winner_lcd = winner.get('last_contact_date') or ''
            loser_lcd = loser.get('last_contact_date') or ''
            merged_lcd = max(winner_lcd, loser_lcd) if winner_lcd and loser_lcd else (winner_lcd or loser_lcd)

            # Keep winner's relationship_type unless NULL
            merged_rt = winner.get('relationship_type') or loser.get('relationship_type')

            cursor.execute("""
                UPDATE people SET context = %s, notes = %s, last_contact_date = %s,
                       relationship_type = %s, updated_at = NOW()
                WHERE id = %s
            """, (merged_context, merged_notes, merged_lcd or None, merged_rt, winner_id))

            # Step 3: Transfer people_followups
            cursor.execute(
                "UPDATE people_followups SET person_id = %s WHERE person_id = %s",
                (winner_id, loser_id)
            )
            followups_moved = cursor.rowcount

            # Step 4: Transfer activity_log
            cursor.execute(
                "UPDATE activity_log SET person_id = %s WHERE person_id = %s",
                (winner_id, loser_id)
            )
            activity_moved = cursor.rowcount

            # Step 5: Transfer detected_actions
            cursor.execute(
                "UPDATE detected_actions SET person_id = %s WHERE person_id = %s",
                (winner_id, loser_id)
            )
            actions_moved = cursor.rowcount

            # Step 6: Transfer entity_mappings (handle UNIQUE conflicts)
            # Delete conflicts first: same source+source_identifier mapped to both winner and loser
            cursor.execute("""
                DELETE FROM entity_mappings
                WHERE person_id = %s
                  AND (user_id, source, source_identifier) IN (
                      SELECT user_id, source, source_identifier
                      FROM entity_mappings WHERE person_id = %s
                  )
            """, (loser_id, winner_id))
            # Update remaining
            cursor.execute(
                "UPDATE entity_mappings SET person_id = %s WHERE person_id = %s",
                (winner_id, loser_id)
            )
            mappings_moved = cursor.rowcount

            # Step 7: Transfer cross_references (handle UNIQUE conflicts)
            cursor.execute("""
                DELETE FROM cross_references
                WHERE entity_type = 'person' AND entity_id = %s
                  AND scanned_item_id IN (
                      SELECT scanned_item_id FROM cross_references
                      WHERE entity_type = 'person' AND entity_id = %s
                  )
            """, (loser_id, winner_id))
            cursor.execute(
                "UPDATE cross_references SET entity_id = %s WHERE entity_type = 'person' AND entity_id = %s",
                (winner_id, loser_id)
            )

            # Step 8: Delete embedding_tracking for loser
            cursor.execute(
                "DELETE FROM embedding_tracking WHERE entity_type = 'people' AND entity_id = %s AND user_id = %s",
                (str(loser_id), user_id)
            )

            # Step 9: Log merge to activity_log
            cursor.execute("""
                INSERT INTO activity_log (user_id, person_id, action_type, old_value, new_value, source, created_at)
                VALUES (%s, %s, 'merge', %s, %s, 'system', NOW())
            """, (user_id, winner_id, f"Merged: {loser['name']} (ID {loser_id})", f"Into: {winner['name']} (ID {winner_id})"))

            # Step 10: Delete loser
            cursor.execute("DELETE FROM people WHERE id = %s", (loser_id,))

        # Steps 11-12: Post-commit ChromaDB cleanup (non-transactional, fail-open)
        try:
            from web.services.embedding_service import get_embedding_service
            emb = get_embedding_service()
            if emb.enabled:
                emb.delete_embeddings("people", [f"person_{loser_id}"])
                # Force re-embed winner with merged data
                delete_embedding_tracking("people", str(winner_id), user_id)
        except Exception as e:
            logger.warning("merge_people: ChromaDB cleanup failed (non-blocking): %s", repr(e))

        return {
            "success": True,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "loser_name": loser['name'],
            "winner_name": winner['name'],
            "followups_moved": followups_moved,
            "activity_moved": activity_moved,
            "actions_moved": actions_moved,
            "mappings_moved": mappings_moved,
        }

    except Exception as e:
        logger.error("merge_people error: %s", repr(e))
        return {"success": False, "error": str(e)}


def search_people(user_id: int, query: str, limit: int = 20) -> list[dict]:
    """
    Search people using FTS5 full-text search.

    Args:
        user_id: User's unique identifier
        query: Search query
        limit: Maximum results to return

    Returns:
        List of matching person dictionaries
    """
    if not query or not query.strip():
        return []

    original_query = query.strip()
    search_pattern = f'%{original_query}%'

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT DISTINCT p.id, p.user_id, p.name, p.google_contact_id,
                       p.context, p.notes, p.relationship_type, p.last_contact_date,
                       p.created_at, p.updated_at
                FROM people p
                WHERE (p.name ILIKE %s OR p.context ILIKE %s)
                AND p.user_id = %s
                ORDER BY p.name ASC
                LIMIT %s
            """, (search_pattern, search_pattern, user_id, limit))
            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                snippet_text = row_dict.get('context') or row_dict.get('name') or ''
                row_dict['snippet'] = extract_snippet(snippet_text, original_query)
                results.append(row_dict)
            return results
        except Exception:
            return []


def add_person_followup(person_id: int, content: str) -> Optional[int]:
    """
    Add a follow-up item for a person.

    Args:
        person_id: Person's unique identifier
        content: What to remember/follow up on

    Returns:
        Followup ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO people_followups (person_id, content)
                VALUES (%s, %s)
                RETURNING id
            """, (person_id, content))
            row = cursor.fetchone()

            return row['id'] if row else None
    except Exception as e:
        return None


def get_person_followups(person_id: int, status: str = 'active') -> list[dict]:
    """
    Get follow-up items for a person.

    Args:
        person_id: Person's unique identifier
        status: Filter by status ('active', 'completed', 'dismissed', or None for all)

    Returns:
        List of followup dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT id, person_id, content, status, created_at, completed_at
                FROM people_followups
                WHERE person_id = %s AND status = %s
                ORDER BY created_at DESC
            """, (person_id, status))
        else:
            cursor.execute("""
                SELECT id, person_id, content, status, created_at, completed_at
                FROM people_followups
                WHERE person_id = %s
                ORDER BY created_at DESC
            """, (person_id,))
        return [dict(row) for row in cursor.fetchall()]


def complete_person_followup(followup_id: int) -> bool:
    """
    Mark a follow-up as completed.

    Args:
        followup_id: Followup's unique identifier

    Returns:
        True if updated, False otherwise
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE people_followups
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (followup_id,))
        return cursor.rowcount > 0


# ============================================================================
# Predictive Intelligence - People & Follow-up Helpers
# ============================================================================

def get_stale_contacts(user_id: int, min_days_stale: int = 14, limit: int = 10) -> list[dict]:
    """
    Return people whose last_contact_date is older than min_days_stale days.

    Args:
        user_id: User's unique identifier
        min_days_stale: Minimum days since last contact to qualify (default 14)
        limit: Maximum number of results (oldest first)

    Returns:
        List of dicts with id, name, last_contact_date
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, last_contact_date
            FROM people
            WHERE user_id = %s
              AND last_contact_date IS NOT NULL
              AND last_contact_date::DATE < CURRENT_DATE - (%s * INTERVAL '1 day')
            ORDER BY last_contact_date ASC
            LIMIT %s
        """, (user_id, min_days_stale, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_contact_frequency(user_id: int, person_id: int, days_lookback: int = 180) -> Optional[float]:
    """
    Compute average contact interval in days from activity_log history.

    Args:
        user_id: User's unique identifier
        person_id: Person's unique identifier
        days_lookback: How far back to look for contact history (default 180 days)

    Returns:
        Average days between contacts as float, or None if fewer than 2 entries found.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_lookback)
        cursor.execute("""
            SELECT COUNT(*) AS cnt
            FROM activity_log
            WHERE user_id = %s
              AND person_id = %s
              AND deleted_at IS NULL
              AND created_at > %s
        """, (user_id, person_id, cutoff))
        row = cursor.fetchone()
        count = row['cnt'] if row else 0
        if count < 2:
            return None
        return days_lookback / count


def get_overdue_followups(user_id: int, min_age_days: int = 7, limit: int = 10) -> list[dict]:
    """
    Return active follow-up items older than min_age_days days.

    Args:
        user_id: User's unique identifier
        min_age_days: Minimum age in days for a follow-up to qualify (default 7)
        limit: Maximum number of results (oldest first)

    Returns:
        List of dicts with followup_id, person_id, person_name, content, created_at
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
        cursor.execute("""
            SELECT pf.id AS followup_id,
                   p.id  AS person_id,
                   p.name AS person_name,
                   pf.content,
                   pf.created_at
            FROM people_followups pf
            JOIN people p ON p.id = pf.person_id
            WHERE p.user_id = %s
              AND pf.status = 'active'
              AND pf.created_at < %s
            ORDER BY pf.created_at ASC
            LIMIT %s
        """, (user_id, cutoff, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_recent_nudge_for_source(
    user_id: int,
    source_type: str,
    source_id: int,
    nudge_type: Optional[str] = None,
    days: int = 7,
) -> Optional[dict]:
    """
    Check if a nudge was sent for a specific source within the last N days.

    Used for time-windowed deduplication (e.g. prevent relationship nudges more
    than once per week for the same person).

    Args:
        user_id: User's unique identifier
        source_type: Type of source item (e.g. 'person', 'followup')
        source_id: ID of source item
        nudge_type: Optional nudge_type filter (e.g. 'relationship_check')
        days: Look-back window in days (default 7)

    Returns:
        Nudge dict if found within window, None otherwise
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            if nudge_type:
                cursor.execute("""
                    SELECT id FROM nudges
                    WHERE user_id = %s
                      AND source_type = %s
                      AND source_id = %s
                      AND nudge_type = %s
                      AND created_at > %s
                    LIMIT 1
                """, (user_id, source_type, source_id, nudge_type, cutoff))
            else:
                cursor.execute("""
                    SELECT id FROM nudges
                    WHERE user_id = %s
                      AND source_type = %s
                      AND source_id = %s
                      AND created_at > %s
                    LIMIT 1
                """, (user_id, source_type, source_id, cutoff))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to check recent nudge for source: %s", repr(e))
        return None


def get_recent_feedback_for_source(
    user_id: int,
    source_type: str,
    source_id: int,
    hours: int = 24
) -> Optional[dict]:
    """
    Check if user gave 'already_handled' or 'dismissed' feedback on a source item
    recently, from ANY delivery path. Used by send_nudge() to prevent re-sending
    items the user has already actioned.

    Does NOT suppress on 'not_helpful' — that means the nudge was poorly worded,
    not that the underlying item is resolved.
    """
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT uf.feedback_type, uf.created_at
                FROM user_feedback uf
                JOIN nudges n ON n.id = uf.item_id
                WHERE uf.user_id = %s
                  AND uf.item_type = 'nudge'
                  AND uf.feedback_type IN ('already_handled', 'dismissed')
                  AND n.source_type = %s
                  AND n.source_id = %s
                  AND uf.created_at > %s
                ORDER BY uf.created_at DESC
                LIMIT 1
            """, (user_id, source_type, source_id, cutoff))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.warning("get_recent_feedback_for_source failed: %s", repr(e))
        return None


# ============================================================================
# Second Brain - Projects CRUD Functions
# ============================================================================

def create_project(
    user_id: int,
    name: str,
    next_action: str = None,
    notes: str = None,
    status: str = 'active'
) -> Optional[int]:
    """
    Create a new project in the Second Brain projects database.

    Args:
        user_id: User's unique identifier
        name: Project name
        next_action: GTD-style next executable action
        notes: Project notes
        status: Project status (active, waiting, blocked, someday, done)

    Returns:
        Project ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO projects (user_id, name, next_action, notes, status)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, name, next_action, notes, status))
            row = cursor.fetchone()

            return row['id'] if row else None
    except Exception as e:
        return None


def get_project(project_id: int) -> Optional[dict]:
    """
    Get a project by ID.

    Args:
        project_id: Project's unique identifier

    Returns:
        Project dictionary or None if not found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, name, status, next_action, notes,
                   created_at, updated_at
            FROM projects WHERE id = %s
        """, (project_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


def get_projects_by_user(user_id: int, status: str = None, limit: int = 100) -> list[dict]:
    """
    Get all projects for a user.

    Args:
        user_id: User's unique identifier
        status: Optional status filter
        limit: Maximum results to return

    Returns:
        List of project dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute("""
                SELECT id, user_id, name, status, next_action, notes,
                       created_at, updated_at
                FROM projects
                WHERE user_id = %s AND status = %s
                ORDER BY updated_at DESC
                LIMIT %s
            """, (user_id, status, limit))
        else:
            cursor.execute("""
                SELECT id, user_id, name, status, next_action, notes,
                       created_at, updated_at
                FROM projects
                WHERE user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
            """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def update_project(project_id: int, **fields) -> bool:
    """
    Update a project's fields.

    Args:
        project_id: Project's unique identifier
        **fields: Fields to update (name, status, next_action, notes)

    Returns:
        True if updated, False otherwise
    """
    if not fields:
        return False

    allowed_fields = {'name', 'status', 'next_action', 'notes'}
    update_fields = {k: v for k, v in fields.items() if k in allowed_fields}

    if not update_fields:
        return False

    set_clause = ', '.join(f'{k} = %s' for k in update_fields.keys())
    set_clause += ', updated_at = CURRENT_TIMESTAMP'
    values = list(update_fields.values()) + [project_id]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE projects SET {set_clause} WHERE id = %s
        """, values)
        return cursor.rowcount > 0


def delete_project(project_id: int) -> bool:
    """
    Delete a project.

    Args:
        project_id: Project's unique identifier

    Returns:
        True if deleted, False otherwise
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        return cursor.rowcount > 0


def search_projects(user_id: int, query: str, limit: int = 20) -> list[dict]:
    """
    Search projects using FTS5 full-text search.

    Args:
        user_id: User's unique identifier
        query: Search query
        limit: Maximum results to return

    Returns:
        List of matching project dictionaries
    """
    if not query or not query.strip():
        return []

    original_query = query.strip()
    search_pattern = f'%{original_query}%'

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT DISTINCT p.id, p.user_id, p.name, p.status,
                       p.next_action, p.notes, p.created_at, p.updated_at
                FROM projects p
                WHERE p.name ILIKE %s
                AND p.user_id = %s
                ORDER BY p.updated_at DESC
                LIMIT %s
            """, (search_pattern, user_id, limit))
            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                snippet_text = row_dict.get('name') or ''
                row_dict['snippet'] = extract_snippet(snippet_text, original_query)
                results.append(row_dict)
            return results
        except Exception:
            return []


# ============================================================================
# Second Brain - Ideas CRUD Functions
# ============================================================================

def create_idea(
    user_id: int,
    title: str,
    summary: str = None,
    notes: str = None,
    tags: str = None
) -> Optional[int]:
    """
    Create a new idea in the Second Brain ideas database.

    Args:
        user_id: User's unique identifier
        title: Idea title
        summary: One-liner capturing core insight
        notes: Elaboration
        tags: Comma-separated tags

    Returns:
        Idea ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ideas (user_id, title, summary, notes, tags)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, title, summary, notes, tags))
            row = cursor.fetchone()

            return row['id'] if row else None
    except Exception as e:
        return None


def get_idea(idea_id: int) -> Optional[dict]:
    """
    Get an idea by ID.

    Args:
        idea_id: Idea's unique identifier

    Returns:
        Idea dictionary or None if not found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, title, summary, notes, tags,
                   created_at, updated_at
            FROM ideas WHERE id = %s
        """, (idea_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


def get_ideas_by_user(user_id: int, limit: int = 100) -> list[dict]:
    """
    Get all ideas for a user.

    Args:
        user_id: User's unique identifier
        limit: Maximum results to return

    Returns:
        List of idea dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, title, summary, notes, tags,
                   created_at, updated_at
            FROM ideas
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def update_idea(idea_id: int, **fields) -> bool:
    """
    Update an idea's fields.

    Args:
        idea_id: Idea's unique identifier
        **fields: Fields to update (title, summary, notes, tags)

    Returns:
        True if updated, False otherwise
    """
    if not fields:
        return False

    allowed_fields = {'title', 'summary', 'notes', 'tags'}
    update_fields = {k: v for k, v in fields.items() if k in allowed_fields}

    if not update_fields:
        return False

    set_clause = ', '.join(f'{k} = %s' for k in update_fields.keys())
    set_clause += ', updated_at = CURRENT_TIMESTAMP'
    values = list(update_fields.values()) + [idea_id]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            UPDATE ideas SET {set_clause} WHERE id = %s
        """, values)
        return cursor.rowcount > 0


def delete_idea(idea_id: int) -> bool:
    """
    Delete an idea.

    Args:
        idea_id: Idea's unique identifier

    Returns:
        True if deleted, False otherwise
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ideas WHERE id = %s", (idea_id,))
        return cursor.rowcount > 0


def merge_ideas(user_id: int, winner_id: int, loser_id: int) -> dict:
    """
    Merge two idea records. Winner absorbs loser's data.

    7-step transactional merge:
    1. Validate both exist
    2. Append loser's summary/notes to winner's notes with merge separator
    3. Combine and deduplicate tags (case-insensitive)
    4. Handle cross_references UNIQUE conflicts (entity_type='idea')
    5. Delete embedding_tracking for loser
    6. Delete loser
    7. Post-commit: ChromaDB cleanup
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Step 1: Validate
            cursor.execute("SELECT * FROM ideas WHERE id = %s AND user_id = %s", (winner_id, user_id))
            winner = cursor.fetchone()
            if not winner:
                return {"success": False, "error": f"Winner idea {winner_id} not found"}
            winner = dict(winner)

            cursor.execute("SELECT * FROM ideas WHERE id = %s AND user_id = %s", (loser_id, user_id))
            loser = cursor.fetchone()
            if not loser:
                return {"success": False, "error": f"Loser idea {loser_id} not found"}
            loser = dict(loser)

            # Step 2: Merge text fields into winner's notes
            merged_notes = winner.get('notes') or ''
            loser_summary = loser.get('summary') or ''
            loser_notes = loser.get('notes') or ''
            merge_text = ''
            if loser_summary:
                merge_text += loser_summary
            if loser_notes:
                merge_text += ('\n' + loser_notes) if merge_text else loser_notes
            if merge_text:
                separator = f"\n---\n(Merged from: {loser['title']})\n"
                merged_notes = f"{merged_notes}{separator}{merge_text}" if merged_notes else merge_text

            # Step 3: Combine and deduplicate tags (case-insensitive)
            winner_tags = [t.strip() for t in (winner.get('tags') or '').split(',') if t.strip()]
            loser_tags = [t.strip() for t in (loser.get('tags') or '').split(',') if t.strip()]
            seen_lower = set()
            merged_tags = []
            for tag in winner_tags + loser_tags:
                if tag.lower() not in seen_lower:
                    seen_lower.add(tag.lower())
                    merged_tags.append(tag)
            merged_tags_str = ', '.join(merged_tags) if merged_tags else None

            cursor.execute("""
                UPDATE ideas SET notes = %s, tags = %s, updated_at = NOW()
                WHERE id = %s
            """, (merged_notes, merged_tags_str, winner_id))

            # Step 4: Handle cross_references UNIQUE conflicts
            cursor.execute("""
                DELETE FROM cross_references
                WHERE entity_type = 'idea' AND entity_id = %s
                  AND scanned_item_id IN (
                      SELECT scanned_item_id FROM cross_references
                      WHERE entity_type = 'idea' AND entity_id = %s
                  )
            """, (loser_id, winner_id))
            cursor.execute(
                "UPDATE cross_references SET entity_id = %s WHERE entity_type = 'idea' AND entity_id = %s",
                (winner_id, loser_id)
            )

            # Step 5: Delete embedding_tracking for loser
            cursor.execute(
                "DELETE FROM embedding_tracking WHERE entity_type = 'ideas' AND entity_id = %s AND user_id = %s",
                (str(loser_id), user_id)
            )

            # Step 6: Delete loser
            cursor.execute("DELETE FROM ideas WHERE id = %s", (loser_id,))

        # Step 7: Post-commit ChromaDB cleanup
        try:
            from web.services.embedding_service import get_embedding_service
            emb = get_embedding_service()
            if emb.enabled:
                emb.delete_embeddings("ideas", [f"idea_{loser_id}"])
                delete_embedding_tracking("ideas", str(winner_id), user_id)
        except Exception as e:
            logger.warning("merge_ideas: ChromaDB cleanup failed (non-blocking): %s", repr(e))

        return {
            "success": True,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "loser_title": loser['title'],
            "winner_title": winner['title'],
        }

    except Exception as e:
        logger.error("merge_ideas error: %s", repr(e))
        return {"success": False, "error": str(e)}


def find_duplicate_people(user_id: int) -> list:
    """
    Find potential duplicate people using name similarity.

    Match types:
    - exact (1.0): case-insensitive exact match
    - contains (0.8): one name contains the other, both >= 3 chars
    - first_name (0.6): first word matches, both multi-word, first word >= 3 chars

    Uses union-find for transitive grouping. Filters out dismissed pairs.
    Returns groups sorted by confidence DESC.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, name FROM people WHERE user_id = %s AND name != 'Unknown'",
                (user_id,)
            )
            people = [dict(r) for r in cursor.fetchall()]

        if len(people) < 2:
            return []

        # Get dismissed pairs
        dismissed = get_dismissed_pairs(user_id, 'people')

        # Find matching pairs
        pairs = []
        for i in range(len(people)):
            for j in range(i + 1, len(people)):
                a, b = people[i], people[j]
                pair_key = (min(a['id'], b['id']), max(a['id'], b['id']))
                if pair_key in dismissed:
                    continue

                name_a = a['name'].strip().lower()
                name_b = b['name'].strip().lower()

                match_type = None
                confidence = 0.0

                if name_a == name_b:
                    match_type = 'exact'
                    confidence = 1.0
                elif len(name_a) >= 3 and len(name_b) >= 3 and (name_a in name_b or name_b in name_a):
                    match_type = 'contains'
                    confidence = 0.8
                else:
                    words_a = name_a.split()
                    words_b = name_b.split()
                    if (len(words_a) > 1 and len(words_b) > 1 and
                            len(words_a[0]) >= 3 and words_a[0] == words_b[0]):
                        match_type = 'first_name'
                        confidence = 0.6

                if match_type:
                    pairs.append((a['id'], b['id'], match_type, confidence))

        if not pairs:
            return []

        # Union-find for transitive grouping
        parent = {}
        def find(x):
            if x not in parent:
                parent[x] = x
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]
        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        pair_info = {}
        for a_id, b_id, match_type, confidence in pairs:
            union(a_id, b_id)
            pair_info[(min(a_id, b_id), max(a_id, b_id))] = (match_type, confidence)

        # Build groups
        groups = {}
        for p in people:
            pid = p['id']
            if pid in parent:
                root = find(pid)
                if root not in groups:
                    groups[root] = {'items': [], 'match_type': None, 'confidence': 0.0}
                groups[root]['items'].append(p)

        # Assign best match_type and confidence per group
        for (a_id, b_id), (match_type, confidence) in pair_info.items():
            root = find(a_id)
            if root in groups and confidence > groups[root]['confidence']:
                groups[root]['match_type'] = match_type
                groups[root]['confidence'] = confidence

        result = []
        for group_data in groups.values():
            if len(group_data['items']) >= 2:
                result.append({
                    'items': group_data['items'],
                    'match_type': group_data['match_type'],
                    'confidence': group_data['confidence'],
                })

        result.sort(key=lambda g: g['confidence'], reverse=True)
        return result

    except Exception as e:
        logger.error("find_duplicate_people error: %s", repr(e))
        return []


def find_duplicate_ideas(user_id: int) -> list:
    """
    Find potential duplicate ideas using title similarity.

    Match types:
    - exact (1.0): case-insensitive exact title match
    - contains (0.8): one title contains the other, both >= 5 chars
    - word_overlap (0.6): >= 60% shared non-stop words, both >= 3 content words

    Uses union-find for transitive grouping. Filters out dismissed pairs.
    """
    STOP_WORDS = {'the', 'a', 'an', 'to', 'for', 'of', 'and', 'in', 'on', 'with', 'is', 'it'}

    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, title FROM ideas WHERE user_id = %s",
                (user_id,)
            )
            ideas = [dict(r) for r in cursor.fetchall()]

        if len(ideas) < 2:
            return []

        dismissed = get_dismissed_pairs(user_id, 'ideas')

        def content_words(title):
            return [w for w in title.lower().split() if w not in STOP_WORDS and len(w) >= 2]

        pairs = []
        for i in range(len(ideas)):
            for j in range(i + 1, len(ideas)):
                a, b = ideas[i], ideas[j]
                pair_key = (min(a['id'], b['id']), max(a['id'], b['id']))
                if pair_key in dismissed:
                    continue

                title_a = a['title'].strip().lower()
                title_b = b['title'].strip().lower()

                match_type = None
                confidence = 0.0

                if title_a == title_b:
                    match_type = 'exact'
                    confidence = 1.0
                elif len(title_a) >= 5 and len(title_b) >= 5 and (title_a in title_b or title_b in title_a):
                    match_type = 'contains'
                    confidence = 0.8
                else:
                    words_a = set(content_words(a['title']))
                    words_b = set(content_words(b['title']))
                    if len(words_a) >= 3 and len(words_b) >= 3:
                        overlap = words_a & words_b
                        total = words_a | words_b
                        if total and len(overlap) / len(total) >= 0.6:
                            match_type = 'word_overlap'
                            confidence = 0.6

                if match_type:
                    pairs.append((a['id'], b['id'], match_type, confidence))

        if not pairs:
            return []

        # Union-find
        parent = {}
        def find(x):
            if x not in parent:
                parent[x] = x
            if parent[x] != x:
                parent[x] = find(parent[x])
            return parent[x]
        def union(x, y):
            px, py = find(x), find(y)
            if px != py:
                parent[px] = py

        pair_info = {}
        for a_id, b_id, match_type, confidence in pairs:
            union(a_id, b_id)
            pair_info[(min(a_id, b_id), max(a_id, b_id))] = (match_type, confidence)

        groups = {}
        for idea in ideas:
            iid = idea['id']
            if iid in parent:
                root = find(iid)
                if root not in groups:
                    groups[root] = {'items': [], 'match_type': None, 'confidence': 0.0}
                groups[root]['items'].append(idea)

        for (a_id, b_id), (match_type, confidence) in pair_info.items():
            root = find(a_id)
            if root in groups and confidence > groups[root]['confidence']:
                groups[root]['match_type'] = match_type
                groups[root]['confidence'] = confidence

        result = []
        for group_data in groups.values():
            if len(group_data['items']) >= 2:
                result.append({
                    'items': group_data['items'],
                    'match_type': group_data['match_type'],
                    'confidence': group_data['confidence'],
                })

        result.sort(key=lambda g: g['confidence'], reverse=True)
        return result

    except Exception as e:
        logger.error("find_duplicate_ideas error: %s", repr(e))
        return []


def dismiss_duplicate_pair(user_id: int, category: str, id_a: int, id_b: int) -> bool:
    """Store a normalized dismissed pair (smaller ID always id_a)."""
    try:
        normalized_a = min(id_a, id_b)
        normalized_b = max(id_a, id_b)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO dismissed_duplicates (user_id, category, id_a, id_b)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, category, id_a, id_b) DO NOTHING
            """, (user_id, category, normalized_a, normalized_b))
            return True
    except Exception as e:
        logger.error("dismiss_duplicate_pair error: %s", repr(e))
        return False


def get_dismissed_pairs(user_id: int, category: str) -> set:
    """Returns set of (id_a, id_b) tuples for filtering duplicate matches."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id_a, id_b FROM dismissed_duplicates WHERE user_id = %s AND category = %s",
                (user_id, category)
            )
            return {(r['id_a'], r['id_b']) for r in cursor.fetchall()}
    except Exception as e:
        logger.error("get_dismissed_pairs error: %s", repr(e))
        return set()


def search_ideas(user_id: int, query: str, limit: int = 20) -> list[dict]:
    """
    Search ideas using FTS5 full-text search.

    Args:
        user_id: User's unique identifier
        query: Search query
        limit: Maximum results to return

    Returns:
        List of matching idea dictionaries
    """
    if not query or not query.strip():
        return []

    original_query = query.strip()
    search_pattern = f'%{original_query}%'

    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT DISTINCT i.id, i.user_id, i.title, i.summary,
                       i.notes, i.tags, i.created_at, i.updated_at
                FROM ideas i
                WHERE (i.title ILIKE %s OR i.summary ILIKE %s OR i.notes ILIKE %s)
                AND i.user_id = %s
                ORDER BY i.created_at DESC
                LIMIT %s
            """, (search_pattern, search_pattern, search_pattern, user_id, limit))
            rows = cursor.fetchall()
            results = []
            for row in rows:
                row_dict = dict(row)
                snippet_text = row_dict.get('title') or row_dict.get('summary') or ''
                row_dict['snippet'] = extract_snippet(snippet_text, original_query)
                results.append(row_dict)
            return results
        except Exception:
            return []


# ============================================================================
# Second Brain - Inbox Log Functions
# ============================================================================

def log_inbox_entry(
    user_id: int,
    original_text: str,
    classification: str,
    confidence: float = None,
    routed_to_table: str = None,
    routed_to_id: int = None
) -> Optional[int]:
    """
    Log an entry to the inbox audit trail.

    Args:
        user_id: User's unique identifier
        original_text: What the user said
        classification: Category (people, project, idea, admin, unknown)
        confidence: AI confidence score (0.0-1.0)
        routed_to_table: Which table it was filed to
        routed_to_id: ID in that table

    Returns:
        Log entry ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO inbox_log
                (user_id, original_text, classification, confidence, routed_to_table, routed_to_id)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, original_text, classification, confidence, routed_to_table, routed_to_id))
            row = cursor.fetchone()

            return row['id'] if row else None
    except Exception as e:
        return None


def get_recent_inbox(user_id: int, limit: int = 50) -> list[dict]:
    """
    Get recent inbox log entries for a user.

    Args:
        user_id: User's unique identifier
        limit: Maximum entries to return

    Returns:
        List of inbox log entry dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, original_text, classification, confidence,
                   routed_to_table, routed_to_id, created_at
            FROM inbox_log
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


# ============================================================================
# User Profile Functions (system prompt template variables)
# ============================================================================

def get_user_profile(user_id: int) -> dict:
    """
    Get user profile for system prompt template injection.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with user_name, pronouns, user_context, key_people,
        key_projects, priorities, setup_complete
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_name, user_pronouns_subject, user_pronouns_object,
                   user_pronouns_possessive, user_context, key_people,
                   key_projects, priorities, setup_complete, personality_casual,
                   updated_at
            FROM user_settings WHERE user_id = %s
        """, (user_id,))
        row = cursor.fetchone()

        if not row:
            return {
                'user_name': 'User',
                'user_pronouns_subject': 'they',
                'user_pronouns_object': 'them',
                'user_pronouns_possessive': 'their',
                'user_context': '',
                'key_people': '[]',
                'key_projects': '[]',
                'priorities': '',
                'setup_complete': False,
                'personality_casual': False
            }

        return {
            'user_name': row['user_name'] or 'User',
            'user_pronouns_subject': row['user_pronouns_subject'] or 'they',
            'user_pronouns_object': row['user_pronouns_object'] or 'them',
            'user_pronouns_possessive': row['user_pronouns_possessive'] or 'their',
            'user_context': row['user_context'] or '',
            'key_people': row['key_people'] or '[]',
            'key_projects': row['key_projects'] or '[]',
            'priorities': row['priorities'] or '',
            'setup_complete': bool(row['setup_complete']) if row['setup_complete'] is not None else False,
            'personality_casual': bool(row['personality_casual']) if row['personality_casual'] is not None else False
        }


# ============================================================================
# Daily Digest Preferences Functions (08-07)
# ============================================================================

def get_digest_preferences(user_id: int) -> dict:
    """
    Get digest preferences for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with digest_enabled, digest_time, digest_email, digest_push, digest_timezone
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT digest_enabled, digest_time, digest_email, digest_push, digest_timezone
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            # Return defaults if user has no settings yet
            return {
                'digest_enabled': True,
                'digest_time': '07:00',
                'digest_email': True,
                'digest_push': True,
                'digest_timezone': 'America/Chicago'
            }

        return {
            'digest_enabled': bool(row['digest_enabled']) if row['digest_enabled'] is not None else True,
            'digest_time': row['digest_time'] or '07:00',
            'digest_email': bool(row['digest_email']) if row['digest_email'] is not None else True,
            'digest_push': bool(row['digest_push']) if row['digest_push'] is not None else True,
            'digest_timezone': row['digest_timezone'] or 'America/Chicago'
        }


def update_digest_preferences(
    user_id: int,
    digest_enabled: bool = None,
    digest_time: str = None,
    digest_email: bool = None,
    digest_push: bool = None,
    digest_timezone: str = None
) -> bool:
    """
    Update digest preferences for a user.

    Args:
        user_id: User's unique identifier
        digest_enabled: Enable/disable digest
        digest_time: Time to send digest (HH:MM format)
        digest_email: Send via email
        digest_push: Send via push notification
        digest_timezone: User's timezone (IANA format)

    Returns:
        True if updated successfully
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # First ensure user_settings row exists
            cursor.execute("""
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT DO NOTHING
            """, (user_id,))

            # Build UPDATE statement for provided fields
            updates = []
            values = []

            if digest_enabled is not None:
                updates.append("digest_enabled = %s")
                values.append(1 if digest_enabled else 0)

            if digest_time is not None:
                updates.append("digest_time = %s")
                values.append(digest_time)

            if digest_email is not None:
                updates.append("digest_email = %s")
                values.append(1 if digest_email else 0)

            if digest_push is not None:
                updates.append("digest_push = %s")
                values.append(1 if digest_push else 0)

            if digest_timezone is not None:
                updates.append("digest_timezone = %s")
                values.append(digest_timezone)

            if not updates:
                return True  # Nothing to update

            updates.append("updated_at = CURRENT_TIMESTAMP")
            values.append(user_id)

            cursor.execute(f"""
                UPDATE user_settings
                SET {', '.join(updates)}
                WHERE user_id = %s
            """, values)

            return True

    except Exception as e:
        return False


def get_users_for_digest(current_hour: int, timezone: str = None) -> list[dict]:
    """
    Get users whose digest should be sent at the current hour.

    Args:
        current_hour: Current hour (0-23) in UTC
        timezone: Optional filter for specific timezone

    Returns:
        List of dicts with user_id and preferences
    """
    with get_db() as conn:
        cursor = conn.cursor()

        if timezone:
            cursor.execute("""
                SELECT user_id, digest_enabled, digest_time, digest_email,
                       digest_push, digest_timezone
                FROM user_settings
                WHERE digest_enabled = 1
                AND digest_timezone = %s
            """, (timezone,))
        else:
            cursor.execute("""
                SELECT user_id, digest_enabled, digest_time, digest_email,
                       digest_push, digest_timezone
                FROM user_settings
                WHERE digest_enabled = 1
            """)

        users = []
        for row in cursor.fetchall():
            # Parse the digest_time (HH:MM) and compare with current_hour
            # after adjusting for timezone
            digest_time = row['digest_time'] or '07:00'
            try:
                digest_hour = int(digest_time.split(':')[0])
            except (ValueError, IndexError):
                digest_hour = 7

            user_tz = row['digest_timezone'] or 'America/Chicago'

            # Get current hour in user's timezone
            from datetime import datetime
            from zoneinfo import ZoneInfo

            try:
                now_utc = datetime.now(ZoneInfo('UTC'))
                now_user_tz = now_utc.astimezone(ZoneInfo(user_tz))
                user_current_hour = now_user_tz.hour

                if user_current_hour == digest_hour:
                    users.append({
                        'user_id': row['user_id'],
                        'digest_enabled': bool(row['digest_enabled']),
                        'digest_time': digest_time,
                        'digest_email': bool(row['digest_email']),
                        'digest_push': bool(row['digest_push']),
                        'digest_timezone': user_tz
                    })
            except Exception:
                # Invalid timezone, skip this user
                continue

        return users


# ============================================================================
# Channel Exclusion Preferences Functions (16-05)
# ============================================================================

def get_channel_exclusion_preferences(user_id: int) -> dict:
    """
    Get channel exclusion preferences for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with slack_excluded_channels and telegram_excluded_chats (as lists)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT slack_excluded_channels, telegram_excluded_chats
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            # Return defaults if user has no settings yet
            return {
                'slack_excluded_channels': [],
                'telegram_excluded_chats': []
            }

        # Parse JSON strings to lists
        slack_excluded = []
        telegram_excluded = []

        if row['slack_excluded_channels']:
            try:
                slack_excluded = json.loads(row['slack_excluded_channels'])
            except (json.JSONDecodeError, TypeError):
                slack_excluded = []

        if row['telegram_excluded_chats']:
            try:
                telegram_excluded = json.loads(row['telegram_excluded_chats'])
            except (json.JSONDecodeError, TypeError):
                telegram_excluded = []

        return {
            'slack_excluded_channels': slack_excluded,
            'telegram_excluded_chats': telegram_excluded
        }


def update_channel_exclusion_preferences(
    user_id: int,
    slack_excluded_channels: list = None,
    telegram_excluded_chats: list = None
) -> bool:
    """
    Update channel exclusion preferences for a user.

    Args:
        user_id: User's unique identifier
        slack_excluded_channels: List of Slack channel IDs to exclude
        telegram_excluded_chats: List of Telegram chat IDs to exclude

    Returns:
        True if updated successfully
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # First ensure user_settings row exists
            cursor.execute("""
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT DO NOTHING
            """, (user_id,))

            # Build UPDATE statement for provided fields
            updates = []
            values = []

            if slack_excluded_channels is not None:
                updates.append("slack_excluded_channels = %s")
                values.append(json.dumps(slack_excluded_channels))

            if telegram_excluded_chats is not None:
                updates.append("telegram_excluded_chats = %s")
                values.append(json.dumps(telegram_excluded_chats))

            if not updates:
                return True  # Nothing to update

            updates.append("updated_at = CURRENT_TIMESTAMP")
            values.append(user_id)

            cursor.execute(f"""
                UPDATE user_settings
                SET {', '.join(updates)}
                WHERE user_id = %s
            """, values)

            return True

    except Exception as e:
        return False


# ============================================================================
# Weekly Review Preferences Functions (08-08)
# ============================================================================

def get_weekly_review_preferences(user_id: int) -> dict:
    """
    Get weekly review preferences for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with weekly_review_enabled, weekly_review_day, weekly_review_time
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT weekly_review_enabled, weekly_review_day, weekly_review_time,
                   digest_timezone
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            # Return defaults if user has no settings yet
            return {
                'weekly_review_enabled': True,
                'weekly_review_day': 'sunday',
                'weekly_review_time': '18:00',
                'timezone': 'America/Chicago'
            }

        return {
            'weekly_review_enabled': bool(row['weekly_review_enabled']) if row['weekly_review_enabled'] is not None else True,
            'weekly_review_day': row['weekly_review_day'] or 'sunday',
            'weekly_review_time': row['weekly_review_time'] or '18:00',
            'timezone': row['digest_timezone'] or 'America/Chicago'
        }


def update_weekly_review_preferences(
    user_id: int,
    weekly_review_enabled: bool = None,
    weekly_review_day: str = None,
    weekly_review_time: str = None
) -> bool:
    """
    Update weekly review preferences for a user.

    Args:
        user_id: User's unique identifier
        weekly_review_enabled: Enable/disable weekly review
        weekly_review_day: Day to send review (sunday, saturday, friday)
        weekly_review_time: Time to send review (HH:MM format)

    Returns:
        True if successful, False otherwise
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Check if user_settings row exists
            cursor.execute("SELECT 1 FROM user_settings WHERE user_id = %s", (user_id,))
            exists = cursor.fetchone()

            if not exists:
                # Create row with defaults
                cursor.execute("""
                    INSERT INTO user_settings (user_id, weekly_review_enabled, weekly_review_day, weekly_review_time)
                    VALUES (%s, %s, %s, %s)
                """, (
                    user_id,
                    1 if weekly_review_enabled is None or weekly_review_enabled else 0,
                    weekly_review_day or 'sunday',
                    weekly_review_time or '18:00'
                ))
            else:
                # Build update query dynamically
                updates = []
                values = []

                if weekly_review_enabled is not None:
                    updates.append("weekly_review_enabled = %s")
                    values.append(1 if weekly_review_enabled else 0)

                if weekly_review_day is not None:
                    updates.append("weekly_review_day = %s")
                    values.append(weekly_review_day)

                if weekly_review_time is not None:
                    updates.append("weekly_review_time = %s")
                    values.append(weekly_review_time)

                if not updates:
                    return True  # Nothing to update

                updates.append("updated_at = CURRENT_TIMESTAMP")
                values.append(user_id)

                cursor.execute(f"""
                    UPDATE user_settings
                    SET {', '.join(updates)}
                    WHERE user_id = %s
                """, values)

            return True

    except Exception as e:
        return False


def get_users_for_weekly_review(current_day: str, current_hour: int) -> list[dict]:
    """
    Get users whose weekly review should be sent at the current day and hour.

    Args:
        current_day: Current day name (lowercase, e.g., 'sunday')
        current_hour: Current hour (0-23) in UTC

    Returns:
        List of dicts with user_id and preferences
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT user_id, weekly_review_enabled, weekly_review_day,
                   weekly_review_time, digest_timezone
            FROM user_settings
            WHERE weekly_review_enabled = 1
            AND LOWER(weekly_review_day) = %s
        """, (current_day.lower(),))

        users = []
        for row in cursor.fetchall():
            review_time = row['weekly_review_time'] or '18:00'
            try:
                review_hour = int(review_time.split(':')[0])
            except (ValueError, IndexError):
                review_hour = 18

            user_tz = row['digest_timezone'] or 'America/Chicago'

            # Get current hour in user's timezone
            from datetime import datetime
            from zoneinfo import ZoneInfo

            try:
                now_utc = datetime.now(ZoneInfo('UTC'))
                now_user_tz = now_utc.astimezone(ZoneInfo(user_tz))
                user_current_hour = now_user_tz.hour
                user_current_day = now_user_tz.strftime('%A').lower()

                # Check if both day and hour match
                if user_current_day == current_day.lower() and user_current_hour == review_hour:
                    users.append({
                        'user_id': row['user_id'],
                        'weekly_review_enabled': bool(row['weekly_review_enabled']),
                        'weekly_review_day': row['weekly_review_day'],
                        'weekly_review_time': review_time,
                        'timezone': user_tz
                    })
            except Exception:
                # Invalid timezone, skip this user
                continue

        return users


# ============================================================================
# Scanner Engine & Entity Resolution
# ============================================================================


def create_scanner_run(user_id: int, source: str) -> Optional[int]:
    """
    Create a new scanner run record.

    Args:
        user_id: User's unique identifier
        source: Data source name (gmail, slack, telegram, etc.)

    Returns:
        Run ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO scanner_runs (user_id, source, started_at, status)
                VALUES (%s, %s, CURRENT_TIMESTAMP, 'running')
                RETURNING id
            """, (user_id, source))
            row = cursor.fetchone()

            return row['id'] if row else None
    except Exception as e:
        return None


def complete_scanner_run(
    run_id: int,
    status: str,
    items_found: int,
    items_new: int,
    error_message: str = None
) -> bool:
    """
    Mark a scanner run as completed.

    Args:
        run_id: Scanner run ID
        status: Final status (completed, failed, partial)
        items_found: Total items found during scan
        items_new: Number of new items discovered
        error_message: Optional error message if failed

    Returns:
        True if updated, False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scanner_runs
                SET completed_at = CURRENT_TIMESTAMP,
                    status = %s,
                    items_found = %s,
                    items_new = %s,
                    error_message = %s
                WHERE id = %s
            """, (status, items_found, items_new, error_message, run_id))
            return cursor.rowcount > 0
    except Exception as e:
        return False


def insert_scanned_item(
    user_id: int,
    scanner_run_id: int,
    source: str,
    source_id: str,
    source_metadata: str,
    item_type: str,
    direction: str = 'inbound'
) -> Optional[int]:
    """
    Insert a scanned item, deduplicating by (user_id, source, source_id).

    Args:
        user_id: User's unique identifier
        scanner_run_id: ID of the scanner run that found this item.
            scanner_run_id may be None for drip-mode scanners (Slack drip loop)
        source: Data source name
        source_id: Unique identifier within the source
        source_metadata: JSON string with source-specific metadata
        item_type: Type of item (email, slack_message, etc.)
        direction: Communication direction — 'inbound' (default) or 'outbound'

    Returns:
        Item ID if inserted, None if duplicate or error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO scanned_items
                (user_id, scanner_run_id, source, source_id, source_metadata, item_type, direction)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (user_id, scanner_run_id, source, source_id, source_metadata, item_type, direction))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        return None


def get_last_scanner_run(user_id: int, source: str) -> Optional[dict]:
    """
    Get the most recent completed scanner run for a source.

    Args:
        user_id: User's unique identifier
        source: Data source name

    Returns:
        Dict with run details, or None if no completed runs
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, source, started_at, completed_at,
                   status, items_found, items_new, error_message
            FROM scanner_runs
            WHERE user_id = %s AND source = %s AND status = 'completed'
            ORDER BY completed_at DESC
            LIMIT 1
        """, (user_id, source))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


# ============================================================
# Slack Drip Scanner — Channel Cursor Helpers
# ============================================================

def get_slack_channel_cursor(user_id: int, team_id: str, channel_id: str) -> Optional[dict]:
    """Get the scan cursor for a specific Slack channel.

    Returns dict with all columns from slack_channel_cursors, or None if channel
    has never been registered in the drip loop.

    Key fields the drip loop uses:
    - last_scan_ts: Unix epoch string to pass as 'oldest' to conversations.history
    - circuit_state: 'closed', 'open', 'half_open'
    - circuit_opened_at: Datetime when circuit opened (for 15-min recovery check)
    - is_excluded: Whether user has excluded this channel in settings
    - consecutive_failures: For circuit breaker logic
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM slack_channel_cursors
            WHERE user_id = %s AND team_id = %s AND channel_id = %s
        """, (user_id, team_id, channel_id))
        row = cursor.fetchone()
        return dict(row) if row else None


def upsert_slack_channel_cursor(
    user_id: int,
    team_id: str,
    channel_id: str,
    channel_name: str = None,
    channel_type: str = 'channel',
    is_excluded: bool = False,
    last_scan_ts: str = None,
    last_error: str = None,
    consecutive_failures: int = None,
    circuit_state: str = None,
    circuit_opened_at: str = None,
) -> None:
    """Insert or update a channel cursor record.

    Used in two scenarios:
    1. Channel registration: drip loop discovers a new channel, registers it
    2. Post-scan update: drip loop finished scanning a channel, updates its cursor

    Only updates fields that are explicitly passed (not None). This allows the
    drip loop to call this with just last_scan_ts after a successful scan without
    accidentally resetting circuit_state.

    Exception: consecutive_failures=0 is valid and must be written (reset after success).
    Use the sentinel None to mean "don't update this field".
    """
    with get_db() as conn:
        cursor = conn.cursor()
        # First, ensure the row exists
        cursor.execute("""
            INSERT INTO slack_channel_cursors
                (user_id, team_id, channel_id, channel_name, channel_type, is_excluded)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (user_id, team_id, channel_id, channel_name, channel_type, 1 if is_excluded else 0))

        # Build dynamic UPDATE for only the fields being changed
        updates = ["updated_at = CURRENT_TIMESTAMP"]
        params = []

        if channel_name is not None:
            updates.append("channel_name = %s")
            params.append(channel_name)
        if channel_type is not None:
            updates.append("channel_type = %s")
            params.append(channel_type)
        updates.append("is_excluded = %s")
        params.append(1 if is_excluded else 0)
        if last_scan_ts is not None:
            updates.append("last_scan_ts = %s")
            params.append(last_scan_ts)
            updates.append("last_scan_at = CURRENT_TIMESTAMP")
        if last_error is not None:
            updates.append("last_error = %s")
            params.append(last_error)
        elif last_error == '':  # Explicit clear
            updates.append("last_error = NULL")
        if consecutive_failures is not None:
            updates.append("consecutive_failures = %s")
            params.append(consecutive_failures)
        if circuit_state is not None:
            updates.append("circuit_state = %s")
            params.append(circuit_state)
        if circuit_opened_at is not None:
            updates.append("circuit_opened_at = %s")
            params.append(circuit_opened_at)

        params.extend([user_id, team_id, channel_id])
        cursor.execute(f"""
            UPDATE slack_channel_cursors
            SET {', '.join(updates)}
            WHERE user_id = %s AND team_id = %s AND channel_id = %s
        """, params)
        conn.commit()


def get_next_slack_channel_to_scan(user_id: int, team_id: str) -> Optional[dict]:
    """Return the channel that should be scanned next (round-robin by staleness).

    Algorithm: Pick the non-excluded channel with the oldest last_scan_at.
    Channels never scanned (last_scan_at IS NULL) are treated as most stale (priority).

    Returns None if all channels are excluded or no channels are registered.

    The drip loop calls this every 10 seconds to decide which channel to scan next.
    By always picking the most stale channel, we naturally implement round-robin
    without needing explicit position tracking.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM slack_channel_cursors
            WHERE user_id = %s AND team_id = %s AND is_excluded = 0
              AND channel_id != '__workspace__'
            ORDER BY
                CASE WHEN last_scan_at IS NULL THEN 0 ELSE 1 END ASC,
                last_scan_at ASC
            LIMIT 1
        """, (user_id, team_id))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_slack_drip_status(user_id: int) -> dict:
    """Get summary status of the Slack drip scanner for API display.

    Used by /api/scanner/status to report Slack scanner health to the frontend.
    Returns equivalent data to what get_last_scanner_run() returns for other sources,
    but derived from slack_channel_cursors instead of scanner_runs.

    Returns:
    {
        'last_scan': ISO datetime of most recently scanned channel (or None),
        'status': 'never_run' | 'active' | 'circuit_open' | 'all_excluded',
        'channels_tracked': total channel count,
        'channels_excluded': excluded count,
        'channels_with_open_circuit': count of channels with circuit_state='open',
        'items_found_24h': count of slack scanned_items in last 24 hours,
    }
    """
    with get_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                MAX(last_scan_at) as last_scan,
                COUNT(*) as total,
                SUM(CASE WHEN is_excluded = 1 THEN 1 ELSE 0 END) as excluded,
                SUM(CASE WHEN circuit_state = 'open' THEN 1 ELSE 0 END) as open_circuits
            FROM slack_channel_cursors
            WHERE user_id = %s
        """, (user_id,))
        row = cursor.fetchone()

        cursor.execute("""
            SELECT COUNT(*) as cnt FROM scanned_items
            WHERE user_id = %s AND source = 'slack'
            AND detected_at > NOW() - INTERVAL '24 hours'
        """, (user_id,))
        items_row = cursor.fetchone()

        total = row['total'] if row else 0
        excluded = row['excluded'] if row else 0
        open_circuits = row['open_circuits'] if row else 0
        last_scan = row['last_scan'] if row else None
        items_24h = items_row['cnt'] if items_row else 0

        if total == 0:
            status = 'never_run'
        elif total == excluded:
            status = 'all_excluded'
        elif open_circuits > 0 and open_circuits == (total - excluded):
            status = 'circuit_open'
        else:
            status = 'active'

        return {
            'last_scan': last_scan,
            'status': status,
            'channels_tracked': total,
            'channels_excluded': excluded,
            'channels_with_open_circuit': open_circuits,
            'items_found_24h': items_24h,
        }


def upsert_entity_mapping(
    user_id: int,
    source: str,
    source_identifier: str,
    display_name: str,
    person_id: int = None,
    confidence: float = 1.0
) -> Optional[int]:
    """
    Insert or update an entity mapping for cross-source identity resolution.

    Args:
        user_id: User's unique identifier
        source: Data source name
        source_identifier: Identifier within the source (email, user ID, etc.)
        display_name: Name as displayed in the source
        person_id: Optional link to people table
        confidence: Match confidence (1.0 = exact)

    Returns:
        Mapping ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO entity_mappings
                (user_id, source, source_identifier, display_name, person_id, confidence)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(user_id, source, source_identifier) DO UPDATE SET
                    display_name = excluded.display_name,
                    person_id = COALESCE(excluded.person_id, entity_mappings.person_id),
                    confidence = excluded.confidence,
                    updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """, (user_id, source, source_identifier, display_name, person_id, confidence))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        return None


def get_entity_mappings_for_person(person_id: int) -> list[dict]:
    """
    Get all entity mappings linked to a person.

    Args:
        person_id: Person's unique identifier

    Returns:
        List of mapping dicts
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, person_id, source, source_identifier,
                   display_name, confidence, created_at, updated_at
            FROM entity_mappings
            WHERE person_id = %s
        """, (person_id,))
        return [dict(row) for row in cursor.fetchall()]


def resolve_entity(user_id: int, source: str, source_identifier: str) -> Optional[dict]:
    """
    Look up the person_id for a source identity.

    Args:
        user_id: User's unique identifier
        source: Data source name
        source_identifier: Identifier within the source

    Returns:
        Mapping dict with person_id, or None if not found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, person_id, source, source_identifier,
                   display_name, confidence, created_at, updated_at
            FROM entity_mappings
            WHERE user_id = %s AND source = %s AND source_identifier = %s
        """, (user_id, source, source_identifier))
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


def update_entity_mapping_person(mapping_id: int, person_id: int, confidence: float) -> bool:
    """
    Update an entity mapping to link it to a person.

    Args:
        mapping_id: Entity mapping ID
        person_id: Person to link to
        confidence: Match confidence

    Returns:
        True if updated, False otherwise
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE entity_mappings
                SET person_id = %s, confidence = %s, updated_at = NOW()
                WHERE id = %s
            """, (person_id, confidence, mapping_id))
            return cursor.rowcount > 0
    except Exception as e:
        logger.error("Error updating entity mapping %d: %s", mapping_id, repr(e))
        return False


# ============================================================================
# Classification Helper Functions
# ============================================================================

import logging as _logging
_classification_logger = _logging.getLogger(__name__)


def insert_item_classification(
    user_id: int,
    scanned_item_id: int,
    relevance: str,
    urgency: str = "normal",
    summary: str = None,
    extracted_entities: str = None,
    extracted_actions: str = None,
    model_used: str = None,
    thread_context: str = None,
    thread_summary: str = None,
    thread_id: str = None
) -> Optional[int]:
    """
    Insert a classification result for a scanned item.

    Args:
        user_id: User's unique identifier
        scanned_item_id: ID of the scanned item being classified
        relevance: Classification result ('actionable', 'informational', 'noise', 'internal_reference', 'filtered')
        urgency: Urgency level ('urgent', 'normal', 'low')
        summary: One-line AI summary
        extracted_entities: JSON string of detected entities
        extracted_actions: JSON string of detected actions
        model_used: AI model used ('haiku', 'sonnet', or None for rule-based)
        thread_context: Full thread context string for Slack/Telegram threads (Phase 22)
        thread_summary: AI-generated summary of thread context (Phase 22)
        thread_id: Thread timestamp/ID for grouping messages in the same thread (Phase 22)

    Returns:
        Classification ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO item_classifications
                (user_id, scanned_item_id, relevance, urgency, summary,
                 extracted_entities, extracted_actions, model_used,
                 thread_context, thread_summary, thread_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (user_id, scanned_item_id, relevance, urgency, summary,
                  extracted_entities, extracted_actions, model_used,
                  thread_context, thread_summary, thread_id))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _classification_logger.error("Failed to insert item classification: %s", repr(e))
        return None


def insert_cross_reference(
    user_id: int,
    scanned_item_id: int,
    entity_type: str,
    entity_id: int,
    relationship: str = None,
    confidence: float = 1.0
) -> Optional[int]:
    """
    Insert a cross-reference linking a scanned item to a Second Brain entity.

    Args:
        user_id: User's unique identifier
        scanned_item_id: ID of the scanned item
        entity_type: Type of entity ('person', 'project', 'idea', 'task')
        entity_id: ID in the referenced table
        relationship: Relationship type ('mentioned', 'from', 'about', 'assigned', 'deadline')
        confidence: Match confidence (1.0 = exact)

    Returns:
        Cross-reference ID if inserted, None if duplicate or error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO cross_references
                (user_id, scanned_item_id, entity_type, entity_id, relationship, confidence)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING id
            """, (user_id, scanned_item_id, entity_type, entity_id, relationship, confidence))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _classification_logger.error("Failed to insert cross reference: %s", repr(e))
        return None


def insert_detected_action(
    user_id: int,
    scanned_item_id: int,
    action_text: str,
    action_type: str = "follow_up",
    person_name: str = None,
    person_id: int = None,
    deadline: str = None
) -> Optional[int]:
    """
    Insert a detected action from a classified item.

    Args:
        user_id: User's unique identifier
        scanned_item_id: ID of the scanned item
        action_text: Description of the action
        action_type: Type ('reply', 'follow_up', 'commitment', 'deadline', 'review')
        person_name: Linked person name if detected
        person_id: Linked person ID if resolved
        deadline: ISO 8601 deadline if detected

    Returns:
        Action ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO detected_actions
                (user_id, scanned_item_id, action_text, action_type, person_name, person_id, deadline)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, scanned_item_id, action_text, action_type, person_name, person_id, deadline))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _classification_logger.error("Failed to insert detected action: %s", repr(e))
        return None


def get_pending_actions(user_id: int, limit: int = 50) -> list[dict]:
    """
    Get pending detected actions for a user.

    Args:
        user_id: User's unique identifier
        limit: Maximum actions to return

    Returns:
        List of pending action dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT da.id, da.user_id, da.scanned_item_id, da.action_text,
                   da.action_type, da.person_name, da.person_id, da.deadline,
                   da.status, da.promoted_task_id, da.detected_at,
                   si.source, si.source_id, si.source_metadata, si.item_type
            FROM detected_actions da
            JOIN scanned_items si ON da.scanned_item_id = si.id
            WHERE da.user_id = %s AND da.status = 'pending'
            ORDER BY da.detected_at DESC
            LIMIT %s
        """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_cross_references_for_entity(
    user_id: int, entity_type: str, entity_id: int, limit: int = 50
) -> list[dict]:
    """
    Get all cross-references for a specific entity (person, project, idea, task).

    Args:
        user_id: User's unique identifier
        entity_type: Type of entity ('person', 'project', 'idea', 'task')
        entity_id: ID in the referenced table
        limit: Maximum references to return

    Returns:
        List of cross-reference dictionaries with scanned item context
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT cr.id, cr.scanned_item_id, cr.entity_type, cr.entity_id,
                   cr.relationship, cr.confidence, cr.created_at,
                   si.source, si.source_id, si.source_metadata, si.item_type, si.detected_at
            FROM cross_references cr
            JOIN scanned_items si ON cr.scanned_item_id = si.id
            WHERE cr.user_id = %s AND cr.entity_type = %s AND cr.entity_id = %s
            ORDER BY si.detected_at DESC
            LIMIT %s
        """, (user_id, entity_type, entity_id, limit))
        return [dict(row) for row in cursor.fetchall()]


_enrichment_logger = logging.getLogger(__name__ + '.enrichment')


def get_nudges_by_source(
    user_id: int, source_type: str, source_id: int, limit: int = 5
) -> list[dict]:
    """
    Get recent nudges sent for a specific source entity.

    Used to enrich people_get/project_get responses with nudge history.
    Example: source_type='person', source_id=person_id returns relationship_check nudges.

    Args:
        user_id: User's unique identifier
        source_type: Type of source entity ('person', 'project', etc.)
        source_id: ID of the source entity
        limit: Maximum nudges to return (default 5)

    Returns:
        List of nudge dicts ordered by created_at DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nudge_type, channel, title, urgency, status, created_at
                FROM nudges
                WHERE user_id = %s AND source_type = %s AND source_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, source_type, source_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _enrichment_logger.error("Failed to get nudges by source: %s", repr(e))
        return []


def get_unanswered_task_nudges(
    user_id: int, min_hours: int = 4, max_hours: int = 24
) -> list[dict]:
    """
    Find overdue_task nudges sent min_hours–max_hours ago with no user response
    and no follow-up nudge already created for them.

    Used by the nudge follow-up loop to send gentle reminders.

    Args:
        user_id: User's database ID
        min_hours: Minimum hours since nudge was sent (default 4)
        max_hours: Maximum hours since nudge was sent (default 24)

    Returns:
        List of nudge dicts (id, title, body, source_id, source_type, sent_at)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cutoff_min = datetime.now(timezone.utc) - timedelta(hours=min_hours)
        cutoff_max = datetime.now(timezone.utc) - timedelta(hours=max_hours)
        cursor.execute("""
            SELECT n.id, n.nudge_type, n.title, n.body,
                   n.source_id, n.source_type, n.sent_at
            FROM nudges n
            WHERE n.user_id = %s
              AND n.nudge_type = 'overdue_task'
              AND n.status = 'sent'
              AND n.user_response IS NULL
              AND n.sent_at < %s
              AND n.sent_at > %s
              AND NOT EXISTS (
                  SELECT 1 FROM nudges f
                  WHERE f.user_id = n.user_id
                    AND f.nudge_type = 'nudge_followup'
                    AND f.source_type = 'nudge'
                    AND f.source_id = n.id
              )
            ORDER BY n.sent_at ASC
            LIMIT 10
        """, (user_id, cutoff_min, cutoff_max))
        return [dict(row) for row in cursor.fetchall()]


def get_related_projects_for_person(
    user_id: int, person_id: int, limit: int = 5
) -> list[dict]:
    """
    Find active projects that appear in scanned items also cross-referenced to this person.

    Uses the cross_references table to infer person-project co-occurrence:
    finds projects mentioned in the same inbound items as the person.

    Args:
        user_id: User's unique identifier
        person_id: Person's ID in the people table
        limit: Maximum projects to return (default 5)

    Returns:
        List of project dicts (id, name, status, next_action) ordered by updated_at DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT p.id, p.name, p.status, p.next_action
                FROM projects p
                JOIN cross_references cr_proj
                  ON cr_proj.entity_type = 'project' AND cr_proj.entity_id = p.id
                WHERE p.user_id = %s
                  AND p.status = 'active'
                  AND cr_proj.scanned_item_id IN (
                      SELECT scanned_item_id
                      FROM cross_references
                      WHERE user_id = %s AND entity_type = 'person' AND entity_id = %s
                  )
                ORDER BY p.updated_at DESC
                LIMIT %s
            """, (user_id, user_id, person_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _enrichment_logger.error("Failed to get related projects for person: %s", repr(e))
        return []


def get_related_people_for_project(
    user_id: int, project_id: int, limit: int = 5
) -> list[dict]:
    """
    Find people who appear in scanned items also cross-referenced to this project.

    Uses the cross_references table to infer project-person co-occurrence.

    Args:
        user_id: User's unique identifier
        project_id: Project's ID in the projects table
        limit: Maximum people to return (default 5)

    Returns:
        List of person dicts (id, name, context) ordered by last_contact_date DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT pe.id, pe.name, pe.context
                FROM people pe
                JOIN cross_references cr_person
                  ON cr_person.entity_type = 'person' AND cr_person.entity_id = pe.id
                WHERE pe.user_id = %s
                  AND cr_person.scanned_item_id IN (
                      SELECT scanned_item_id
                      FROM cross_references
                      WHERE user_id = %s AND entity_type = 'project' AND entity_id = %s
                  )
                ORDER BY pe.last_contact_date DESC NULLS LAST
                LIMIT %s
            """, (user_id, user_id, project_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _enrichment_logger.error("Failed to get related people for project: %s", repr(e))
        return []


def get_open_tasks_for_project(
    user_id: int, project_name: str, limit: int = 5
) -> list[dict]:
    """
    Get open tasks associated with a project by name.

    Uses the tasks.project TEXT field (exact case-insensitive match).

    Args:
        user_id: User's unique identifier
        project_name: Project name to match against tasks.project field
        limit: Maximum tasks to return (default 5)

    Returns:
        List of task dicts (id, title, priority, due_date, status) ordered by priority DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, title, priority, due_date, status
                FROM tasks
                WHERE user_id = %s
                  AND LOWER(project) = LOWER(%s)
                  AND status NOT IN ('completed', 'cancelled')
                ORDER BY
                  CASE priority
                    WHEN 'urgent' THEN 1
                    WHEN 'high' THEN 2
                    WHEN 'medium' THEN 3
                    WHEN 'low' THEN 4
                    ELSE 5
                  END,
                  due_date ASC NULLS LAST
                LIMIT %s
            """, (user_id, project_name, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _enrichment_logger.error("Failed to get open tasks for project: %s", repr(e))
        return []


def get_actionable_items(user_id: int, since: str = None, limit: int = 50) -> list[dict]:
    """
    Get actionable classified items with full scanned item context.

    Args:
        user_id: User's unique identifier
        since: ISO 8601 timestamp to filter from (optional)
        limit: Maximum items to return

    Returns:
        List of actionable item dictionaries
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if since:
            cursor.execute("""
                SELECT ic.id, ic.scanned_item_id, ic.relevance, ic.urgency,
                       ic.summary, ic.extracted_entities, ic.extracted_actions,
                       ic.model_used, ic.classified_at,
                       si.source, si.source_id, si.source_metadata, si.item_type, si.detected_at
                FROM item_classifications ic
                JOIN scanned_items si ON ic.scanned_item_id = si.id
                WHERE ic.user_id = %s AND ic.relevance = 'actionable' AND ic.classified_at >= %s
                ORDER BY ic.classified_at DESC
                LIMIT %s
            """, (user_id, since, limit))
        else:
            cursor.execute("""
                SELECT ic.id, ic.scanned_item_id, ic.relevance, ic.urgency,
                       ic.summary, ic.extracted_entities, ic.extracted_actions,
                       ic.model_used, ic.classified_at,
                       si.source, si.source_id, si.source_metadata, si.item_type, si.detected_at
                FROM item_classifications ic
                JOIN scanned_items si ON ic.scanned_item_id = si.id
                WHERE ic.user_id = %s AND ic.relevance = 'actionable'
                ORDER BY ic.classified_at DESC
                LIMIT %s
            """, (user_id, limit))
        return [dict(row) for row in cursor.fetchall()]


def get_needs_reply_items(user_id: int, since: str = None, limit: int = 5) -> list[dict]:
    """
    Get items from the last 24h that need user reply, based on classification.

    Looks for actionable items where extracted_actions JSON contains
    actions with type "reply".

    Args:
        user_id: User's unique identifier
        since: ISO 8601 timestamp to filter from (optional, defaults to 24h ago)
        limit: Maximum items to return

    Returns:
        List of needs-reply item dictionaries
    """
    if since is None:
        from datetime import datetime, timedelta
        since = (datetime.now() - timedelta(hours=24)).isoformat()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ic.id, ic.scanned_item_id, ic.relevance, ic.urgency,
                   ic.summary, ic.extracted_entities, ic.extracted_actions,
                   ic.classified_at,
                   si.source, si.source_id, si.source_metadata, si.item_type, si.detected_at
            FROM item_classifications ic
            JOIN scanned_items si ON ic.scanned_item_id = si.id
            WHERE ic.user_id = %s AND ic.relevance = 'actionable'
            AND ic.classified_at >= %s
            ORDER BY ic.classified_at DESC
            LIMIT %s
        """, (user_id, since, limit * 3))  # Fetch extra since we filter in Python

        results = []
        for row in cursor.fetchall():
            row_dict = dict(row)
            # Check if extracted_actions contains a "reply" type action
            try:
                import json as _json
                actions = _json.loads(row_dict.get('extracted_actions') or '[]')
                if any(a.get('type') == 'reply' for a in actions):
                    results.append(row_dict)
            except (ValueError, TypeError):
                continue
            if len(results) >= limit:
                break

        return results


def update_detected_action_status(
    action_id: int, status: str, promoted_task_id: int = None
) -> bool:
    """
    Update the status of a detected action.

    Args:
        action_id: ID of the detected action
        status: New status ('pending', 'promoted', 'dismissed')
        promoted_task_id: Task ID if user promoted this action to a task

    Returns:
        True if updated, False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE detected_actions
                SET status = %s, promoted_task_id = %s
                WHERE id = %s
            """, (status, promoted_task_id, action_id))
            updated = cursor.rowcount > 0
            # Cascade: close any pending/sent nudges for this DA
            # so the item cannot reappear in a future batch
            if status in ('dismissed', 'promoted'):
                cursor.execute("""
                    UPDATE nudges
                    SET status = 'resolved'
                    WHERE source_type = 'detected_action'
                      AND source_id = %s
                      AND status NOT IN ('resolved', 'failed')
                """, (action_id,))
            return updated
    except Exception as e:
        _classification_logger.error("Failed to update detected action status: %s", repr(e))
        return False


def get_classification_model(user_id: int) -> str:
    """
    Get classification model preference for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Model string (defaults to Haiku)
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT classification_model FROM user_settings WHERE user_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        if row and row['classification_model']:
            return row['classification_model']
    return 'claude-haiku-4-5-20251001'


def set_classification_model(user_id: int, model: str) -> bool:
    """
    Set classification model preference for a user.

    Args:
        user_id: User's unique identifier
        model: Model string ('claude-haiku-4-5-20251001' or 'claude-sonnet-4-5-20250929')

    Returns:
        True if updated, False on error
    """
    valid_models = {'claude-haiku-4-5-20251001', 'claude-sonnet-4-5-20250929'}
    if model not in valid_models:
        _classification_logger.warning("Invalid classification model: %s", model)
        return False
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE user_settings SET classification_model = %s WHERE user_id = %s",
                (model, user_id)
            )
            return cursor.rowcount > 0
    except Exception as e:
        _classification_logger.error("Failed to set classification model: %s", repr(e))
        return False


def mark_scanned_item_processed(item_id: int, classification: str) -> bool:
    """
    Mark a scanned item as processed with a classification label.

    Args:
        item_id: ID of the scanned item
        classification: Classification label to set

    Returns:
        True if updated, False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scanned_items
                SET processed = 1, classification = %s
                WHERE id = %s
            """, (classification, item_id))
            return cursor.rowcount > 0
    except Exception as e:
        _classification_logger.error("Failed to mark scanned item processed: %s", repr(e))
        return False


def increment_classification_attempts(item_id: int) -> int:
    """
    Increment classification_attempts counter for a scanned item.

    Args:
        item_id: ID of the scanned item

    Returns:
        New attempt count, or -1 on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE scanned_items
                SET classification_attempts = COALESCE(classification_attempts, 0) + 1
                WHERE id = %s
            """, (item_id,))
            cursor.execute(
                "SELECT classification_attempts FROM scanned_items WHERE id = %s",
                (item_id,)
            )
            row = cursor.fetchone()
            return row['classification_attempts'] if row else -1
    except Exception as e:
        _classification_logger.error("Failed to increment classification attempts: %s", repr(e))
        return -1


# ============================================================================
# Nudge Helper Functions
# ============================================================================

import logging as _nudge_logging
_nudge_logger = _nudge_logging.getLogger(__name__)


def create_nudge(
    user_id: int,
    nudge_type: str,
    channel: str,
    title: str,
    body: str = None,
    urgency: str = "normal",
    source_type: str = None,
    source_id: int = None,
    batch_id: str = None
) -> Optional[int]:
    """
    Create a nudge record in the database.

    Args:
        user_id: User's unique identifier
        nudge_type: Type of nudge ('detected_action', 'urgent_item', 'overdue_task', 'batch')
        channel: Delivery channel ('push', 'slack', 'telegram', 'pending_delivery')
        title: Nudge title/headline
        body: Nudge body text
        urgency: 'urgent' or 'normal'
        source_type: Type of source item ('detected_action', 'classification', 'task')
        source_id: ID of source item
        batch_id: Optional batch identifier for grouped nudges

    Returns:
        Nudge ID if created, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO nudges (
                    user_id, nudge_type, channel, title, body, urgency,
                    source_type, source_id, status, batch_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
            """, (user_id, nudge_type, channel, title, body, urgency,
                  source_type, source_id, batch_id))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _nudge_logger.error("Failed to create nudge: %s", repr(e))
        return None


def get_recent_nudges(user_id: int, hours: int = 24, limit: int = 50) -> list[dict]:
    """
    Get recent nudges for a user.

    Args:
        user_id: User's unique identifier
        hours: Number of hours to look back
        limit: Maximum nudges to return

    Returns:
        List of nudge dictionaries ordered by created_at DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cursor.execute("""
                SELECT id, user_id, nudge_type, channel, title, body, urgency,
                       source_type, source_id, status, sent_at, delivered_at,
                       acted_at, batch_id, created_at
                FROM nudges
                WHERE user_id = %s
                  AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, cutoff, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _nudge_logger.error("Failed to get recent nudges: %s", repr(e))
        return []


def get_last_sent_nudge(user_id: int, channel: str, hours: int = 4) -> Optional[dict]:
    """
    Get the most recently sent nudge for a user on a specific channel.

    Used to inject nudge context into bot chat replies — when a user replies
    after receiving a nudge, the chat AI needs to know what was sent.

    Args:
        user_id: User's unique identifier
        channel: Delivery channel ('telegram', 'slack')
        hours: Look back window in hours (default 4)

    Returns:
        Most recent nudge dict or None if no nudge sent recently
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cursor.execute("""
                SELECT id, nudge_type, channel, title, body, urgency,
                       source_type, source_id, sent_at, created_at
                FROM nudges
                WHERE user_id = %s
                  AND channel = %s
                  AND status IN ('sent', 'delivered')
                  AND created_at >= %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (user_id, channel, cutoff))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to get last sent nudge: %s", repr(e))
        return None


def get_nudge_by_telegram_message_id(user_id: int, telegram_message_id: str) -> Optional[dict]:
    """
    Look up a nudge by the Telegram message ID it was delivered as.

    Used by reply threading (Phase 37-03) to identify which nudge a user is replying to.

    Args:
        user_id: User's unique identifier
        telegram_message_id: Telegram message ID (as string)

    Returns:
        Nudge dict or None if not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, nudge_type, channel, title, body, urgency,
                       source_type, source_id, status, sent_at, delivered_at,
                       acted_at, user_response, batch_id, telegram_message_id,
                       slack_message_ts, created_at
                FROM nudges
                WHERE user_id = %s AND telegram_message_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (user_id, telegram_message_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to get nudge by telegram_message_id: %s", repr(e))
        return None


def get_nudge_by_slack_ts(user_id: int, slack_ts: str) -> Optional[dict]:
    """
    Look up a nudge by the Slack message timestamp it was delivered as.

    Used by reply threading (Phase 37-03) to identify which nudge a user is replying to.

    Args:
        user_id: User's unique identifier
        slack_ts: Slack message timestamp string (e.g. "1234567890.123456")

    Returns:
        Nudge dict or None if not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, nudge_type, channel, title, body, urgency,
                       source_type, source_id, status, sent_at, delivered_at,
                       acted_at, user_response, batch_id, telegram_message_id,
                       slack_message_ts, created_at
                FROM nudges
                WHERE user_id = %s AND slack_message_ts = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (user_id, slack_ts))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to get nudge by slack_ts: %s", repr(e))
        return None


def get_nudge_by_id(user_id: int, nudge_id: int) -> Optional[dict]:
    """Get a specific nudge by ID, scoped to the user."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nudge_type, channel, title, body, urgency,
                       source_type, source_id, status, sent_at, delivered_at,
                       acted_at, user_response, batch_id, created_at
                FROM nudges
                WHERE id = %s AND user_id = %s
            """, (nudge_id, user_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to get nudge by id: %s", repr(e))
        return None


def get_nudge_for_source(user_id: int, source_type: str, source_id: int) -> Optional[dict]:
    """
    Check if a nudge already exists for a specific source item.

    Used for deduplication - prevents sending multiple nudges for the same item.

    Args:
        user_id: User's unique identifier
        source_type: Type of source item
        source_id: ID of source item

    Returns:
        Nudge dict if exists, None otherwise
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, nudge_type, channel, title, body, urgency,
                       source_type, source_id, status, sent_at, delivered_at,
                       acted_at, batch_id, created_at
                FROM nudges
                WHERE user_id = %s AND source_type = %s AND source_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            """, (user_id, source_type, source_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to get nudge for source: %s", repr(e))
        return None


def update_nudge_status(
    nudge_id: int,
    status: str,
    sent_at: str = None,
    delivered_at: str = None,
    acted_at: str = None
) -> bool:
    """
    Update nudge status and timestamps.

    Args:
        nudge_id: Nudge ID to update
        status: New status ('pending', 'sent', 'delivered', 'acted', 'failed')
        sent_at: ISO timestamp when sent
        delivered_at: ISO timestamp when confirmed delivered
        acted_at: ISO timestamp when user acted on it

    Returns:
        True if updated, False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Build dynamic update based on provided fields
            updates = ["status = %s"]
            params = [status]

            if sent_at:
                updates.append("sent_at = %s")
                params.append(sent_at)
            if delivered_at:
                updates.append("delivered_at = %s")
                params.append(delivered_at)
            if acted_at:
                updates.append("acted_at = %s")
                params.append(acted_at)

            params.append(nudge_id)

            cursor.execute(f"""
                UPDATE nudges
                SET {', '.join(updates)}
                WHERE id = %s
            """, params)
            return cursor.rowcount > 0
    except Exception as e:
        _nudge_logger.error("Failed to update nudge status: %s", repr(e))
        return False


def get_nudge_preferences(user_id: int) -> dict:
    """
    Get nudge preferences for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with nudge_enabled, nudge_quiet_start, nudge_quiet_end,
        nudge_max_urgent_per_hour, nudge_batch_interval_minutes,
        nudge_channels, nudge_batch_channel, nudge_last_batch_at, digest_timezone,
        nudge_drip_interval_minutes, and nudge_last_drip_at
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nudge_enabled, nudge_quiet_start, nudge_quiet_end,
                   nudge_max_urgent_per_hour, nudge_batch_interval_minutes,
                   nudge_channels, nudge_batch_channel, nudge_last_batch_at, digest_timezone,
                   nudge_drip_interval_minutes, nudge_last_drip_at,
                   pending_action_notification_channel, nudge_quiet_skip_weekend,
                   nudge_smart_dedup
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()

        if row is None:
            # Return defaults if user has no settings yet
            return {
                'nudge_enabled': True,
                'nudge_quiet_start': '22:00',
                'nudge_quiet_end': '08:00',
                'nudge_max_urgent_per_hour': 3,
                'nudge_batch_interval_minutes': 60,
                'nudge_channels': '["push"]',
                'nudge_batch_channel': 'push',
                'nudge_last_batch_at': None,
                'digest_timezone': 'America/Chicago',
                'nudge_drip_interval_minutes': 15,
                'nudge_last_drip_at': None,
                'pending_action_notification_channel': 'none',
                'nudge_quiet_skip_weekend': False,
                'nudge_smart_dedup': True,
            }

        return {
            'nudge_enabled': bool(row['nudge_enabled']) if row['nudge_enabled'] is not None else True,
            'nudge_quiet_start': row['nudge_quiet_start'] or '22:00',
            'nudge_quiet_end': row['nudge_quiet_end'] or '08:00',
            'nudge_max_urgent_per_hour': row['nudge_max_urgent_per_hour'] or 3,
            'nudge_batch_interval_minutes': row['nudge_batch_interval_minutes'] or 60,
            'nudge_channels': row['nudge_channels'] or '["push"]',
            'nudge_batch_channel': row['nudge_batch_channel'] or 'push',
            'nudge_last_batch_at': row['nudge_last_batch_at'],
            'digest_timezone': row['digest_timezone'] or 'America/Chicago',
            'nudge_drip_interval_minutes': row['nudge_drip_interval_minutes'] or 15,
            'nudge_last_drip_at': row['nudge_last_drip_at'],
            'pending_action_notification_channel': row['pending_action_notification_channel'] or 'none',
            'nudge_quiet_skip_weekend': bool(row['nudge_quiet_skip_weekend']) if row['nudge_quiet_skip_weekend'] is not None else False,
            'nudge_smart_dedup': bool(row['nudge_smart_dedup']) if row.get('nudge_smart_dedup') is not None else True,
        }


def update_nudge_preferences(
    user_id: int,
    nudge_enabled: bool = None,
    nudge_quiet_start: str = None,
    nudge_quiet_end: str = None,
    nudge_max_urgent_per_hour: int = None,
    nudge_batch_interval_minutes: int = None,
    nudge_channels: str = None,
    nudge_batch_channel: str = None,
    nudge_last_batch_at: str = None,
    nudge_drip_interval_minutes: int = None,
    nudge_last_drip_at: str = None,
    pending_action_notification_channel: str = None,
    nudge_quiet_skip_weekend: int = None,
    nudge_smart_dedup: int = None,
) -> bool:
    """
    Update nudge preferences for a user.

    Args:
        user_id: User's unique identifier
        nudge_enabled: Master toggle for nudges
        nudge_quiet_start: Start of quiet hours (HH:MM)
        nudge_quiet_end: End of quiet hours (HH:MM)
        nudge_max_urgent_per_hour: Rate limit for urgent nudges
        nudge_batch_interval_minutes: Batch interval in minutes
        nudge_channels: JSON array of enabled channels
        nudge_batch_channel: Channel for batch nudges (push, telegram, slack, email)
        nudge_last_batch_at: Timestamp of last batch send
        nudge_drip_interval_minutes: Drip interval in minutes (Phase 37-02)
        nudge_last_drip_at: Timestamp of last drip send (Phase 37-02)

    Returns:
        True if updated successfully
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # First ensure user_settings row exists
            cursor.execute("""
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT DO NOTHING
            """, (user_id,))

            # Build dynamic update
            updates = []
            params = []

            if nudge_enabled is not None:
                updates.append("nudge_enabled = %s")
                params.append(1 if nudge_enabled else 0)
            if nudge_quiet_start is not None:
                updates.append("nudge_quiet_start = %s")
                params.append(nudge_quiet_start)
            if nudge_quiet_end is not None:
                updates.append("nudge_quiet_end = %s")
                params.append(nudge_quiet_end)
            if nudge_max_urgent_per_hour is not None:
                updates.append("nudge_max_urgent_per_hour = %s")
                params.append(nudge_max_urgent_per_hour)
            if nudge_batch_interval_minutes is not None:
                updates.append("nudge_batch_interval_minutes = %s")
                params.append(nudge_batch_interval_minutes)
            if nudge_channels is not None:
                updates.append("nudge_channels = %s")
                params.append(nudge_channels)
            if nudge_batch_channel is not None:
                updates.append("nudge_batch_channel = %s")
                params.append(nudge_batch_channel)
            if nudge_last_batch_at is not None:
                updates.append("nudge_last_batch_at = %s")
                params.append(nudge_last_batch_at)
            if nudge_drip_interval_minutes is not None:
                updates.append("nudge_drip_interval_minutes = %s")
                params.append(nudge_drip_interval_minutes)
            if nudge_last_drip_at is not None:
                updates.append("nudge_last_drip_at = %s")
                params.append(nudge_last_drip_at)
            if pending_action_notification_channel is not None:
                updates.append("pending_action_notification_channel = %s")
                params.append(pending_action_notification_channel)
            if nudge_quiet_skip_weekend is not None:
                updates.append("nudge_quiet_skip_weekend = %s")
                params.append(nudge_quiet_skip_weekend)
            if nudge_smart_dedup is not None:
                updates.append("nudge_smart_dedup = %s")
                params.append(nudge_smart_dedup)

            if not updates:
                return True  # Nothing to update

            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(user_id)

            cursor.execute(f"""
                UPDATE user_settings
                SET {', '.join(updates)}
                WHERE user_id = %s
            """, params)
            return True
    except Exception as e:
        _nudge_logger.error("Failed to update nudge preferences: %s", repr(e))
        return False


def get_last_batch_time(user_id: int) -> Optional[str]:
    """
    Get the timestamp of the last batch nudge send for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        ISO timestamp string or None if never sent
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nudge_last_batch_at
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()
        return row['nudge_last_batch_at'] if row else None


def get_next_drip_nudge(user_id: int) -> Optional[dict]:
    """
    Get the single highest-priority pending nudge to drip next.

    Priority order:
    1. urgency = 'urgent' first
    2. Then oldest by created_at (FIFO within same urgency tier)
    3. Only status = 'pending', batch_id IS NULL (not already batched)

    Phase 37-02: Conversational Nudge Flow.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with nudge fields or None if no pending nudges
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT n.id, n.nudge_type, n.title, n.body, n.urgency,
                       n.source_type, n.source_id, n.created_at,
                       n.closure_delay_count, n.closure_hold_until,
                       da.deadline, cen.event_start
                FROM nudges n
                LEFT JOIN detected_actions da
                    ON n.source_type = 'detected_action' AND n.source_id = da.id
                LEFT JOIN calendar_event_nudges cen
                    ON cen.nudge_id = n.id
                WHERE n.user_id = %s
                  AND n.status = 'pending'
                  AND n.batch_id IS NULL
                  AND (n.closure_hold_until IS NULL OR n.closure_hold_until <= NOW())
                  AND NOT (
                      n.nudge_type = 'meeting_prep'
                      AND n.created_at < NOW() - INTERVAL '90 minutes'
                  )
                  AND NOT (
                      n.nudge_type = 'calendar_event'
                      AND n.created_at < NOW() - INTERVAL '5 hours'
                  )
                ORDER BY
                    CASE WHEN COALESCE(da.deadline, cen.event_start) IS NOT NULL
                              AND COALESCE(da.deadline::timestamptz, cen.event_start::timestamptz) > NOW()
                         THEN 0 ELSE 1 END ASC,
                    COALESCE(da.deadline::timestamptz, cen.event_start::timestamptz) ASC NULLS LAST,
                    CASE WHEN n.urgency = 'urgent' THEN 0 ELSE 1 END ASC,
                    n.created_at ASC
                LIMIT 1
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _nudge_logger.error("Failed to get next drip nudge: %s", repr(e))
        return None


# ============================================================================
# Scanner Preferences
# ============================================================================

_scanner_prefs_logging = __import__('logging')
_scanner_prefs_logger = _scanner_prefs_logging.getLogger(__name__)


def get_scanner_preferences(user_id: int) -> dict:
    """
    Get scanner preferences for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with scanner interval settings and classification tier
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT scanner_gmail_interval_minutes,
                   scanner_slack_interval_minutes,
                   scanner_telegram_interval_minutes,
                   scanner_calendar_interval_minutes,
                   classification_tier
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()

        if row:
            return {
                'scanner_gmail_interval_minutes': row['scanner_gmail_interval_minutes'] or 15,
                'scanner_slack_interval_minutes': row['scanner_slack_interval_minutes'] or 120,
                'scanner_telegram_interval_minutes': row['scanner_telegram_interval_minutes'] or 5,
                'scanner_calendar_interval_minutes': row['scanner_calendar_interval_minutes'] or 60,
                'classification_tier': row['classification_tier'] or 'haiku',
            }

        # Default values if no user_settings row
        return {
            'scanner_gmail_interval_minutes': 15,
            'scanner_slack_interval_minutes': 120,
            'scanner_telegram_interval_minutes': 5,
            'scanner_calendar_interval_minutes': 60,
            'classification_tier': 'haiku',
        }


def update_scanner_preferences(
    user_id: int,
    scanner_gmail_interval_minutes: Optional[int] = None,
    scanner_slack_interval_minutes: Optional[int] = None,
    scanner_telegram_interval_minutes: Optional[int] = None,
    scanner_calendar_interval_minutes: Optional[int] = None,
    classification_tier: Optional[str] = None
) -> bool:
    """
    Update scanner preferences for a user.

    Args:
        user_id: User's unique identifier
        scanner_gmail_interval_minutes: Gmail scan interval (5-1440 minutes)
        scanner_slack_interval_minutes: Slack scan interval (5-1440 minutes)
        scanner_telegram_interval_minutes: Telegram scan interval (5-1440 minutes)
        scanner_calendar_interval_minutes: Calendar scan interval (5-1440 minutes)
        classification_tier: 'haiku' or 'full'

    Returns:
        True if updated successfully
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Ensure user_settings row exists
            cursor.execute("""
                INSERT INTO user_settings (user_id)
                VALUES (%s)
                ON CONFLICT DO NOTHING
            """, (user_id,))

            # Build dynamic update
            updates = []
            params = []

            if scanner_gmail_interval_minutes is not None:
                updates.append("scanner_gmail_interval_minutes = %s")
                params.append(scanner_gmail_interval_minutes)

            if scanner_slack_interval_minutes is not None:
                updates.append("scanner_slack_interval_minutes = %s")
                params.append(scanner_slack_interval_minutes)

            if scanner_telegram_interval_minutes is not None:
                updates.append("scanner_telegram_interval_minutes = %s")
                params.append(scanner_telegram_interval_minutes)

            if scanner_calendar_interval_minutes is not None:
                updates.append("scanner_calendar_interval_minutes = %s")
                params.append(scanner_calendar_interval_minutes)

            if classification_tier is not None:
                updates.append("classification_tier = %s")
                params.append(classification_tier)

            if not updates:
                return True  # Nothing to update

            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(user_id)

            cursor.execute(f"""
                UPDATE user_settings
                SET {', '.join(updates)}
                WHERE user_id = %s
            """, params)
            return True
    except Exception as e:
        _scanner_prefs_logger.error("Failed to update scanner preferences: %s", repr(e))
        return False


def get_scanner_interval_for_source(user_id: int, source: str) -> int:
    """
    Get the scan interval for a specific source for a user.

    Args:
        user_id: User's unique identifier
        source: Source name ('gmail', 'slack', 'telegram', 'calendar')

    Returns:
        Interval in minutes (falls back to default if not set)
    """
    # Map source to column name and default
    source_defaults = {
        'gmail': ('scanner_gmail_interval_minutes', 15),
        'slack': ('scanner_slack_interval_minutes', 120),
        'telegram': ('scanner_telegram_interval_minutes', 5),
        'calendar': ('scanner_calendar_interval_minutes', 60),
    }

    if source not in source_defaults:
        # Return default from SOURCE_CONFIGS for other sources
        return None

    column_name, default_interval = source_defaults[source]

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT {column_name}
            FROM user_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()
        if row and row[column_name]:
            return row[column_name]

        return default_interval


# ============================================================================
# User Feedback and Pattern Learning
# ============================================================================

_feedback_logging = __import__('logging')
_feedback_logger = _feedback_logging.getLogger(__name__)


def record_feedback(
    user_id: int,
    item_type: str,
    item_id: Optional[int] = None,
    feedback_type: str = None,
    feedback_context: Optional[str] = None,
    reason: Optional[str] = None,
    item_context: Optional[str] = None
) -> Optional[int]:
    """
    Record user feedback on an intelligence item.

    Args:
        user_id: User's unique identifier
        item_type: Type of item ('nudge', 'detected_action', 'needs_reply',
                   'unfulfilled_commitment', 'cross_source_connection', 'open_loop')
        item_id: Optional ID of the specific item
        feedback_type: Type of feedback ('helpful', 'not_helpful', 'too_much', 'snooze',
                       'accurate', 'inaccurate', 'more_like_this', 'less_like_this')
        feedback_context: Optional JSON string with additional context (e.g., snooze duration)
        reason: Optional text explaining WHY this feedback was given (Phase 26)
        item_context: Optional short quote of the item text being rated (Phase 26)

    Returns:
        Feedback ID if created, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_feedback (
                    user_id, item_type, item_id, feedback_type, feedback_context,
                    reason, item_context
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, item_type, item_id, feedback_type, feedback_context,
                  reason, item_context))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _feedback_logger.error("Failed to record feedback: %s", repr(e))
        return None


def get_recent_feedback(
    user_id: int,
    days: int = 30,
    item_type: Optional[str] = None
) -> list[dict]:
    """
    Get recent feedback for a user.

    Args:
        user_id: User's unique identifier
        days: Number of days to look back (default 30)
        item_type: Optional filter by item type

    Returns:
        List of feedback dictionaries ordered by created_at DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            if item_type:
                cursor.execute("""
                    SELECT id, user_id, item_type, item_id, feedback_type,
                           feedback_context, reason, item_context, created_at
                    FROM user_feedback
                    WHERE user_id = %s
                      AND item_type = %s
                      AND created_at >= %s
                    ORDER BY created_at DESC
                """, (user_id, item_type, cutoff))
            else:
                cursor.execute("""
                    SELECT id, user_id, item_type, item_id, feedback_type,
                           feedback_context, reason, item_context, created_at
                    FROM user_feedback
                    WHERE user_id = %s
                      AND created_at >= %s
                    ORDER BY created_at DESC
                """, (user_id, cutoff))

            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _feedback_logger.error("Failed to get recent feedback: %s", repr(e))
        return []


def get_feedback_stats(user_id: int) -> dict:
    """
    Get aggregated feedback statistics for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with counts by item_type and feedback_type:
        {
            'by_item_type': {'nudge': 5, 'detected_action': 3, ...},
            'by_feedback_type': {'helpful': 4, 'not_helpful': 2, ...},
            'total': 8
        }
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Get counts by item_type
            cursor.execute("""
                SELECT item_type, COUNT(*) as count
                FROM user_feedback
                WHERE user_id = %s
                GROUP BY item_type
            """, (user_id,))
            by_item_type = {row['item_type']: row['count'] for row in cursor.fetchall()}

            # Get counts by feedback_type
            cursor.execute("""
                SELECT feedback_type, COUNT(*) as count
                FROM user_feedback
                WHERE user_id = %s
                GROUP BY feedback_type
            """, (user_id,))
            by_feedback_type = {row['feedback_type']: row['count'] for row in cursor.fetchall()}

            # Get total count
            cursor.execute("""
                SELECT COUNT(*) as total
                FROM user_feedback
                WHERE user_id = %s
            """, (user_id,))
            total = cursor.fetchone()['total']

            return {
                'by_item_type': by_item_type,
                'by_feedback_type': by_feedback_type,
                'total': total
            }
    except Exception as e:
        _feedback_logger.error("Failed to get feedback stats: %s", repr(e))
        return {'by_item_type': {}, 'by_feedback_type': {}, 'total': 0}


def get_pattern_preferences(user_id: int) -> Optional[dict]:
    """
    Get computed pattern preferences for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with responsive_hours, preferred_channels_by_time,
        item_type_preferences, and last_computed_at; or None if not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, responsive_hours, preferred_channels_by_time,
                       item_type_preferences, lessons_learned, last_computed_at,
                       created_at, updated_at
                FROM user_pattern_preferences
                WHERE user_id = %s
            """, (user_id,))

            row = cursor.fetchone()
            if row:
                return dict(row)
            return None
    except Exception as e:
        _feedback_logger.error("Failed to get pattern preferences: %s", repr(e))
        return None


def update_pattern_preferences(
    user_id: int,
    responsive_hours: Optional[str] = None,
    preferred_channels_by_time: Optional[str] = None,
    item_type_preferences: Optional[str] = None,
    lessons_learned: Optional[str] = None,
) -> bool:
    """
    Update or create pattern preferences for a user.

    Args:
        user_id: User's unique identifier
        responsive_hours: JSON array of hours when user typically responds
        preferred_channels_by_time: JSON object mapping time ranges to preferred channels
        item_type_preferences: JSON object with preference scores by item type
        lessons_learned: JSON object with aggregated reasons grouped by feedback_type (Phase 26-02)

    Returns:
        True if updated/created successfully
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Check if record exists
            cursor.execute("""
                SELECT id FROM user_pattern_preferences WHERE user_id = %s
            """, (user_id,))

            existing = cursor.fetchone()

            if existing:
                # Build dynamic update
                updates = []
                params = []

                if responsive_hours is not None:
                    updates.append("responsive_hours = %s")
                    params.append(responsive_hours)
                if preferred_channels_by_time is not None:
                    updates.append("preferred_channels_by_time = %s")
                    params.append(preferred_channels_by_time)
                if item_type_preferences is not None:
                    updates.append("item_type_preferences = %s")
                    params.append(item_type_preferences)
                if lessons_learned is not None:
                    updates.append("lessons_learned = %s")
                    params.append(lessons_learned)

                if not updates:
                    return True  # Nothing to update

                updates.append("last_computed_at = CURRENT_TIMESTAMP")
                updates.append("updated_at = CURRENT_TIMESTAMP")
                params.append(user_id)

                cursor.execute(f"""
                    UPDATE user_pattern_preferences
                    SET {', '.join(updates)}
                    WHERE user_id = %s
                """, params)
            else:
                # Insert new record
                cursor.execute("""
                    INSERT INTO user_pattern_preferences (
                        user_id, responsive_hours, preferred_channels_by_time,
                        item_type_preferences, lessons_learned, last_computed_at
                    ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                """, (user_id, responsive_hours, preferred_channels_by_time,
                      item_type_preferences, lessons_learned))

            return True
    except Exception as e:
        _feedback_logger.error("Failed to update pattern preferences: %s", repr(e))
        return False


def get_suppression_overrides(user_id: int) -> dict:
    """
    Get the suppression override map for a user.

    Override entries set to True mean "do NOT suppress this item_type, regardless of
    computed preference score". The compute cycle never touches this column so overrides
    survive indefinitely.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict mapping item_type -> bool. Returns {} if no row or column is NULL.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT suppression_overrides
                FROM user_pattern_preferences
                WHERE user_id = %s
            """, (user_id,))
            row = cursor.fetchone()
            if not row:
                return {}
            value = row['suppression_overrides'] if isinstance(row, dict) else row[0]
            if value is None:
                return {}
            if isinstance(value, dict):
                return value
            return json.loads(value)
    except Exception as e:
        _feedback_logger.error("Failed to get suppression overrides for user %d: %s", user_id, repr(e))
        return {}


def reset_suppression_override(user_id: int, item_type: str) -> bool:
    """
    Set an override that permanently prevents suppression for a given item_type.

    Sets overrides[item_type] = True, meaning "do NOT suppress this type".
    Handles the case where no user_pattern_preferences row exists yet by using
    INSERT ... ON CONFLICT DO UPDATE.

    Args:
        user_id: User's unique identifier
        item_type: The nudge/item type to un-suppress (e.g. 'needs_reply')

    Returns:
        True on success, False on failure
    """
    try:
        current = get_suppression_overrides(user_id)
        current[item_type] = True
        overrides_json = json.dumps(current)

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_pattern_preferences (user_id, suppression_overrides)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (user_id) DO UPDATE
                    SET suppression_overrides = EXCLUDED.suppression_overrides,
                        updated_at = CURRENT_TIMESTAMP
            """, (user_id, overrides_json))
            conn.commit()
        return True
    except Exception as e:
        _feedback_logger.error(
            "Failed to reset suppression override for user %d item_type %s: %s",
            user_id, item_type, repr(e)
        )
        return False


def record_nudge_response(nudge_id: int, user_response: str) -> bool:
    """
    Record user's response to a nudge.

    Args:
        nudge_id: Nudge ID to update
        user_response: Response type ('helpful', 'dismissed', 'snoozed')

    Returns:
        True if updated, False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE nudges
                SET user_response = %s, acted_at = NOW()
                WHERE id = %s
            """, (user_response, nudge_id))
            return cursor.rowcount > 0
    except Exception as e:
        _feedback_logger.error("Failed to record nudge response: %s", repr(e))
        return False


def requeue_nudge(nudge_id: int, delay_hours: float = 4.0) -> Optional[int]:
    """
    Re-queue a nudge to resurface after delay_hours.
    Creates a new pending nudge record copied from the original.
    Used when user replies 'not yet' to a check-in.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # Fetch original nudge
            cursor.execute(
                "SELECT user_id, nudge_type, channel, title, body, urgency, source_type, source_id "
                "FROM nudges WHERE id = %s",
                (nudge_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None

            # Calculate future created_at so it surfaces after delay
            from datetime import datetime, timedelta
            future_at = (datetime.now() + timedelta(hours=delay_hours)).isoformat()

            cursor.execute("""
                INSERT INTO nudges (user_id, nudge_type, channel, title, body, urgency,
                                    source_type, source_id, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
            """, (
                row['user_id'], row['nudge_type'], row['channel'],
                row['title'], row['body'], row['urgency'],
                row['source_type'], row['source_id'], future_at
            ))
            row = cursor.fetchone()
            new_id = row['id'] if row else None
            _nudge_logger.info("Re-queued nudge %d as new nudge %d (delay=%.1fh)", nudge_id, new_id, delay_hours)
            return new_id
    except Exception as e:
        _nudge_logger.error("Failed to requeue nudge %d: %s", nudge_id, repr(e))
        return None


# ============================================================================
# Email Digest Feedback Functions
# ============================================================================

_email_feedback_logger = logging.getLogger(__name__ + '.email_feedback')

# Secret key for HMAC token generation - use env var or generate per-session
_EMAIL_FEEDBACK_SECRET = os.environ.get('EMAIL_FEEDBACK_SECRET', secrets.token_hex(32))


def create_email_feedback_token(
    user_id: int,
    item_type: str,
    feedback_action: str,
    item_id: Optional[int] = None,
    scanned_item_id: Optional[int] = None,
    sender_identifier: Optional[str] = None,
    source_type: Optional[str] = None,
    expires_days: int = 7
) -> Optional[str]:
    """
    Create a secure feedback token for email links.

    Args:
        user_id: User's unique identifier
        item_type: Type of item ('needs_reply', 'detected_action', etc.)
        feedback_action: Action type ('not_helpful', 'ignore_sender')
        item_id: Optional ID of the specific item
        scanned_item_id: Optional scanned_item ID
        sender_identifier: Optional sender email/identifier for ignore_sender
        source_type: Optional source type (gmail, slack, telegram)
        expires_days: Days until token expires (default 7)

    Returns:
        Token string if created, None on error
    """
    try:
        # Generate unique token using HMAC-SHA256
        timestamp = datetime.now().isoformat()
        token_data = f"{user_id}:{item_type}:{item_id}:{scanned_item_id}:{feedback_action}:{timestamp}"
        token = hmac.new(
            _EMAIL_FEEDBACK_SECRET.encode(),
            token_data.encode(),
            hashlib.sha256
        ).hexdigest()

        expires_at = (datetime.now() + timedelta(days=expires_days)).isoformat()

        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO email_feedback_tokens (
                    user_id, token, item_type, item_id, scanned_item_id,
                    sender_identifier, source_type, feedback_action, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                user_id, token, item_type, item_id, scanned_item_id,
                sender_identifier, source_type, feedback_action, expires_at
            ))
            return token

    except Exception as e:
        _email_feedback_logger.error("Failed to create email feedback token: %s", repr(e))
        return None


def validate_and_consume_email_feedback_token(token: str) -> Optional[dict]:
    """
    Validate an email feedback token and mark it as used.

    Args:
        token: The token string to validate

    Returns:
        Token data dict if valid (user_id, item_type, item_id, feedback_action, etc.),
        None if invalid, expired, or already used
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Find the token
            cursor.execute("""
                SELECT id, user_id, token, item_type, item_id, scanned_item_id,
                       sender_identifier, source_type, feedback_action,
                       created_at, expires_at, used_at
                FROM email_feedback_tokens
                WHERE token = %s
            """, (token,))

            row = cursor.fetchone()

            if not row:
                _email_feedback_logger.warning("Token not found: %s...", token[:8])
                return None

            token_data = dict(row)

            # Check if already used
            if token_data['used_at']:
                _email_feedback_logger.warning("Token already used: %s...", token[:8])
                return None

            # Check if expired
            expires_at = datetime.fromisoformat(token_data['expires_at'])
            if datetime.now() > expires_at:
                _email_feedback_logger.warning("Token expired: %s...", token[:8])
                return None

            # Mark as used
            cursor.execute("""
                UPDATE email_feedback_tokens
                SET used_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (token_data['id'],))

            return token_data

    except Exception as e:
        _email_feedback_logger.error("Failed to validate email feedback token: %s", repr(e))
        return None


def peek_email_feedback_token(token: str) -> Optional[dict]:
    """
    Validate an email feedback token without consuming it (read-only).

    Identical to validate_and_consume_email_feedback_token except it does NOT
    run the UPDATE used_at statement. Used to inspect the token before deciding
    whether to show a form (not_helpful) or process it immediately.

    Args:
        token: The token string to validate

    Returns:
        Token data dict if valid and unused (user_id, item_type, item_id, feedback_action, etc.),
        None if invalid, expired, or already used
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Find the token
            cursor.execute("""
                SELECT id, user_id, token, item_type, item_id, scanned_item_id,
                       sender_identifier, source_type, feedback_action,
                       created_at, expires_at, used_at
                FROM email_feedback_tokens
                WHERE token = %s
            """, (token,))

            row = cursor.fetchone()

            if not row:
                _email_feedback_logger.warning("peek: Token not found: %s...", token[:8])
                return None

            token_data = dict(row)

            # Check if already used
            if token_data['used_at']:
                _email_feedback_logger.warning("peek: Token already used: %s...", token[:8])
                return None

            # Check if expired
            expires_at = datetime.fromisoformat(token_data['expires_at'])
            if datetime.now() > expires_at:
                _email_feedback_logger.warning("peek: Token expired: %s...", token[:8])
                return None

            # No UPDATE — read-only peek
            return token_data

    except Exception as e:
        _email_feedback_logger.error("Failed to peek email feedback token: %s", repr(e))
        return None


def add_ignored_sender(
    user_id: int,
    source_type: str,
    sender_identifier: str
) -> bool:
    """
    Add a sender to the user's ignore list.

    Args:
        user_id: User's unique identifier
        source_type: Source type (gmail, slack, telegram)
        sender_identifier: Sender email/ID to ignore

    Returns:
        True if added (or already exists), False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO ignored_senders (
                    user_id, source_type, sender_identifier
                ) VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (user_id, source_type, sender_identifier))

            if cursor.rowcount > 0:
                _email_feedback_logger.info(
                    "Added ignored sender for user %d: %s/%s",
                    user_id, source_type, sender_identifier
                )
            return True

    except Exception as e:
        _email_feedback_logger.error("Failed to add ignored sender: %s", repr(e))
        return False


def get_ignored_senders(user_id: int, source_type: Optional[str] = None) -> list[dict]:
    """
    Get list of ignored senders for a user.

    Args:
        user_id: User's unique identifier
        source_type: Optional filter by source type

    Returns:
        List of dicts with source_type, sender_identifier, ignored_at
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()

            if source_type:
                cursor.execute("""
                    SELECT id, source_type, sender_identifier, ignored_at
                    FROM ignored_senders
                    WHERE user_id = %s AND source_type = %s
                    ORDER BY ignored_at DESC
                """, (user_id, source_type))
            else:
                cursor.execute("""
                    SELECT id, source_type, sender_identifier, ignored_at
                    FROM ignored_senders
                    WHERE user_id = %s
                    ORDER BY ignored_at DESC
                """, (user_id,))

            return [dict(row) for row in cursor.fetchall()]

    except Exception as e:
        _email_feedback_logger.error("Failed to get ignored senders: %s", repr(e))
        return []


def is_sender_ignored(user_id: int, source_type: str, sender_identifier: str) -> bool:
    """
    Check if a sender is in the user's ignore list.

    Args:
        user_id: User's unique identifier
        source_type: Source type (gmail, slack, telegram)
        sender_identifier: Sender email/ID to check

    Returns:
        True if sender is ignored, False otherwise
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM ignored_senders
                WHERE user_id = %s AND source_type = %s AND sender_identifier = %s
                LIMIT 1
            """, (user_id, source_type, sender_identifier))

            return cursor.fetchone() is not None

    except Exception as e:
        _email_feedback_logger.error("Failed to check ignored sender: %s", repr(e))
        return False


def remove_ignored_sender(user_id: int, source_type: str, sender_identifier: str) -> bool:
    """
    Remove a sender from the user's ignore list.

    Args:
        user_id: User's unique identifier
        source_type: Source type (gmail, slack, telegram)
        sender_identifier: Sender email/ID to remove

    Returns:
        True if removed, False on error or not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM ignored_senders
                WHERE user_id = %s AND source_type = %s AND sender_identifier = %s
            """, (user_id, source_type, sender_identifier))

            return cursor.rowcount > 0

    except Exception as e:
        _email_feedback_logger.error("Failed to remove ignored sender: %s", repr(e))
        return False


# ============================================================================
# Nudge-Suppressed Senders Functions
# ============================================================================
# These functions manage sender-level nudge suppression.  Unlike ignored_senders
# (which prevents scanning entirely), nudge suppression only prevents nudge
# creation while still scanning the sender's messages for context and digest.


def is_sender_nudge_suppressed(
    user_id: int,
    source_type: str,
    sender_identifier: str,
) -> bool:
    """
    Check if a sender is nudge-suppressed for a user.

    Args:
        user_id: User's unique identifier
        source_type: Source type (gmail, slack, telegram)
        sender_identifier: Sender email/ID to check

    Returns:
        True if sender is nudge-suppressed, False otherwise (including on error)
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM nudge_suppressed_senders
                WHERE user_id = %s AND source_type = %s AND sender_identifier = %s
                LIMIT 1
            """, (user_id, source_type, sender_identifier))
            return cursor.fetchone() is not None
    except Exception as e:
        logger.error("Failed to check nudge-suppressed sender: %s", repr(e))
        return False  # Fail open — never block nudge delivery on error


def add_nudge_suppressed_sender(
    user_id: int,
    source_type: str,
    sender_identifier: str,
    reason: Optional[str] = None,
) -> bool:
    """
    Add a sender to the user's nudge suppression list.

    Args:
        user_id: User's unique identifier
        source_type: Source type (gmail, slack, telegram)
        sender_identifier: Sender email/ID to suppress
        reason: Optional human-readable reason (e.g. "3 dismissed nudges in 30d")

    Returns:
        True if added (or already exists), False on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO nudge_suppressed_senders (
                    user_id, source_type, sender_identifier, reason
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (user_id, source_type, sender_identifier, reason))

            if cursor.rowcount > 0:
                logger.info(
                    "Added nudge-suppressed sender for user %d: %s/%s (reason: %s)",
                    user_id, source_type, sender_identifier, reason
                )
            return True
    except Exception as e:
        logger.error("Failed to add nudge-suppressed sender: %s", repr(e))
        return False


def get_nudge_suppressed_senders(
    user_id: int,
    source_type: Optional[str] = None,
) -> list[dict]:
    """
    Get list of nudge-suppressed senders for a user.

    Args:
        user_id: User's unique identifier
        source_type: Optional filter by source type

    Returns:
        List of dicts with source_type, sender_identifier, reason, suppressed_at
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if source_type:
                cursor.execute("""
                    SELECT id, source_type, sender_identifier, reason, suppressed_at
                    FROM nudge_suppressed_senders
                    WHERE user_id = %s AND source_type = %s
                    ORDER BY suppressed_at DESC
                """, (user_id, source_type))
            else:
                cursor.execute("""
                    SELECT id, source_type, sender_identifier, reason, suppressed_at
                    FROM nudge_suppressed_senders
                    WHERE user_id = %s
                    ORDER BY suppressed_at DESC
                """, (user_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error("Failed to get nudge-suppressed senders: %s", repr(e))
        return []


def remove_nudge_suppressed_sender(
    user_id: int,
    source_type: str,
    sender_identifier: str,
) -> bool:
    """
    Remove a sender from the user's nudge suppression list.

    Args:
        user_id: User's unique identifier
        source_type: Source type (gmail, slack, telegram)
        sender_identifier: Sender email/ID to un-suppress

    Returns:
        True if removed, False on error or not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM nudge_suppressed_senders
                WHERE user_id = %s AND source_type = %s AND sender_identifier = %s
            """, (user_id, source_type, sender_identifier))
            return cursor.rowcount > 0
    except Exception as e:
        logger.error("Failed to remove nudge-suppressed sender: %s", repr(e))
        return False


def get_sender_for_detected_action(detected_action_id: int) -> Optional[tuple]:
    """
    Resolve the sender identity for a detected_action by JOINing through
    detected_actions -> scanned_items and parsing source_metadata JSON.

    Sender extraction per source:
        - Gmail:    source_metadata["from"]          -> (gmail, email_address)
        - Slack:    source_metadata["username"]       -> (slack, username)
        - Telegram: source_metadata["sender_name"]   -> (telegram, sender_name)

    Returns:
        (source_type, sender_identifier) tuple, or None if not resolvable.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT si.source, si.source_metadata
                FROM detected_actions da
                JOIN scanned_items si ON da.scanned_item_id = si.id
                WHERE da.id = %s
            """, (detected_action_id,))
            row = cursor.fetchone()
            if not row:
                return None

            row = dict(row) if hasattr(row, 'keys') else {'source': row[0], 'source_metadata': row[1]}
            source = row.get('source', '')
            raw_meta = row.get('source_metadata')
            if not raw_meta:
                return None

            import json as _json
            try:
                meta = _json.loads(raw_meta) if isinstance(raw_meta, str) else raw_meta
            except (ValueError, TypeError):
                return None

            if not isinstance(meta, dict):
                return None

            # Gmail: "from" field contains the sender email
            if source == 'gmail':
                sender = (meta.get('from') or '').strip().lower()
                if sender:
                    return ('gmail', sender)

            # Slack: "username" field (fallback to "user_id")
            elif source == 'slack':
                sender = (meta.get('username') or meta.get('user_id') or '').strip()
                if sender:
                    return ('slack', sender)

            # Telegram: "sender_name" field (fallback to "sender_id")
            elif source == 'telegram':
                sender = (meta.get('sender_name') or '').strip()
                if not sender:
                    sid = meta.get('sender_id')
                    if sid is not None:
                        sender = str(sid)
                if sender:
                    return ('telegram', sender)

            return None

    except Exception as e:
        logger.error(
            "Failed to resolve sender for detected_action %d: %s",
            detected_action_id, repr(e),
        )
        return None


# ============================================================================
# Voice Session Functions
# ============================================================================


def get_active_voice_session(user_id: int, satellite_id: Optional[str] = None) -> Optional[dict]:
    """
    Get active voice session for user/satellite (active within last 5 minutes).

    Args:
        user_id: User's unique identifier
        satellite_id: Optional satellite device identifier

    Returns:
        Session dict with id, conversation_id, satellite_id, last_activity
        None if no active session
    """
    with get_db() as conn:
        cursor = conn.cursor()

        # Session expires after 5 minutes of inactivity
        cutoff = (datetime.now() - timedelta(minutes=5)).isoformat()

        if satellite_id:
            cursor.execute("""
                SELECT id, conversation_id, satellite_id, last_activity
                FROM voice_sessions
                WHERE user_id = %s AND satellite_id = %s AND active = 1
                AND last_activity > %s
                ORDER BY last_activity DESC LIMIT 1
            """, (user_id, satellite_id, cutoff))
        else:
            cursor.execute("""
                SELECT id, conversation_id, satellite_id, last_activity
                FROM voice_sessions
                WHERE user_id = %s AND satellite_id IS NULL AND active = 1
                AND last_activity > %s
                ORDER BY last_activity DESC LIMIT 1
            """, (user_id, cutoff))

        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "conversation_id": row["conversation_id"],
                "satellite_id": row["satellite_id"],
                "last_activity": row["last_activity"]
            }
        return None


def create_voice_session(user_id: int, conversation_id: str, satellite_id: Optional[str] = None) -> int:
    """
    Create a new voice session.

    Args:
        user_id: User's unique identifier
        conversation_id: Associated conversation ID
        satellite_id: Optional satellite device identifier

    Returns:
        Session ID
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO voice_sessions (user_id, conversation_id, satellite_id)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (user_id, conversation_id, satellite_id))
        row = cursor.fetchone()
        return row['id'] if row else None


def update_voice_session_activity(session_id: int) -> None:
    """
    Update last_activity timestamp for a session.

    Args:
        session_id: Voice session ID
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE voice_sessions SET last_activity = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (session_id,))


def end_voice_session(session_id: int) -> None:
    """
    Mark a voice session as inactive.

    Args:
        session_id: Voice session ID
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE voice_sessions SET active = 0
            WHERE id = %s
        """, (session_id,))


# =============================================================================
# Multi-Channel Chat Functions
# =============================================================================

def get_telegram_bot_conversation(
    user_id: int,
    telegram_chat_id: int
) -> Optional[dict]:
    """
    Get conversation for a Telegram bot chat.

    Args:
        user_id: User's unique identifier
        telegram_chat_id: Telegram chat ID

    Returns:
        Dict with conversation info or None
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, telegram_chat_id, conversation_id, created_at, last_activity
            FROM telegram_bot_conversations
            WHERE user_id = %s AND telegram_chat_id = %s
        """, (user_id, telegram_chat_id))

        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "telegram_chat_id": row["telegram_chat_id"],
                "conversation_id": row["conversation_id"],
                "created_at": row["created_at"],
                "last_activity": row["last_activity"]
            }
        return None


def create_telegram_bot_conversation(
    user_id: int,
    telegram_chat_id: int,
    conversation_id: str
) -> int:
    """
    Create a new Telegram bot conversation link.

    Args:
        user_id: User's unique identifier
        telegram_chat_id: Telegram chat ID
        conversation_id: Seny conversation ID

    Returns:
        ID of created row
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO telegram_bot_conversations
            (user_id, telegram_chat_id, conversation_id)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (user_id, telegram_chat_id, conversation_id))
        row = cursor.fetchone()
        return row['id'] if row else None


def update_telegram_bot_conversation_activity(
    user_id: int,
    telegram_chat_id: int
) -> None:
    """
    Update last_activity timestamp for a Telegram bot conversation.

    Args:
        user_id: User's unique identifier
        telegram_chat_id: Telegram chat ID
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE telegram_bot_conversations
            SET last_activity = CURRENT_TIMESTAMP
            WHERE user_id = %s AND telegram_chat_id = %s
        """, (user_id, telegram_chat_id))


def delete_telegram_bot_conversation(user_id: int, telegram_chat_id: int) -> None:
    """
    Delete the conversation link for a Telegram chat.

    Used to clear stale links whose conversation_id no longer exists
    in the conversations table (e.g. after a data migration).

    Args:
        user_id: User's unique identifier
        telegram_chat_id: Telegram chat ID
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM telegram_bot_conversations
            WHERE user_id = %s AND telegram_chat_id = %s
        """, (user_id, telegram_chat_id))


def get_telegram_bot_user_link(telegram_chat_id: int) -> Optional[dict]:
    """
    Get user linked to a Telegram chat ID.

    Args:
        telegram_chat_id: Telegram chat ID

    Returns:
        Dict with user link info or None
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, telegram_chat_id, telegram_username,
                   telegram_first_name, linked_at
            FROM telegram_bot_user_links
            WHERE telegram_chat_id = %s
        """, (telegram_chat_id,))

        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "telegram_chat_id": row["telegram_chat_id"],
                "telegram_username": row["telegram_username"],
                "telegram_first_name": row["telegram_first_name"],
                "linked_at": row["linked_at"]
            }
        return None


def create_telegram_bot_user_link(
    user_id: int,
    telegram_chat_id: int,
    telegram_username: str = None,
    telegram_first_name: str = None
) -> int:
    """
    Create a link between a Telegram chat and a Seny user.

    Args:
        user_id: User's unique identifier
        telegram_chat_id: Telegram chat ID
        telegram_username: Optional Telegram username
        telegram_first_name: Optional Telegram first name

    Returns:
        ID of created row
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO telegram_bot_user_links
            (user_id, telegram_chat_id, telegram_username, telegram_first_name)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (user_id, telegram_chat_id, telegram_username, telegram_first_name))
        row = cursor.fetchone()
        return row['id'] if row else None


def get_telegram_bot_user_links_for_user(user_id: int) -> list[dict]:
    """
    Get all Telegram chats linked to a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of user link dicts
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, telegram_chat_id, telegram_username,
                   telegram_first_name, linked_at
            FROM telegram_bot_user_links
            WHERE user_id = %s
            ORDER BY linked_at DESC
        """, (user_id,))

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "telegram_chat_id": row["telegram_chat_id"],
                "telegram_username": row["telegram_username"],
                "telegram_first_name": row["telegram_first_name"],
                "linked_at": row["linked_at"]
            }
            for row in cursor.fetchall()
        ]


def delete_telegram_bot_user_link(telegram_chat_id: int) -> bool:
    """
    Delete a Telegram user link.

    Args:
        telegram_chat_id: Telegram chat ID

    Returns:
        True if deleted, False if not found
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM telegram_bot_user_links
            WHERE telegram_chat_id = %s
        """, (telegram_chat_id,))
        return cursor.rowcount > 0


# =============================================================================
# Slack Bot Conversation Functions
# =============================================================================

def get_slack_bot_conversation(
    user_id: int,
    slack_channel_id: str
) -> Optional[dict]:
    """
    Get conversation for a Slack bot DM channel.

    Args:
        user_id: User's unique identifier
        slack_channel_id: Slack DM channel ID

    Returns:
        Dict with conversation info or None
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, slack_channel_id, slack_user_id,
                   conversation_id, last_message_ts, created_at, last_activity
            FROM slack_bot_conversations
            WHERE user_id = %s AND slack_channel_id = %s
        """, (user_id, slack_channel_id))

        row = cursor.fetchone()
        if row:
            return {
                "id": row["id"],
                "user_id": row["user_id"],
                "slack_channel_id": row["slack_channel_id"],
                "slack_user_id": row["slack_user_id"],
                "conversation_id": row["conversation_id"],
                "last_message_ts": row["last_message_ts"],
                "created_at": row["created_at"],
                "last_activity": row["last_activity"]
            }
        return None


def create_slack_bot_conversation(
    user_id: int,
    slack_channel_id: str,
    slack_user_id: str,
    conversation_id: str
) -> int:
    """
    Create a new Slack bot conversation link.

    Args:
        user_id: User's unique identifier
        slack_channel_id: Slack DM channel ID
        slack_user_id: Slack user ID who is chatting with the bot
        conversation_id: Seny conversation ID

    Returns:
        ID of created row
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO slack_bot_conversations
            (user_id, slack_channel_id, slack_user_id, conversation_id)
            VALUES (%s, %s, %s, %s)
            RETURNING id
        """, (user_id, slack_channel_id, slack_user_id, conversation_id))
        row = cursor.fetchone()
        return row['id'] if row else None


def update_slack_bot_conversation(
    user_id: int,
    slack_channel_id: str,
    last_message_ts: str = None
) -> None:
    """
    Update last_message_ts and last_activity for a Slack bot conversation.

    Args:
        user_id: User's unique identifier
        slack_channel_id: Slack DM channel ID
        last_message_ts: Timestamp of last processed message
    """
    with get_db() as conn:
        cursor = conn.cursor()
        if last_message_ts:
            cursor.execute("""
                UPDATE slack_bot_conversations
                SET last_message_ts = %s, last_activity = CURRENT_TIMESTAMP
                WHERE user_id = %s AND slack_channel_id = %s
            """, (last_message_ts, user_id, slack_channel_id))
        else:
            cursor.execute("""
                UPDATE slack_bot_conversations
                SET last_activity = CURRENT_TIMESTAMP
                WHERE user_id = %s AND slack_channel_id = %s
            """, (user_id, slack_channel_id))


def delete_slack_bot_conversation(user_id: int, slack_channel_id: str) -> None:
    """
    Delete the conversation link for a Slack bot DM channel.

    Used to clear stale links whose conversation_id no longer exists
    in the conversations table (e.g. after a data migration).

    Args:
        user_id: User's unique identifier
        slack_channel_id: Slack DM channel ID
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM slack_bot_conversations
            WHERE user_id = %s AND slack_channel_id = %s
        """, (user_id, slack_channel_id))


def list_slack_bot_conversations(user_id: int) -> list[dict]:
    """
    List all Slack bot conversations for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        List of conversation dicts
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, slack_channel_id, slack_user_id,
                   conversation_id, last_message_ts, created_at, last_activity
            FROM slack_bot_conversations
            WHERE user_id = %s
            ORDER BY last_activity DESC
        """, (user_id,))

        return [
            {
                "id": row["id"],
                "user_id": row["user_id"],
                "slack_channel_id": row["slack_channel_id"],
                "slack_user_id": row["slack_user_id"],
                "conversation_id": row["conversation_id"],
                "last_message_ts": row["last_message_ts"],
                "created_at": row["created_at"],
                "last_activity": row["last_activity"]
            }
            for row in cursor.fetchall()
        ]


# =============================================================================
# Multi-Channel Chat Settings Functions
# =============================================================================

def get_multichannel_chat_settings(user_id: int) -> dict:
    """
    Get multi-channel chat settings for a user.

    Creates default settings if they don't exist.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict with telegram_chat_enabled and slack_chat_enabled
    """
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT telegram_chat_enabled, slack_chat_enabled
            FROM multichannel_chat_settings
            WHERE user_id = %s
        """, (user_id,))

        row = cursor.fetchone()
        if row:
            return {
                "telegram_chat_enabled": bool(row["telegram_chat_enabled"]),
                "slack_chat_enabled": bool(row["slack_chat_enabled"])
            }

        # Create default settings
        cursor.execute("""
            INSERT INTO multichannel_chat_settings (user_id)
            VALUES (%s)
        """, (user_id,))

        return {
            "telegram_chat_enabled": True,
            "slack_chat_enabled": True
        }


def update_multichannel_chat_settings(
    user_id: int,
    telegram_chat_enabled: bool = None,
    slack_chat_enabled: bool = None
) -> bool:
    """
    Update multi-channel chat settings for a user.

    Args:
        user_id: User's unique identifier
        telegram_chat_enabled: Enable/disable Telegram bot chat
        slack_chat_enabled: Enable/disable Slack bot chat

    Returns:
        True if successful
    """
    # Build update clause dynamically based on provided params
    updates = []
    values = []

    if telegram_chat_enabled is not None:
        updates.append("telegram_chat_enabled = %s")
        values.append(1 if telegram_chat_enabled else 0)

    if slack_chat_enabled is not None:
        updates.append("slack_chat_enabled = %s")
        values.append(1 if slack_chat_enabled else 0)

    if not updates:
        return True  # Nothing to update

    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(user_id)

    with get_db() as conn:
        cursor = conn.cursor()

        # Try update first
        cursor.execute(f"""
            UPDATE multichannel_chat_settings
            SET {', '.join(updates)}
            WHERE user_id = %s
        """, tuple(values))

        if cursor.rowcount == 0:
            # Row doesn't exist, insert with defaults then update
            cursor.execute("""
                INSERT INTO multichannel_chat_settings (user_id)
                VALUES (%s)
            """, (user_id,))
            # Re-run update
            cursor.execute(f"""
                UPDATE multichannel_chat_settings
                SET {', '.join(updates)}
                WHERE user_id = %s
            """, tuple(values))

        return True


def is_telegram_chat_enabled(user_id: int) -> bool:
    """
    Check if Telegram bot chat is enabled for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        True if enabled, False otherwise
    """
    settings = get_multichannel_chat_settings(user_id)
    return settings.get("telegram_chat_enabled", True)


def is_slack_chat_enabled(user_id: int) -> bool:
    """
    Check if Slack bot chat is enabled for a user.

    Args:
        user_id: User's unique identifier

    Returns:
        True if enabled, False otherwise
    """
    settings = get_multichannel_chat_settings(user_id)
    return settings.get("slack_chat_enabled", True)


def get_unembedded_ids(entity_type: str, user_id: int, source_ids: list) -> list:
    """
    Given a list of candidate entity IDs, return only those NOT yet in
    embedding_tracking for this user and entity_type.

    Used for idempotency: callers pass all candidate IDs and receive only
    the subset that has not yet been embedded.

    Args:
        entity_type: One of "items", "notes", "conversations", "people", "projects", "ideas"
        user_id: User's unique identifier
        source_ids: List of string entity IDs to check

    Returns:
        List of entity ID strings that have not yet been embedded
    """
    if not source_ids:
        return []

    with get_db() as conn:
        placeholders = ", ".join(["%s"] * len(source_ids))
        _cur = conn.cursor()

        _cur.execute(
            f"""
            SELECT entity_id FROM embedding_tracking
            WHERE entity_type = %s AND user_id = %s AND entity_id IN ({placeholders})
            """,
            [entity_type, user_id] + list(source_ids),
        )

        rows = _cur.fetchall()

    already_embedded = {row["entity_id"] if isinstance(row, dict) else row[0] for row in rows}
    return [sid for sid in source_ids if sid not in already_embedded]


def save_embedding_records(records: list) -> None:
    """
    Batch-insert or replace embedding tracking records.

    Each record must have:
        {"entity_type": str, "entity_id": str, "user_id": int, "content_hash": str}

    Uses INSERT OR REPLACE so re-embedding an entity updates its embedded_at timestamp
    and content_hash without violating the UNIQUE constraint.

    Args:
        records: List of dicts with entity_type, entity_id, user_id, content_hash
    """
    if not records:
        return

    rows = [
        (r["entity_type"], r["entity_id"], r["user_id"], r["content_hash"])
        for r in records
    ]

    with get_db() as conn:
        conn.cursor().executemany(
            """
            INSERT INTO embedding_tracking
                (entity_type, entity_id, user_id, content_hash)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (entity_type, entity_id, user_id) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                embedded_at = NOW()
            """,
            rows,
        )


def get_embedding_tracking_summary(user_id: int) -> dict:
    """
    Return a count of embedded entities per type for the given user.

    Queries the embedding_tracking table and groups by entity_type.
    Missing entity types default to 0.

    Args:
        user_id: User's unique identifier

    Returns:
        Dict: {"items": N, "notes": N, "conversations": N, "people": N, "projects": N, "ideas": N}
    """
    defaults = {"items": 0, "notes": 0, "conversations": 0, "people": 0, "projects": 0, "ideas": 0}
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            """
            SELECT entity_type, COUNT(*) as count
            FROM embedding_tracking
            WHERE user_id = %s
            GROUP BY entity_type
            """,
            (user_id,),
        )

        rows = _cur.fetchall()
    for row in rows:
        entity_type = row[0] if isinstance(row, tuple) else row["entity_type"]
        count = row[1] if isinstance(row, tuple) else row["count"]
        if entity_type in defaults:
            defaults[entity_type] = count
    return defaults


def get_all_users_for_embedding() -> list:
    """
    Return all user IDs for embedding migration.

    Returns:
        List of dicts with {"id": int} for each user
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute("SELECT id FROM users")

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


def get_all_item_classifications(user_id: int) -> list:
    """
    Fetch all item classifications joined with scanned_items for embedding.

    Returns list of dicts with id, summary, thread_summary, relevance, urgency,
    classified_at, and source fields.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dicts with item classification data
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            """
            SELECT ic.id, ic.summary, ic.thread_summary, ic.relevance, ic.urgency,
                   ic.classified_at, si.source
            FROM item_classifications ic
            JOIN scanned_items si ON ic.scanned_item_id = si.id
            WHERE ic.user_id = %s
            """,
            (user_id,),
        )

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


def get_all_notes_for_embedding(user_id: int) -> list:
    """
    Fetch all notes for embedding.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dicts with id, title, content
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            "SELECT id, title, content FROM notes WHERE user_id = %s",
            (user_id,),
        )

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


def get_all_conversations_for_embedding(user_id: int) -> list:
    """
    Fetch all conversations with their messages concatenated for embedding.

    Groups messages by conversation, concatenating role and content.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dicts with id and text (concatenated messages)
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            """
            SELECT c.id, STRING_AGG(m.role || ': ' || m.content, CHR(10) ORDER BY m.id) as text
            FROM conversations c
            JOIN messages m ON m.conversation_id = c.id
            WHERE c.user_id = %s
            GROUP BY c.id
            """,
            (user_id,),
        )

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


def get_all_people_for_embedding(user_id: int) -> list:
    """
    Fetch all people for embedding.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dicts with id, name, context, notes
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            "SELECT id, name, context, notes FROM people WHERE user_id = %s",
            (user_id,),
        )

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


def get_all_projects_for_embedding(user_id: int) -> list:
    """
    Fetch all projects for embedding.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dicts with id, name, status, next_action, notes
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            "SELECT id, name, status, next_action, notes FROM projects WHERE user_id = %s",
            (user_id,),
        )

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


def get_all_ideas_for_embedding(user_id: int) -> list:
    """
    Fetch all ideas for embedding.

    Args:
        user_id: User's unique identifier

    Returns:
        List of dicts with id, title, summary, notes, tags
    """
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            "SELECT id, title, summary, notes, tags FROM ideas WHERE user_id = %s",
            (user_id,),
        )

        rows = _cur.fetchall()
    return [dict(row) for row in rows]


# ============================================================================
# Predictive Intelligence Helpers
# ============================================================================

def get_user_identifiers(user_id: int) -> dict:
    """
    Return the user's own email addresses, Slack member IDs, Telegram user IDs, and display names.

    Used by InboundClassifier (ISS-003) to filter outbound messages the user sent
    from being re-classified as inbound items. Also used to inject the user's name
    into the classification prompt so the AI knows when it's about to suggest
    "respond to yourself".

    Returns:
        Dict with keys:
          'emails'        — list of lowercase email strings (from google_tokens)
          'slack_ids'     — list of Slack member ID strings (from slack_tokens.authed_user_id)
          'telegram_ids'  — list of Telegram user ID strings (from user_settings JSON)
          'display_names' — list of the user's own name strings (from telegram_sessions)

        Each list is empty if the relevant table/column doesn't exist or the query fails.
        Never raises — all failures are swallowed silently.
    """
    result: dict = {'emails': [], 'slack_ids': [], 'telegram_ids': [], 'display_names': []}

    try:
        with get_db() as conn:
            cursor = conn.cursor()

            # Gmail / Google accounts
            try:
                cursor.execute(
                    "SELECT email FROM google_tokens WHERE user_id = %s",
                    (user_id,)
                )
                for row in cursor.fetchall():
                    email = (row['email'] or '').strip().lower()
                    if email:
                        result['emails'].append(email)
            except Exception:
                pass  # Table missing or column mismatch — safe to skip

            # Slack workspaces — authed_user_id is the Slack member ID (e.g. U012AB3CD)
            try:
                cursor.execute(
                    "SELECT authed_user_id FROM slack_tokens WHERE user_id = %s",
                    (user_id,)
                )
                for row in cursor.fetchall():
                    slack_id = (row['authed_user_id'] or '').strip()
                    if slack_id:
                        result['slack_ids'].append(slack_id)
            except Exception:
                pass

            # Telegram — from telegram_bot_user_links (private chat ID = Telegram user ID)
            try:
                cursor.execute(
                    "SELECT telegram_chat_id FROM telegram_bot_user_links WHERE user_id = %s",
                    (user_id,)
                )
                for row in cursor.fetchall():
                    tg_id = row['telegram_chat_id']
                    if tg_id:
                        result['telegram_ids'].append(str(tg_id))
            except Exception:
                pass

            # Display names — from Telegram session records (display_name, user_name)
            # These are the names the user appears as in conversations (e.g. "Your Name")
            try:
                cursor.execute(
                    "SELECT display_name, user_name FROM telegram_sessions WHERE user_id = %s",
                    (user_id,)
                )
                for row in cursor.fetchall():
                    for name in [row['display_name'], row['user_name']]:
                        if name and name.strip() and name not in result['display_names']:
                            result['display_names'].append(name.strip())
            except Exception:
                pass

            # HF-04: Derive display_names from users.email as guaranteed fallback.
            # e.g. yourname@gmail.com → "Your Name" and also adds the full email.
            try:
                cursor.execute(
                    "SELECT email FROM users WHERE id = %s",
                    (user_id,)
                )
                row = cursor.fetchone()
                if row and row['email']:
                    email = row['email'].strip().lower()
                    # Add full email as a recognised identity token
                    if email not in result['display_names']:
                        result['display_names'].append(email)
                    # Derive a human-readable name from the local part (before @)
                    local_part = email.split('@')[0]  # e.g. "yourname"
                    # Replace dots, underscores, hyphens with spaces then title-case each segment
                    segments = local_part.replace('_', '.').replace('-', '.').split('.')
                    derived_name = ' '.join(s.capitalize() for s in segments if s)
                    # e.g. "yourname" → "Your Name" (single-char segments keep their dot)
                    # Re-join preserving single-letter initials with a dot
                    parts = local_part.replace('_', '.').replace('-', '.').split('.')
                    rebuilt = []
                    for p in parts:
                        if not p:
                            continue
                        if len(p) == 1:
                            rebuilt.append(p.upper() + '.')
                        else:
                            rebuilt.append(p.capitalize())
                    if rebuilt:
                        derived_name = ' '.join(rebuilt).strip()
                        if derived_name and derived_name not in result['display_names']:
                            result['display_names'].append(derived_name)
            except Exception:
                pass  # Table missing or column mismatch — safe to skip

            # HF-04: Check google_tokens for a display_name or name column (future-proof).
            # If the column exists, pull it; if not, skip silently.
            try:
                cursor.execute(
                    "SELECT display_name FROM google_tokens WHERE user_id = %s",
                    (user_id,)
                )
                for row in cursor.fetchall():
                    name = (row['display_name'] or '').strip()
                    if name and name not in result['display_names']:
                        result['display_names'].append(name)
            except Exception:
                pass  # Column doesn't exist yet — safe to skip

    except Exception:
        pass  # DB connection failure — return empty dict

    return result


def count_nudges_today(user_id: int, nudge_type: str) -> int:
    """
    Count nudges of a given type sent to the user today (UTC).

    Used by PredictiveService to enforce daily caps so multiple scheduler runs
    don't stack up more than the allowed daily quota.

    Args:
        user_id:    User's unique identifier.
        nudge_type: Nudge type string (e.g. 'meeting_prep', 'relationship_check').

    Returns:
        Integer count of matching nudges created today. Returns 0 on any error.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM nudges
                WHERE user_id = %s
                  AND nudge_type = %s
                  AND DATE(created_at) = CURRENT_DATE
                """,
                (user_id, nudge_type)
            )
            row = cursor.fetchone()
            return row['cnt'] if row else 0
    except Exception:
        return 0


# ============================================================================
# PRIORITY CONTEXT HELPERS
# ============================================================================

_priority_logger = logging.getLogger("priority_context")

def add_priority_item(
    user_id: int,
    item_type: str,
    title: str,
    description: Optional[str] = None,
    source: str = 'chat',
    source_id: Optional[str] = None,
    priority_level: int = 0,
    due_at: Optional[str] = None,
) -> Optional[int]:
    """Add an item to the user's priority context stack. Returns new item id or None on error."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                INSERT INTO priority_context
                    (user_id, item_type, title, description, source, source_id, priority_level, due_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, item_type, title, description, source, source_id, priority_level, due_at))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _priority_logger.error(f"add_priority_item error: {repr(e)}")
        return None


def get_priority_items(
    user_id: int,
    status: str = 'active',
    limit: int = 50,
) -> list:
    """Get priority context items for a user. Ordered by priority_level DESC, then created_at DESC."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT id, user_id, item_type, title, description, source, source_id,
                       priority_level, status, due_at, resolved_at, created_at, updated_at
                FROM priority_context
                WHERE user_id = %s AND status = %s
                ORDER BY priority_level DESC, created_at DESC
                LIMIT %s
            """, (user_id, status, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _priority_logger.error(f"get_priority_items error: {repr(e)}")
        return []


def get_priority_item(item_id: int, user_id: int) -> Optional[dict]:
    """Get a single priority context item by ID. Returns None if not found or not owned by user."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT id, user_id, item_type, title, description, source, source_id,
                       priority_level, status, due_at, resolved_at, created_at, updated_at
                FROM priority_context
                WHERE id = %s AND user_id = %s
            """, (item_id, user_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _priority_logger.error(f"get_priority_item error: {repr(e)}")
        return None


def resolve_priority_item(item_id: int, user_id: int) -> bool:
    """Mark a priority context item as resolved. Returns True on success."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                UPDATE priority_context
                SET status = 'resolved', resolved_at = NOW(), updated_at = NOW()
                WHERE id = %s AND user_id = %s
            """, (item_id, user_id))
            return cursor.rowcount > 0
    except Exception as e:
        _priority_logger.error(f"resolve_priority_item error: {repr(e)}")
        return False


# ============================================================================
# Calendar Event Nudge Helpers
# ============================================================================

_cal_nudge_logger = logging.getLogger('calendar_nudge')


def has_event_nudge_sequence(user_id: int, event_id: str) -> bool:
    """Return True if any non-cancelled rows exist for this event."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM calendar_event_nudges
                WHERE user_id = %s AND event_id = %s AND status != 'cancelled'
            """, (user_id, event_id))
            row = cursor.fetchone()
            return (row[0] > 0) if row else False
    except Exception as e:
        _cal_nudge_logger.error(f"has_event_nudge_sequence error: {repr(e)}")
        return False


def schedule_event_nudge_sequence(
    user_id: int,
    event_id: str,
    event_title: str,
    event_start: str,
    event_end,
    is_all_day: bool,
    attendees_json,
    description,
    nudge_rows: list,
) -> bool:
    """Insert nudge rows atomically. Skips if sequence already exists. Returns True on success."""
    try:
        if has_event_nudge_sequence(user_id, event_id):
            _cal_nudge_logger.info(
                f"schedule_event_nudge_sequence: sequence already exists for user={user_id} event={event_id}, skipping"
            )
            return False
        with get_db() as db:
            cursor = db.cursor()
            for row in nudge_rows:
                cursor.execute("""
                    INSERT INTO calendar_event_nudges
                        (user_id, event_id, event_title, event_start, event_end,
                         event_attendees, event_description, is_all_day,
                         offset_minutes, scheduled_for, status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                """, (
                    user_id,
                    event_id,
                    event_title,
                    event_start,
                    event_end,
                    attendees_json,
                    description,
                    1 if is_all_day else 0,
                    row['offset_minutes'],
                    row['scheduled_for'],
                ))
            db.commit()
            return True
    except Exception as e:
        _cal_nudge_logger.error(f"schedule_event_nudge_sequence error: {repr(e)}")
        return False


def get_due_event_nudges() -> list:
    """Return all pending rows across all users where scheduled_for <= now (UTC)."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT * FROM calendar_event_nudges
                WHERE status = 'pending'
                  AND scheduled_for::timestamp <= NOW()
                ORDER BY scheduled_for ASC
            """)
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        _cal_nudge_logger.error(f"get_due_event_nudges error: {repr(e)}")
        return []


def mark_event_nudge_sent(row_id: int, nudge_id: int) -> bool:
    """Mark row as sent and record the nudge_id. Returns True on success."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                UPDATE calendar_event_nudges
                SET status = 'sent', nudge_id = %s
                WHERE id = %s
            """, (nudge_id, row_id))
            db.commit()
            return cursor.rowcount > 0
    except Exception as e:
        _cal_nudge_logger.error(f"mark_event_nudge_sent error: {repr(e)}")
        return False


def cancel_event_nudge_sequence(user_id: int, event_id: str) -> int:
    """Cancel all pending rows for this event. Returns count cancelled."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                UPDATE calendar_event_nudges
                SET status = 'cancelled'
                WHERE user_id = %s AND event_id = %s AND status = 'pending'
            """, (user_id, event_id))
            db.commit()
            return cursor.rowcount
    except Exception as e:
        _cal_nudge_logger.error(f"cancel_event_nudge_sequence error: {repr(e)}")
        return 0


def get_pending_event_sequences(user_id: int) -> list:
    """Return distinct (event_id, event_start, event_title) for all pending nudge sequences for a user."""
    try:
        with get_db() as db:
            cursor = db.cursor()
            cursor.execute("""
                SELECT DISTINCT event_id, event_start, event_title
                FROM calendar_event_nudges
                WHERE user_id = %s AND status = 'pending'
            """, (user_id,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        _cal_nudge_logger.error(f"get_pending_event_sequences error: {repr(e)}")
        return []


# ============================================================================
# SECTION: Screen Agent API Key Helpers
# ============================================================================

def get_screen_agent_key(user_id: int) -> Optional[str]:
    """Retrieve the static screen agent API key for a user."""
    with get_db() as conn:
        _cur = conn.cursor()

        _cur.execute(
            "SELECT screen_agent_api_key FROM user_settings WHERE user_id = %s",
            (user_id,)
        )

        row = _cur.fetchone()
        return row[0] if row else None


def set_screen_agent_key(user_id: int, key: str) -> None:
    """Store the static screen agent API key for a user."""
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE user_settings SET screen_agent_api_key = %s WHERE user_id = %s",
            (key, user_id)
        )


# ---------------------------------------------------------------------------
# HF-14: Screen agent cooldown state (shared across workers)
# ---------------------------------------------------------------------------

def get_screen_cooldown_until(user_id: str) -> float:
    """Return epoch timestamp until which screen nudges are suppressed (0.0 if none)."""
    import logging as _logging
    _log = _logging.getLogger("screen_agent")
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT screen_cooldown_until FROM user_settings WHERE user_id = %s",
                (int(user_id),)
            )
            row = cur.fetchone()
            val = row['screen_cooldown_until'] if row else None
            result = float(val) if val else 0.0
            _log.info("[screen_cooldown] get user_id=%s → %.0f", user_id, result)
            return result
    except Exception as e:
        _log.error("[screen_cooldown] get FAILED for user_id=%s: %s", user_id, repr(e))
        return 0.0


def set_screen_cooldown_until(user_id: str, until_ts: float) -> None:
    """Set screen nudge cooldown expiry (epoch float)."""
    import logging as _logging
    _log = _logging.getLogger("screen_agent")
    try:
        with get_db() as conn:
            conn.cursor().execute(
                """INSERT INTO user_settings (user_id, screen_cooldown_until)
                   VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET screen_cooldown_until = EXCLUDED.screen_cooldown_until""",
                (int(user_id), until_ts)
            )
        _log.info("[screen_cooldown] set user_id=%s until=%.0f", user_id, until_ts)
    except Exception as e:
        _log.error("[screen_cooldown] set FAILED for user_id=%s: %s", user_id, repr(e))


def get_screen_last_nudge_at(user_id: str) -> float:
    """Return epoch timestamp of last screen nudge sent (0.0 if none)."""
    import logging as _logging
    _log = _logging.getLogger("screen_agent")
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT screen_last_nudge_at FROM user_settings WHERE user_id = %s",
                (int(user_id),)
            )
            row = cur.fetchone()
            val = row['screen_last_nudge_at'] if row else None
            return float(val) if val else 0.0
    except Exception as e:
        _log.error("[screen_cooldown] get_last_nudge FAILED for user_id=%s: %s", user_id, repr(e))
        return 0.0


def set_screen_last_nudge_at(user_id: str, ts: float) -> None:
    """Record when the last screen nudge was sent (epoch float)."""
    import logging as _logging
    _log = _logging.getLogger("screen_agent")
    try:
        with get_db() as conn:
            conn.cursor().execute(
                """INSERT INTO user_settings (user_id, screen_last_nudge_at)
                   VALUES (%s, %s)
                   ON CONFLICT (user_id) DO UPDATE SET screen_last_nudge_at = EXCLUDED.screen_last_nudge_at""",
                (int(user_id), ts)
            )
    except Exception as e:
        _log.error("[screen_cooldown] set_last_nudge FAILED for user_id=%s: %s", user_id, repr(e))


def clear_screen_last_nudge_at(user_id: str) -> None:
    """Clear last nudge timestamp so next message goes to normal handler."""
    try:
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE user_settings SET screen_last_nudge_at = NULL WHERE user_id = %s",
                (int(user_id),)
            )
    except Exception:
        pass


def add_screen_nudge_message(telegram_message_id: int, user_id: str) -> None:
    """Track a Telegram message ID as a screen nudge (for reply detection)."""
    try:
        import time as _time
        with get_db() as conn:
            conn.cursor().execute(
                """INSERT INTO screen_nudge_messages (telegram_message_id, user_id, sent_at)
                   VALUES (%s, %s, %s) ON CONFLICT (telegram_message_id) DO NOTHING""",
                (telegram_message_id, int(user_id), _time.time())
            )
    except Exception:
        pass


def get_screen_nudge_message_user(telegram_message_id: int) -> "Optional[str]":
    """Return user_id string for a tracked screen nudge message_id, or None if not found/expired."""
    try:
        import time as _time
        cutoff = _time.time() - 3600  # Only look back 1 hour
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT user_id FROM screen_nudge_messages
                   WHERE telegram_message_id = %s AND sent_at > %s""",
                (telegram_message_id, cutoff)
            )
            row = cur.fetchone()
            return str(row['user_id']) if row else None
    except Exception:
        return None


# ============================================================================
# Pending Actions Queue Helpers
# ============================================================================

_pending_actions_logger = logging.getLogger("pending_actions")


def create_pending_action(
    user_id: int,
    action_type: str,
    title: str,
    content_json: str,
    source: str = 'proactive',
    source_ref: Optional[str] = None,
) -> Optional[int]:
    """
    Create a new pending action record.

    Args:
        user_id: User's unique identifier
        action_type: 'email_draft' | 'calendar_proposal' | 'task_proposal'
        title: Human-readable card title
        content_json: JSON blob (shape depends on action_type — see init_db comment)
        source: 'proactive' | 'scanner' | 'claude_chat'
        source_ref: Optional reference (scanned_item_id, email thread_id, etc.)

    Returns:
        New pending action ID if successful, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO pending_actions
                    (user_id, action_type, title, content_json, source, source_ref)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, action_type, title, content_json, source, source_ref))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _pending_actions_logger.error("create_pending_action error: %s", repr(e))
        return None


def get_pending_action(user_id: int, action_id: int) -> Optional[dict]:
    """
    Get a single pending action by ID.

    The user_id check is a security guard — prevents cross-user data access.

    Args:
        user_id: User's unique identifier
        action_id: Pending action ID

    Returns:
        Action dict if found and owned by user, None otherwise
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, action_type, title, content_json, status,
                       source, source_ref, notification_sent, notification_channel,
                       created_at, updated_at, resolved_at
                FROM pending_actions
                WHERE id = %s AND user_id = %s
            """, (action_id, user_id))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _pending_actions_logger.error("get_pending_action error: %s", repr(e))
        return None


def list_pending_actions(
    user_id: int,
    status: Optional[str] = None,
    action_type: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    List pending actions for a user with optional filters.

    Args:
        user_id: User's unique identifier
        status: Optional filter — 'pending' | 'approved' | 'dismissed'
        action_type: Optional filter — 'email_draft' | 'calendar_proposal' | 'task_proposal'
        limit: Maximum actions to return

    Returns:
        List of action dicts ordered by created_at DESC
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            query = """
                SELECT id, user_id, action_type, title, content_json, status,
                       source, source_ref, notification_sent, notification_channel,
                       created_at, updated_at, resolved_at
                FROM pending_actions
                WHERE user_id = %s
            """
            params: list = [user_id]

            if status is not None:
                query += " AND status = %s"
                params.append(status)

            if action_type is not None:
                query += " AND action_type = %s"
                params.append(action_type)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _pending_actions_logger.error("list_pending_actions error: %s", repr(e))
        return []


def count_pending_actions(user_id: int, status: str = 'pending') -> int:
    """
    Count pending actions for a user — used for sidebar badge.

    Args:
        user_id: User's unique identifier
        status: Status to count (default 'pending')

    Returns:
        Count of matching actions, 0 on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM pending_actions
                WHERE user_id = %s AND status = %s
            """, (user_id, status))
            row = cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        _pending_actions_logger.error("count_pending_actions error: %s", repr(e))
        return 0


def get_pending_action_by_source_ref(user_id: int, source_ref: str) -> Optional[dict]:
    """
    Look up a pending action by source_ref for deduplication.

    Used by the email draft scanner to avoid creating duplicate cards
    for the same scanned item.

    Args:
        user_id: User's unique identifier
        source_ref: The source reference string (e.g. scanned_item_id as str)

    Returns:
        Action dict if found (any status), None if not found or on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, status FROM pending_actions
                WHERE user_id = %s AND source_ref = %s
                LIMIT 1
            """, (user_id, source_ref))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _pending_actions_logger.error("get_pending_action_by_source_ref error: %s", repr(e))
        return None


def update_pending_action_status(user_id: int, action_id: int, status: str) -> bool:
    """
    Update the status of a pending action.

    Sets resolved_at to now when status is not 'pending'.

    Args:
        user_id: User's unique identifier (security check)
        action_id: Pending action ID
        status: New status — 'pending' | 'approved' | 'dismissed'

    Returns:
        True if a row was updated, False on error or not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            if status != 'pending':
                cursor.execute("""
                    UPDATE pending_actions
                    SET status = %s, resolved_at = NOW(), updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                """, (status, action_id, user_id))
            else:
                cursor.execute("""
                    UPDATE pending_actions
                    SET status = %s, resolved_at = NULL, updated_at = NOW()
                    WHERE id = %s AND user_id = %s
                """, (status, action_id, user_id))
            return cursor.rowcount > 0
    except Exception as e:
        _pending_actions_logger.error("update_pending_action_status error: %s", repr(e))
        return False


def update_pending_action_content(
    user_id: int,
    action_id: int,
    title: str,
    content_json: str,
) -> bool:
    """
    Update the title and content_json of a pending action.

    Args:
        user_id: User's unique identifier (security check)
        action_id: Pending action ID
        title: New title
        content_json: New JSON blob

    Returns:
        True if a row was updated, False on error or not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE pending_actions
                SET title = %s, content_json = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s
            """, (title, content_json, action_id, user_id))
            return cursor.rowcount > 0
    except Exception as e:
        _pending_actions_logger.error("update_pending_action_content error: %s", repr(e))
        return False


def get_unnotified_pending_actions(user_id: int, limit: int = 10) -> list[dict]:
    """
    Get pending actions that have not yet been notified — used by Phase 47 scheduler.

    Args:
        user_id: User's unique identifier
        limit: Maximum actions to return

    Returns:
        List of action dicts where status='pending' and notification_sent=0
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, user_id, action_type, title, content_json, status,
                       source, source_ref, notification_sent, notification_channel,
                       created_at, updated_at, resolved_at
                FROM pending_actions
                WHERE user_id = %s AND status = 'pending' AND notification_sent = 0
                ORDER BY created_at ASC
                LIMIT %s
            """, (user_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _pending_actions_logger.error("get_unnotified_pending_actions error: %s", repr(e))
        return []


def get_users_with_unnotified_pending_actions() -> list[int]:
    """
    Return distinct user IDs who have at least one pending action with notification_sent=0.
    Used by scheduler to avoid scanning all users on every tick.
    """
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT DISTINCT user_id
                FROM pending_actions
                WHERE status = 'pending' AND notification_sent = 0
            """)
            return [row['user_id'] for row in cursor.fetchall()]
        except Exception as e:
            logger.error("get_users_with_unnotified_pending_actions error: %s", repr(e))
            return []


def mark_pending_action_notified(action_id: int, channel: str) -> bool:
    """
    Mark a pending action as notified and record the delivery channel.

    Args:
        action_id: Pending action ID
        channel: Channel used — 'telegram' | 'slack'

    Returns:
        True if a row was updated, False on error or not found
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE pending_actions
                SET notification_sent = 1, notification_channel = %s, updated_at = NOW()
                WHERE id = %s
            """, (channel, action_id))
            return cursor.rowcount > 0
    except Exception as e:
        _pending_actions_logger.error("mark_pending_action_notified error: %s", repr(e))
        return False


# ============================================================================
# HF-13: Screen Agent Dismissal Helpers
# ============================================================================

_screen_dismiss_logger = logging.getLogger("screen_agent.dismissals")


def record_screen_dismissal(
    user_id: int,
    vision_status: str,
    user_reason: Optional[str] = None,
    calendar_context: Optional[str] = None,
    accepted: bool = True,
) -> Optional[int]:
    """
    Record a screen agent dismissal for pattern learning.

    Args:
        user_id: User's database ID
        vision_status: What the Vision model flagged (e.g. "drifting")
        user_reason: What the user said when dismissing (e.g. "watching a tutorial")
        calendar_context: Brief summary of calendar state at dismissal time
        accepted: Whether Claude accepted the dismissal (True) or pushed back (False)

    Returns:
        Row ID if created, None on error
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO screen_agent_dismissals
                    (user_id, vision_status, user_reason, calendar_context, accepted)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, vision_status, user_reason, calendar_context, accepted))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _screen_dismiss_logger.error("record_screen_dismissal error: %s", repr(e))
        return None


def get_screen_dismissal_patterns(
    user_id: int,
    days: int = 30,
) -> list[dict]:
    """
    Get recent screen agent dismissals for pattern learning.

    Returns dismissals from the last N days, ordered newest first.

    Args:
        user_id: User's database ID
        days: Number of days to look back (default 30)

    Returns:
        List of dismissal dicts with id, vision_status, user_reason,
        calendar_context, accepted, dismissed_at
    """
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, vision_status, user_reason, calendar_context,
                       accepted, dismissed_at
                FROM screen_agent_dismissals
                WHERE user_id = %s
                  AND dismissed_at >= %s
                ORDER BY dismissed_at DESC
            """, (user_id, cutoff))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        _screen_dismiss_logger.error("get_screen_dismissal_patterns error: %s", repr(e))
        return []


# ============================================================================
# SECTION: User Status helpers
# ============================================================================

def set_user_status(user_id: int, status_text: str, expires_at=None) -> bool:
    """Set or update the user's current focus/context status."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO user_status (user_id, status_text, set_at, expires_at)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT (user_id) DO UPDATE
                SET status_text = EXCLUDED.status_text,
                    set_at = NOW(),
                    expires_at = EXCLUDED.expires_at
            """, (user_id, status_text, expires_at))
            return True
    except Exception as e:
        logger.error("set_user_status failed: %s", repr(e))
        return False


def get_user_status(user_id: int) -> Optional[dict]:
    """
    Get current active status for user.
    Returns None if no status set or status has expired.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status_text, set_at, expires_at
                FROM user_status
                WHERE user_id = %s
                  AND (expires_at IS NULL OR expires_at > NOW())
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error("get_user_status failed: %s", repr(e))
        return None


def clear_user_status(user_id: int) -> bool:
    """Clear the user's current status."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM user_status WHERE user_id = %s", (user_id,))
            return True
    except Exception as e:
        logger.error("clear_user_status failed: %s", repr(e))
        return False


# ============================================================================
# SECTION: LCD (Living Context Document) helpers
# ============================================================================

_lcd_logger = logging.getLogger(__name__ + '.lcd')


def get_lcd_layer1(user_id: int):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, content, layer2_synthesis, layer2_synthesized_at, updated_at FROM lcd_layer1 WHERE user_id=%s",
                (user_id,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(row)
    except Exception as e:
        _lcd_logger.error("lcd DB error: %s", repr(e))
        return None


def set_lcd_layer1(user_id: int, content: str) -> None:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO lcd_layer1 (user_id, content, updated_at) VALUES (%s, %s, NOW()) ON CONFLICT (user_id) DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()",
                (user_id, content)
            )
    except Exception as e:
        _lcd_logger.error("lcd DB error: %s", repr(e))


def _resolve_nudges_from_lcd(user_id: int, observation_content: str, conn):
    """Phase 77: Auto-dismiss pending nudges whose title matches the LCD observation.

    Scans pending (unbatched) nudges and dismisses any whose title shares
    >=2 significant words (4+ chars) with the observation content.
    Also closes the underlying detected_action source when applicable.

    Called inside the same transaction as append_lcd_observation() -- uses
    the caller's connection so dismissals commit atomically with the INSERT.
    Fail-open: callers wrap this in try/except.
    """
    # Extract significant words (4+ chars) from observation
    words = re.findall(r'\b[a-zA-Z]{4,}\b', observation_content)
    if not words:
        return
    # Limit to first 10 to keep matching bounded
    sig_words = {w.lower() for w in words[:10]}
    if len(sig_words) < 2:
        return  # Need at least 2 significant words to match

    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, nudge_type, title, source_type, source_id"
        " FROM nudges"
        " WHERE user_id = %s AND status = 'pending' AND batch_id IS NULL",
        (user_id,)
    )
    pending = cursor.fetchall()
    if not pending:
        return

    for nudge in pending:
        nudge_title = nudge['title'] or ''
        title_words = {w.lower() for w in re.findall(r'\b[a-zA-Z]{4,}\b', nudge_title)}
        overlap = sig_words & title_words
        if len(overlap) >= 2:
            cursor.execute(
                "UPDATE nudges SET status = 'dismissed', dismiss_reason = %s WHERE id = %s",
                (
                    f"lcd_proactive_resolution: observation matched nudge title ({', '.join(sorted(overlap))})",
                    nudge['id'],
                )
            )
            _lcd_logger.info(
                "lcd proactive resolution: dismissed nudge %s (overlap: %s)",
                nudge['id'], overlap
            )
            # Close underlying detected_action if applicable
            if nudge['source_type'] == 'detected_action' and nudge['source_id']:
                cursor.execute(
                    "UPDATE detected_actions SET status = 'dismissed'"
                    " WHERE id = %s AND status != 'dismissed'",
                    (nudge['source_id'],)
                )


def append_lcd_observation(user_id: int, source: str, content: str):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            # Dedup: skip if identical content from same source already logged in last 60 min
            cursor.execute(
                "SELECT id FROM lcd_observation_log WHERE user_id=%s AND source=%s AND content=%s AND created_at > NOW() - INTERVAL '60 minutes' LIMIT 1",
                (user_id, source, content)
            )
            existing = cursor.fetchone()
            if existing:
                _lcd_logger.info("lcd dedup: skipping duplicate observation (id=%s)", existing['id'])
                return existing['id']
            cursor.execute(
                "INSERT INTO lcd_observation_log (user_id, source, content) VALUES (%s, %s, %s) RETURNING id",
                (user_id, source, content)
            )
            row = cursor.fetchone()
            # Ensure lcd_layer1 row exists and invalidate synthesis cache
            cursor.execute(
                "INSERT INTO lcd_layer1 (user_id, content, updated_at) VALUES (%s, '', NOW()) ON CONFLICT (user_id) DO UPDATE SET layer2_synthesized_at = NULL",
                (user_id,)
            )
            # Phase 77: proactive LCD resolution -- auto-dismiss matching nudges
            try:
                _resolve_nudges_from_lcd(user_id, content, conn)
            except Exception as _resolve_err:
                _lcd_logger.warning("lcd proactive resolution hook failed (non-blocking): %s", repr(_resolve_err))
            return row['id'] if row else None
    except Exception as e:
        _lcd_logger.error("lcd DB error: %s", repr(e))
        return None


def get_recent_lcd_observations(user_id: int, limit: int = 20) -> list:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, source, content, created_at FROM lcd_observation_log WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
                (user_id, limit)
            )
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        _lcd_logger.error("lcd DB error: %s", repr(e))
        return []


def set_lcd_synthesis(user_id: int, synthesis_text: str) -> None:
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO lcd_layer1 (user_id, content, layer2_synthesis, layer2_synthesized_at, updated_at) VALUES (%s, '', %s, NOW(), NOW()) ON CONFLICT (user_id) DO UPDATE SET layer2_synthesis=EXCLUDED.layer2_synthesis, layer2_synthesized_at=NOW()",
                (user_id, synthesis_text)
            )
    except Exception as e:
        _lcd_logger.error("lcd DB error: %s", repr(e))


def search_lcd_observations(user_id: int, query: str = None, source: str = None, days_back: int = None, limit: int = 15) -> list:
    """Search lcd_observation_log with optional keyword, source, and date filters.
    Returns list of dicts (id, source, content, created_at), newest first.
    Returns [] on any error — fail-open."""
    try:
        conditions = ["user_id=%s"]
        params = [user_id]
        if query:
            conditions.append("content ILIKE %s")
            params.append(f"%{query}%")
        if source:
            conditions.append("source=%s")
            params.append(source)
        if days_back:
            conditions.append("created_at >= NOW() - INTERVAL '1 day' * %s")
            params.append(days_back)
        where = " AND ".join(conditions)
        params.append(limit)
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT id, source, content, created_at FROM lcd_observation_log WHERE {where} ORDER BY created_at DESC LIMIT %s",
                params
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        _lcd_logger.error("search_lcd_observations error: %s", repr(e))
        return []


# ============================================================================
# NightlyResearchService Audit History helpers
# ============================================================================
_audit_logger = logging.getLogger(__name__ + ".audit")


def create_audit_run(
    user_id: int,
    fidelity_score: float,
    negative_unabsorbed_count: int,
    suppression_gap_count: int,
    signals_json: str,
) -> Optional[int]:
    """
    Store a nightly research audit run result.
    Returns new run ID, or None on error.
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO research_audit_runs
                    (user_id, fidelity_score, negative_unabsorbed_count,
                     suppression_gap_count, signals_json)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (user_id, fidelity_score, negative_unabsorbed_count,
                  suppression_gap_count, signals_json))
            row = cursor.fetchone()
            return row['id'] if row else None
    except Exception as e:
        _audit_logger.error("create_audit_run error for user %d: %s", user_id, repr(e))
        return None


def get_last_audit_run(user_id: int) -> Optional[dict]:
    """
    Return the most recent audit run for a user, or None if none exists.
    Used by NightlyResearchService to determine dedup cutoff (only process
    signals newer than the last run_at).
    """
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, run_at, fidelity_score, negative_unabsorbed_count,
                       suppression_gap_count
                FROM research_audit_runs
                WHERE user_id = %s
                ORDER BY run_at DESC
                LIMIT 1
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except Exception as e:
        _audit_logger.error("get_last_audit_run error for user %d: %s", user_id, repr(e))
        return None
