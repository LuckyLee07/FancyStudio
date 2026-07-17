#!/usr/bin/env python3
"""Safe command-line backup, verification, and restore utility."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backup_service import create_backup, list_backups, restore_backup, verify_backup


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("TANG_STUDIO_DATA_DIR", ROOT / "data")).resolve()
BACKUP_ROOT = DATA_DIR / "backups"


def main() -> None:
    parser = argparse.ArgumentParser(description="唐诗插图 SOP 数据备份工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("create", help="创建并立即校验新备份")
    subparsers.add_parser("list", help="列出现有备份")
    verify_parser = subparsers.add_parser("verify", help="重新校验指定备份")
    verify_parser.add_argument("name")
    restore_parser = subparsers.add_parser(
        "restore", help="恢复到一个新的空目录，绝不覆盖现有数据"
    )
    restore_parser.add_argument("name")
    restore_parser.add_argument("target")
    args = parser.parse_args()

    if args.command == "create":
        result = create_backup(DATA_DIR / "studio.db", DATA_DIR, BACKUP_ROOT)
    elif args.command == "list":
        result = {"items": list_backups(BACKUP_ROOT)}
    elif args.command == "verify":
        result = verify_backup(BACKUP_ROOT / args.name)
    else:
        result = restore_backup(BACKUP_ROOT / args.name, Path(args.target))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
