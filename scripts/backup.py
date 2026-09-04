from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.config import load_settings


def main() -> None:
    settings = load_settings(require_platform=False)
    source_path = settings.database_path
    backup_dir = source_path.parent.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target_path = backup_dir / f"bot-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.sqlite3"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    print(target_path)


if __name__ == "__main__":
    main()
