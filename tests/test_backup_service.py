import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backup_service import create_backup, list_backups, restore_backup, verify_backup


class BackupServiceTests(unittest.TestCase):
    def test_atomic_backup_verify_and_restore_to_empty_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            database = data / "studio.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE proof(id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO proof(value) VALUES ('ok')")
            (data / "state.json").write_text('{"images":[]}', encoding="utf-8")
            generated = data / "generated"
            generated.mkdir()
            (generated / "asset.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8"
            )

            backup_root = data / "backups"
            backup = create_backup(database, data, backup_root)
            self.assertTrue(backup["valid"])
            self.assertEqual(backup["database_integrity"], "ok")
            self.assertEqual(len(list_backups(backup_root)), 1)
            verified = verify_backup(Path(backup["path"]))
            self.assertTrue(verified["valid"])

            restored_root = root / "restored"
            restored = restore_backup(Path(backup["path"]), restored_root)
            self.assertTrue(restored["valid"])
            self.assertTrue((restored_root / "generated" / "asset.svg").is_file())
            with sqlite3.connect(restored_root / "studio.db") as connection:
                value = connection.execute("SELECT value FROM proof").fetchone()[0]
            self.assertEqual(value, "ok")

            manifest = json.loads(
                (Path(backup["path"]) / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(manifest["file_count"], 3)

    def test_restore_refuses_to_overwrite_existing_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            database = data / "studio.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE proof(id INTEGER PRIMARY KEY)")
            backup = create_backup(database, data, data / "backups")
            target = root / "target"
            target.mkdir()
            (target / "keep.txt").write_text("do not overwrite", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                restore_backup(Path(backup["path"]), target)
            self.assertEqual((target / "keep.txt").read_text(), "do not overwrite")


if __name__ == "__main__":
    unittest.main()
