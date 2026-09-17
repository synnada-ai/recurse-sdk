"""Run-local authority for frozen contracts, budgets, and complete trial history."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import ModelerError

__all__ = ["get", "put", "transaction", "trials"]


@contextmanager
def transaction(root: Path) -> Iterator[sqlite3.Connection]:
    """Serialize state changes and release the database on every exit path."""
    root.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(root / "state.sqlite", timeout=660)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS trials (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def get(connection: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Read recorded state without exposing the database to agent-authored queries."""
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(connection: sqlite3.Connection, key: str, value: Any) -> None:
    """Persist finite JSON state under one authority-owned key."""
    connection.execute(
        "INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(value, allow_nan=False))
    )


def trials(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every reserved experiment, including failed and unscored candidates."""
    return [
        json.loads(row[0]) for row in connection.execute("SELECT value FROM trials ORDER BY id")
    ]


def require(connection: sqlite3.Connection, key: str) -> Any:
    """Require a completed prerequisite and prevent work after finalization."""
    if get(connection, "receipt"):
        raise ModelerError("This run is finalized; start a new run to change the experiment.")
    value = get(connection, key)
    if value is None:
        raise ModelerError(f"Complete {key} before this action.")
    return value
