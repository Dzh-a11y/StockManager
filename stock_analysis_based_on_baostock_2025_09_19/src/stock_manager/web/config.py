"""Controlled startup configuration for the local Web layer (P3)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WebConfig:
    """Bound the Web layer to caller-provided, validated file locations.

    The database path, template roots and static root are all supplied by the
    operator at startup; the browser can never choose these paths.
    """

    database_path: Path
    system_template_root: Path
    user_template_root: Path
    static_root: Path
    host: str = "127.0.0.1"
    port: int = 8000
    max_request_bytes: int = 1_000_000
    sync_config_path: Path | None = None
    lock_directory: Path | None = None

    def validate(self) -> None:
        """Verify startup prerequisites; do not create a missing database."""
        if not self.database_path.is_file():
            raise ValueError(
                f"local SQLite database does not exist: {self.database_path.name}"
            )
        if not self.static_root.is_dir():
            raise ValueError("static root must be an existing directory")
        for root in (self.system_template_root, self.user_template_root):
            if root.exists() and root.is_symlink():
                raise ValueError("template root must not be a symbolic link")
        if self.sync_config_path is not None and not self.sync_config_path.is_file():
            raise ValueError("sync config path must be an existing file")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be positive")
