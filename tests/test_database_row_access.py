import sqlite3
from contextlib import contextmanager

import pytest

from web.core import database


def test_get_first_count_from_row_tuple_row():
    assert database._get_first_count_from_row((42,)) == 42


def test_get_first_count_from_row_dict_row():
    assert database._get_first_count_from_row({"count": 7}) == 7
    assert database._get_first_count_from_row({"total": 8}) == 8
    assert database._get_first_count_from_row({"other": 9}) == 9


def test_get_first_count_from_row_sqlite_row():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT 123 AS count")
    row = cursor.fetchone()
    assert database._get_first_count_from_row(row) == 123
    conn.close()


@pytest.mark.parametrize(
    "row,expected",
    [
        ((5,), 5),
        ({"count": 5}, 5),
        ({"total": 6}, 6),
    ],
)
def test_count_pending_actions_handles_generic_row_access(monkeypatch, row, expected):
    class FakeCursor:
        def __init__(self, value):
            self._value = value

        def execute(self, query, params):
            pass

        def fetchone(self):
            return self._value

    class FakeConn:
        def __init__(self, cursor):
            self._cursor = cursor

        def cursor(self):
            return self._cursor

    @contextmanager
    def fake_get_db():
        yield FakeConn(FakeCursor(row))

    monkeypatch.setattr(database, "get_db", fake_get_db)

    assert database.count_pending_actions(user_id=1) == expected
