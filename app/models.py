"""Data model representing a single tracked game."""

from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
import uuid


@dataclass
class Game:
    id: str
    name: str
    save_path: str          # folder where the game writes its save files
    backup_root: str        # folder the user wants backups copied into
    last_backup_time: Optional[str] = None
    last_snapshot: Optional[Dict[str, List[float]]] = None  # {relpath: [size, mtime_ns]}
    backup_count: int = 0
    color: Optional[str] = None  # user-assigned tag color, e.g. "#7c5cff"; None = auto

    @staticmethod
    def new(name: str, save_path: str, backup_root: str) -> "Game":
        return Game(
            id=str(uuid.uuid4()),
            name=name,
            save_path=save_path,
            backup_root=backup_root,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Game":
        return Game(
            id=d.get("id") or str(uuid.uuid4()),
            name=d.get("name", "Unknown Game"),
            save_path=d.get("save_path", ""),
            backup_root=d.get("backup_root", ""),
            last_backup_time=d.get("last_backup_time"),
            last_snapshot=d.get("last_snapshot"),
            backup_count=d.get("backup_count", 0),
            color=d.get("color"),
        )
