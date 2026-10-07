from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from personal_vault import database as database_module
from personal_vault.database import (
    SCHEMA_SHA256,
    SCHEMA_VERSION,
    DatabaseSchemaError,
    connect_database,
)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "nested" / "archive.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _set_database_version(
        path: Path,
        version: int,
        *,
        create_user_table: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        database = sqlite3.connect(path)
        try:
            if create_user_table:
                database.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
                database.execute("INSERT INTO sentinel VALUES ('preserved')")
            database.execute(f"PRAGMA user_version = {version}")
            database.commit()
        finally:
            database.close()

    def test_schema_metadata_is_public_and_bound_to_packaged_sql(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(
            SCHEMA_SHA256,
            hashlib.sha256(database_module.SCHEMA_PATH.read_bytes()).hexdigest(),
        )

    def test_new_database_is_initialized_at_schema_version_two(self) -> None:
        database = connect_database(self.database_path)
        try:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIs(database.row_factory, sqlite3.Row)
            self.assertIsNotNone(
                database.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE type = 'table' AND name = 'sources'"
                ).fetchone()
            )
        finally:
            database.close()

    def test_import_runs_enforce_exactly_one_row_per_snapshot_key(self) -> None:
        database = connect_database(self.database_path)
        try:
            unique_indexes = []
            for index in database.execute("PRAGMA index_list(import_runs)"):
                if int(index[2]) != 1:
                    continue
                columns = tuple(
                    row[2]
                    for row in database.execute(
                        f"PRAGMA index_info({index[1]!r})"
                    )
                )
                unique_indexes.append(columns)
            self.assertIn(("snapshot_id",), unique_indexes)
        finally:
            database.close()

    def test_existing_empty_version_zero_database_is_initialized(self) -> None:
        self._set_database_version(self.database_path, 0)

        database = connect_database(self.database_path)
        try:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNotNone(
                database.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE type = 'table' AND name = 'sources'"
                ).fetchone()
            )
        finally:
            database.close()

    def test_existing_version_two_database_is_not_reinitialized(self) -> None:
        self._set_database_version(
            self.database_path,
            2,
            create_user_table=True,
        )

        database = connect_database(self.database_path)
        try:
            tables = {
                row[0]
                for row in database.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(tables, {"sentinel"})
            self.assertEqual(
                database.execute("SELECT value FROM sentinel").fetchone()[0],
                "preserved",
            )
        finally:
            database.close()

    def test_lower_schema_version_is_rejected_even_when_empty(self) -> None:
        self._set_database_version(self.database_path, 1)

        with self.assertRaisesRegex(
            DatabaseSchemaError,
            r"schema version 1.*expected 2",
        ):
            connect_database(self.database_path)

    def test_higher_schema_version_is_rejected(self) -> None:
        self._set_database_version(
            self.database_path,
            3,
            create_user_table=True,
        )

        with self.assertRaisesRegex(
            DatabaseSchemaError,
            r"schema version 3.*expected 2",
        ):
            connect_database(self.database_path)

        check = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(
                check.execute("SELECT value FROM sentinel").fetchone()[0],
                "preserved",
            )
        finally:
            check.close()

    def test_version_zero_database_with_user_table_is_rejected_unchanged(self) -> None:
        self._set_database_version(
            self.database_path,
            0,
            create_user_table=True,
        )

        with self.assertRaisesRegex(
            DatabaseSchemaError,
            r"schema version 0.*non-empty",
        ):
            connect_database(self.database_path)

        check = sqlite3.connect(self.database_path)
        try:
            tables = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(tables, {"sentinel"})
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 0)
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
