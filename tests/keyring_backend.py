"""Process-shared keyring double containing only synthetic test credentials."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from keyring.backend import KeyringBackend


class Backend(KeyringBackend):
    """Persist fake keyring values across subprocesses in an isolated test database."""

    priority = 1

    def get_password(self, service: str, username: str) -> str | None:
        """Read one synthetic credential."""
        with closing(sqlite3.connect(os.environ["RECURSE_TEST_KEYRING"])) as db, db:
            row = db.execute(
                "SELECT value FROM credentials WHERE service = ? AND username = ?",
                (service, username),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def set_password(self, service: str, username: str, password: str) -> None:
        """Write one synthetic credential atomically."""
        with closing(sqlite3.connect(os.environ["RECURSE_TEST_KEYRING"])) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO credentials VALUES (?, ?, ?)", (service, username, password)
            )

    def delete_password(self, service: str, username: str) -> None:
        """Remove one synthetic credential."""
        with closing(sqlite3.connect(os.environ["RECURSE_TEST_KEYRING"])) as db, db:
            db.execute(
                "DELETE FROM credentials WHERE service = ? AND username = ?", (service, username)
            )


def process_environment(tmp_path: Path, url: str) -> dict[str, str]:
    """Create a private test home and shared fake keychain for CLI subprocesses."""
    database = tmp_path / "keyring.sqlite"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "CREATE TABLE credentials (service, username, value, PRIMARY KEY (service, username))"
        )
        db.execute(
            "INSERT INTO credentials VALUES (?, ?, ?)",
            ("recurse-cli", f"device-credential:{url}", "device-1"),
        )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "RECURSE_API_URL": url,
            "RECURSE_TEST_KEYRING": str(database),
            "PYTHON_KEYRING_BACKEND": "tests.keyring_backend.Backend",
            "PYTHONPATH": os.pathsep.join([str(Path.cwd()), str(Path.cwd() / "src")]),
        }
    )
    return env
