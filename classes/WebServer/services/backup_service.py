"""services/backup_service.py — Versioned config.cfg backups and rollback."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import ConfigBackup


def list_backups(db: Session) -> list[ConfigBackup]:
    return (
        db.query(ConfigBackup)
        .order_by(ConfigBackup.created_at.desc())
        .limit(50)
        .all()
    )


def rollback_to(db: Session, backup_id: int, config_path: Path) -> bool:
    """
    Restore config.cfg from a named backup.
    Creates a backup of the current state before overwriting.
    Returns True on success.
    """
    record: ConfigBackup | None = db.get(ConfigBackup, backup_id)
    if not record:
        return False

    backup_path = Path(record.filepath)
    if not backup_path.exists():
        return False

    # Archive current state first
    ts: str = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    pre_rollback: Path = config_path.parent / "backups" / f"pre_rollback_{ts}.cfg"
    pre_rollback.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, pre_rollback)

    # Restore
    shutil.copy2(backup_path, config_path)

    pre_record = ConfigBackup(
        filepath=str(pre_rollback),
        file_size_bytes=pre_rollback.stat().st_size,
        trigger="auto",
        notes=f"Auto-backup before rollback to backup #{backup_id}",
    )
    db.add(pre_record)
    db.commit()
    return True
