"""
Shared pytest fixtures for Seny test suite.

Provides:
- test_db: Fresh SQLite database for each test
- test_user_id: Consistent test user ID
- mock_anthropic: Mocked Anthropic API responses
"""

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime
from typing import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Test user ID used consistently across tests
TEST_USER_ID = 999


@pytest.fixture
def test_user_id() -> int:
    """Returns a consistent test user ID."""
    return TEST_USER_ID


@pytest.fixture
def test_db(tmp_path):
    """
    Create a fresh SQLite database for tests.

    Uses a temporary file that is cleaned up after each test.
    Creates minimal schema needed for proactive intelligence tests.

    Yields:
        Path to the temporary database file
    """
    db_path = tmp_path / "test_seny.db"

    # Create schema directly instead of using init_db()
    # (init_db has migrations that assume existing tables)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Core tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS people (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            email TEXT,
            phone TEXT,
            context TEXT,
            relationship_type TEXT,
            follow_up_frequency TEXT DEFAULT 'monthly',
            last_contact_date TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    # Scanner tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scanner_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            items_found INTEGER DEFAULT 0,
            items_classified INTEGER DEFAULT 0,
            error_message TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scanned_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            classification_attempts INTEGER DEFAULT 0,
            UNIQUE(user_id, source, source_id),
            FOREIGN KEY (scanner_run_id) REFERENCES scanner_runs(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS item_classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cross_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    # Feedback tables
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ignored_senders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            sender_identifier TEXT NOT NULL,
            ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            UNIQUE(user_id, source_type, sender_identifier)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS email_feedback_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    conn.commit()
    conn.close()

    # Patch DB_PATH for any imports that use it
    with patch("web.core.database.DB_PATH", db_path):
        yield db_path


@pytest.fixture
def test_db_conn(test_db):
    """
    Provide a database connection to the test database.

    Yields:
        sqlite3.Connection configured with Row factory
    """
    conn = sqlite3.connect(test_db)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def mock_anthropic():
    """
    Mock Anthropic API to return canned responses.

    Provides a mock that can be configured for different test scenarios.
    Default response simulates a classification result.
    """
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text="""<analysis>
<relevance>actionable</relevance>
<urgency>normal</urgency>
<summary>Test email about project update</summary>
<detected_entities>
- Project: Test Project
</detected_entities>
<detected_actions>
- action_type: follow_up
- description: Follow up on project status
- deadline: None
- priority: medium
</detected_actions>
</analysis>"""
        )
    ]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    mock_client.messages.create.return_value = mock_response

    with patch("anthropic.Anthropic", return_value=mock_client):
        yield mock_client


@pytest.fixture
def mock_anthropic_async():
    """
    Mock Anthropic AsyncAnthropic for async classifier tests.
    """
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text="""<analysis>
<relevance>actionable</relevance>
<urgency>normal</urgency>
<summary>Test email requiring action</summary>
<detected_entities>
- Person: John Doe
</detected_entities>
<detected_actions>
- action_type: reply
- description: Reply to John about meeting
- deadline: None
- priority: high
</detected_actions>
</analysis>"""
        )
    ]
    mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)

    # Create async mock for create method
    async_create = AsyncMock(return_value=mock_response)
    mock_client.messages.create = async_create

    with patch("anthropic.AsyncAnthropic", return_value=mock_client):
        yield mock_client


@pytest.fixture
def create_test_user(test_db):
    """
    Factory fixture to create test users in the database.

    Returns a function that creates users with specified parameters.
    """
    def _create_user(
        user_id: int = TEST_USER_ID,
        email: str = "test@example.com",
        hashed_password: str = "hashed_test_password"
    ) -> int:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO users (id, email, hashed_password)
            VALUES (?, ?, ?)
            """,
            (user_id, email, hashed_password)
        )
        conn.commit()
        conn.close()
        return user_id

    return _create_user


@pytest.fixture
def create_scanner_run(test_db):
    """
    Factory fixture to create scanner runs for testing.
    """
    def _create_run(user_id: int = TEST_USER_ID, source: str = "gmail") -> int:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO scanner_runs (user_id, source, status, started_at)
            VALUES (?, ?, 'running', datetime('now'))
            """,
            (user_id, source)
        )
        run_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return run_id

    return _create_run


@pytest.fixture
def create_scanned_item(test_db, create_scanner_run):
    """
    Factory fixture to create scanned items for testing.
    """
    def _create_item(
        user_id: int = TEST_USER_ID,
        source: str = "gmail",
        source_id: str = None,
        item_type: str = "email",
        metadata: dict = None,
        scanner_run_id: int = None
    ) -> int:
        if source_id is None:
            source_id = f"test_item_{datetime.now().timestamp()}"

        if scanner_run_id is None:
            scanner_run_id = create_scanner_run(user_id, source)

        metadata_json = "{}" if metadata is None else str(metadata).replace("'", '"')

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO scanned_items
            (user_id, scanner_run_id, source, source_id, source_metadata, item_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, scanner_run_id, source, source_id, metadata_json, item_type)
        )
        item_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return item_id

    return _create_item


@pytest.fixture
def create_person(test_db):
    """
    Factory fixture to create people entries for testing cross-references.
    """
    def _create_person(
        user_id: int = TEST_USER_ID,
        name: str = "John Doe",
        email: str = "john@example.com",
        phone: str = None,
        context: str = "Work colleague"
    ) -> int:
        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO people (user_id, name, email, phone, context)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, name, email, phone, context)
        )
        person_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return person_id

    return _create_person
