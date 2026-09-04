from __future__ import annotations

import sqlite3
import tarfile
from datetime import UTC, datetime

from app.config import load_settings


def main() -> None:
    settings = load_settings(require_platform=False)
    source_path = settings.database_path
    backup_dir = source_path.parent.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target_path = backup_dir / f"bot-{timestamp}.sqlite3"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    media_dir = source_path.parent / "cms-media"
    if media_dir.is_dir():
        media_backup = backup_dir / f"cms-media-{timestamp}.tar.gz"
        with tarfile.open(media_backup, "w:gz") as archive:
            archive.add(media_dir, arcname="cms-media")
    print(target_path)


if __name__ == "__main__":
    main()
