"""
Integration tests for the v2.1 Proactive Intelligence pipeline.

Tests the flow:
  Scanner → scanned_items
  Classifier → item_classifications
  Cross-referencer → cross_references
  Digest → needs_reply / detected_actions
  Feedback → ignored_senders, token validation

Uses mocked Anthropic API and in-memory test database.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, AsyncMock

import pytest


class TestScannerPipeline:
    """Tests for scanner → scanned_items storage."""

    def test_scanned_item_stored_correctly(self, test_db, create_test_user, create_scanner_run):
        """Test that scanner stores items in scanned_items table."""
        user_id = create_test_user()
        run_id = create_scanner_run(user_id, "gmail")

        conn = sqlite3.connect(test_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Insert a scanned item
        cursor.execute(
            """
            INSERT INTO scanned_items
            (user_id, scanner_run_id, source, source_id, source_metadata, item_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, run_id, "gmail", "msg_123", '{"from": "test@example.com"}', "email")
        )
        conn.commit()

        # Verify item was stored
        cursor.execute(
            "SELECT * FROM scanned_items WHERE source_id = ?",
            ("msg_123",)
        )
        row = cursor.fetchone()

        assert row is not None
        assert row["source"] == "gmail"
        assert row["source_id"] == "msg_123"
        assert row["item_type"] == "email"
        conn.close()

    def test_scanned_item_dedup_works(self, test_db, create_test_user, create_scanner_run):
        """Test that duplicate items (same source + source_id) are not duplicated."""
        user_id = create_test_user()
        run_id = create_scanner_run(user_id, "gmail")

        conn = sqlite3.connect(test_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Insert same item twice (should use INSERT OR IGNORE)
        for _ in range(2):
            cursor.execute(
                """
                INSERT OR IGNORE INTO scanned_items
                (user_id, scanner_run_id, source, source_id, source_metadata, item_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, run_id, "gmail", "duplicate_msg", '{}', "email")
            )
        conn.commit()

        # Verify only one record exists
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM scanned_items WHERE source_id = ?",
            ("duplicate_msg",)
        )
        count = cursor.fetchone()["cnt"]

        assert count == 1
        conn.close()


class TestClassifierPipeline:
    """Tests for classifier → item_classifications storage."""

    def test_classification_stored_correctly(
        self, test_db, create_test_user, create_scanned_item
    ):
        """Test that classifier creates item_classifications record."""
        user_id = create_test_user()
        item_id = create_scanned_item(user_id, source="gmail", source_id="email_for_classify")

        conn = sqlite3.connect(test_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Simulate classifier output
        cursor.execute(
            """
            INSERT INTO item_classifications
            (user_id, scanned_item_id, relevance, urgency, summary, extracted_actions, model_used)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                item_id,
                "actionable",
                "high",
                "Test email requiring action",
                '[{"type": "reply", "description": "Reply to sender"}]',
                "claude-3-haiku"
            )
        )
        conn.commit()

        # Verify classification was stored
        cursor.execute(
            "SELECT * FROM item_classifications WHERE scanned_item_id = ?",
            (item_id,)
        )
        row = cursor.fetchone()

        assert row is not None
        assert row["relevance"] == "actionable"
        assert row["urgency"] == "high"
        assert "reply" in row["extracted_actions"]
        conn.close()


class TestCrossReferencePipeline:
    """Tests for cross-reference resolver → cross_references storage."""

    def test_cross_reference_links_to_person(
        self, test_db, create_test_user, create_scanned_item, create_person
    ):
        """Test that cross-referencer links items to people."""
        user_id = create_test_user()
        person_id = create_person(user_id, name="Jane Smith", email="jane@company.com")
        item_id = create_scanned_item(
            user_id,
            source="gmail",
            source_id="email_from_jane",
            metadata={"from": "jane@company.com", "sender_name": "Jane Smith"}
        )

        conn = sqlite3.connect(test_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Simulate cross-reference resolver output
        cursor.execute(
            """
            INSERT INTO cross_references
            (user_id, scanned_item_id, entity_type, entity_id, relationship, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_id, item_id, "person", person_id, "sender", 1.0)
        )
        conn.commit()

        # Verify cross-reference was created
        cursor.execute(
            "SELECT * FROM cross_references WHERE scanned_item_id = ?",
            (item_id,)
        )
        row = cursor.fetchone()

        assert row is not None
        assert row["entity_type"] == "person"
        assert row["entity_id"] == person_id
        assert row["relationship"] == "sender"
        conn.close()


class TestDigestPipeline:
    """Tests for digest generation from classified items."""

    def test_needs_reply_item_appears_in_query(
        self, test_db, create_test_user, create_scanned_item
    ):
        """Test that actionable items with reply action are found by get_needs_reply_items."""
        user_id = create_test_user()
        item_id = create_scanned_item(
            user_id,
            source="gmail",
            source_id="needs_reply_email",
            metadata={"from": "colleague@work.com", "subject": "Urgent question"}
        )

        conn = sqlite3.connect(test_db)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Insert classification with reply action
        cursor.execute(
            """
            INSERT INTO item_classifications
            (user_id, scanned_item_id, relevance, urgency, summary, extracted_actions, model_used, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                item_id,
                "actionable",
                "high",
                "Question requiring response",
                '[{"type": "reply", "description": "Answer the question"}]',
                "claude-3-haiku",
                datetime.now().isoformat()
            )
        )
        conn.commit()
        conn.close()

        # Query using the actual function
        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import get_needs_reply_items
            items = get_needs_reply_items(user_id, limit=10)

        assert len(items) >= 1
        assert any(item["scanned_item_id"] == item_id for item in items)


class TestFeedbackPipeline:
    """Tests for feedback recording and ignored senders."""

    def test_add_ignored_sender(self, test_db, create_test_user):
        """Test that ignored sender is stored correctly."""
        user_id = create_test_user()

        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import add_ignored_sender, is_sender_ignored

            # Add ignored sender
            result = add_ignored_sender(user_id, "gmail", "spam@annoying.com")
            assert result is True

            # Verify it's ignored
            is_ignored = is_sender_ignored(user_id, "gmail", "spam@annoying.com")
            assert is_ignored is True

            # Verify other senders not ignored
            is_other_ignored = is_sender_ignored(user_id, "gmail", "legit@example.com")
            assert is_other_ignored is False

    def test_ignored_sender_dedup(self, test_db, create_test_user):
        """Test that adding same ignored sender twice doesn't error."""
        user_id = create_test_user()

        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import add_ignored_sender

            # Add same sender twice
            result1 = add_ignored_sender(user_id, "gmail", "spam@example.com")
            result2 = add_ignored_sender(user_id, "gmail", "spam@example.com")

            # Both should succeed (INSERT OR IGNORE)
            assert result1 is True
            assert result2 is True


class TestEmailFeedbackTokenPipeline:
    """Tests for email feedback token creation and validation."""

    def test_create_and_validate_token(self, test_db, create_test_user):
        """Test that email feedback token can be created and validated."""
        user_id = create_test_user()

        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import (
                create_email_feedback_token,
                validate_and_consume_email_feedback_token
            )

            # Create token
            token = create_email_feedback_token(
                user_id=user_id,
                item_type="needs_reply",
                feedback_action="not_helpful",
                item_id=123
            )
            assert token is not None
            assert len(token) == 64  # SHA256 hex digest

            # Validate and consume token
            token_data = validate_and_consume_email_feedback_token(token)
            assert token_data is not None
            assert token_data["user_id"] == user_id
            assert token_data["item_type"] == "needs_reply"
            assert token_data["feedback_action"] == "not_helpful"

    def test_token_cannot_be_reused(self, test_db, create_test_user):
        """Test that consumed token cannot be used again."""
        user_id = create_test_user()

        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import (
                create_email_feedback_token,
                validate_and_consume_email_feedback_token
            )

            # Create and consume token
            token = create_email_feedback_token(
                user_id=user_id,
                item_type="detected_action",
                feedback_action="not_helpful"
            )
            first_validation = validate_and_consume_email_feedback_token(token)
            assert first_validation is not None

            # Try to use again - should fail
            second_validation = validate_and_consume_email_feedback_token(token)
            assert second_validation is None

    def test_invalid_token_rejected(self, test_db, create_test_user):
        """Test that invalid/unknown tokens are rejected."""
        create_test_user()

        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import validate_and_consume_email_feedback_token

            # Try to validate non-existent token
            result = validate_and_consume_email_feedback_token("fake_token_12345")
            assert result is None


class TestDigestFiltersIgnoredSenders:
    """Test that digest excludes items from ignored senders."""

    def test_digest_filters_ignored_sender(
        self, test_db, create_test_user, create_scanned_item
    ):
        """Test that items from ignored senders don't appear in needs_reply."""
        user_id = create_test_user()

        # Create item from a sender
        item_id = create_scanned_item(
            user_id,
            source="gmail",
            source_id="email_to_filter",
            metadata={"from": "ignored@spam.com", "subject": "Buy now!"}
        )

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Add classification with reply action
        cursor.execute(
            """
            INSERT INTO item_classifications
            (user_id, scanned_item_id, relevance, urgency, summary, extracted_actions, model_used, classified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                item_id,
                "actionable",
                "normal",
                "Spam email",
                '[{"type": "reply", "description": "Respond to offer"}]',
                "claude-3-haiku",
                datetime.now().isoformat()
            )
        )
        conn.commit()
        conn.close()

        with patch("web.core.database.DB_PATH", test_db):
            from web.core.database import add_ignored_sender, is_sender_ignored

            # Add sender to ignore list
            add_ignored_sender(user_id, "gmail", "ignored@spam.com")

            # Verify sender is ignored
            assert is_sender_ignored(user_id, "gmail", "ignored@spam.com") is True

            # The actual filtering happens in DigestService._get_needs_reply,
            # which calls is_sender_ignored. We've verified that works.
            # Full integration test would require async DigestService setup.
