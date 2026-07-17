"""Atomic local backup and verification helpers for production data."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKUP_SCHEMA_VERSION = "1.0"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_integrity(path: Path) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def create_backup(
    database_path: Path,
    data_root: Path,
    backup_root: Path,
) -> dict[str, Any]:
    database_path = Path(database_path).resolve()
    data_root = Path(data_root).resolve()
    backup_root = Path(backup_root).resolve()
    if not database_path.is_file():
        raise FileNotFoundError("生产数据库不存在。")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"backup-{timestamp}-{uuid.uuid4().hex[:8]}"
    final_dir = backup_root / name
    temp_dir = backup_root / f".{name}.tmp"
    try:
        (temp_dir / "files").mkdir(parents=True, exist_ok=False)
        destination_database = temp_dir / "studio.db"
        source_connection = sqlite3.connect(database_path)
        destination_connection = sqlite3.connect(destination_database)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
        for filename in ("state.json", "poems.json", "styles.json"):
            source = data_root / filename
            if source.is_file():
                shutil.copy2(source, temp_dir / "files" / filename)
        for dirname in ("generated", "exports"):
            source = data_root / dirname
            if source.is_dir():
                shutil.copytree(source, temp_dir / "files" / dirname)
        entries = []
        for path in sorted(item for item in temp_dir.rglob("*") if item.is_file()):
            if path.name == "manifest.json":
                continue
            entries.append(
                {
                    "path": str(path.relative_to(temp_dir)),
                    "size": path.stat().st_size,
                    "checksum_sha256": _sha256(path),
                }
            )
        integrity = _database_integrity(destination_database)
        if integrity != "ok":
            raise RuntimeError(f"数据库备份完整性检查失败：{integrity}")
        manifest = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_database": str(database_path),
            "database_integrity": integrity,
            "file_count": len(entries),
            "files": entries,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        backup_root.mkdir(parents=True, exist_ok=True)
        if final_dir.exists():
            raise FileExistsError("备份目录已存在，禁止覆盖。")
        os.replace(temp_dir, final_dir)
        return verify_backup(final_dir)
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    backup_dir = Path(backup_dir).resolve()
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("备份 Manifest 不存在。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    checked_bytes = 0
    for entry in manifest.get("files", []):
        relative = Path(str(entry.get("path") or ""))
        path = (backup_dir / relative).resolve()
        try:
            path.relative_to(backup_dir)
        except ValueError:
            errors.append({"path": str(relative), "error": "path_escape"})
            continue
        if not path.is_file():
            errors.append({"path": str(relative), "error": "missing"})
            continue
        checked_bytes += path.stat().st_size
        if path.stat().st_size != int(entry.get("size") or -1):
            errors.append({"path": str(relative), "error": "size_mismatch"})
            continue
        if _sha256(path) != entry.get("checksum_sha256"):
            errors.append({"path": str(relative), "error": "checksum_mismatch"})
    database_path = backup_dir / "studio.db"
    database_integrity = (
        _database_integrity(database_path) if database_path.is_file() else "missing"
    )
    if database_integrity != "ok":
        errors.append({"path": "studio.db", "error": database_integrity})
    return {
        "name": manifest.get("name", backup_dir.name),
        "path": str(backup_dir),
        "created_at": manifest.get("created_at"),
        "file_count": len(manifest.get("files", [])),
        "checked_bytes": checked_bytes,
        "database_integrity": database_integrity,
        "valid": not errors,
        "errors": errors,
    }


def restore_backup(backup_dir: Path, target_data_root: Path) -> dict[str, Any]:
    """Restore into a new, empty directory; existing data is never overwritten."""

    verification = verify_backup(backup_dir)
    if not verification["valid"]:
        raise RuntimeError("备份校验未通过，禁止恢复。")
    backup_dir = Path(backup_dir).resolve()
    target_data_root = Path(target_data_root).resolve()
    if target_data_root.exists() and any(target_data_root.iterdir()):
        raise FileExistsError("恢复目标目录必须为空，禁止覆盖现有生产数据。")
    target_data_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_dir / "studio.db", target_data_root / "studio.db")
    files_root = backup_dir / "files"
    if files_root.is_dir():
        for source in files_root.iterdir():
            target = target_data_root / source.name
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
    restored_integrity = _database_integrity(target_data_root / "studio.db")
    return {
        "target": str(target_data_root),
        "database_integrity": restored_integrity,
        "valid": restored_integrity == "ok",
    }


def list_backups(backup_root: Path) -> list[dict[str, Any]]:
    backup_root = Path(backup_root).resolve()
    if not backup_root.is_dir():
        return []
    result = []
    for path in sorted(backup_root.glob("backup-*"), reverse=True):
        if not path.is_dir():
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            result.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "created_at": manifest.get("created_at"),
                    "file_count": manifest.get("file_count", 0),
                }
            )
        except (OSError, json.JSONDecodeError):
            result.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "created_at": None,
                    "file_count": 0,
                    "invalid_manifest": True,
                }
            )
    return result
