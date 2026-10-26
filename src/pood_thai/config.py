"""Settings read from environment variables (start.sh loads .env)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    azure_key: str
    azure_region: str
    claude_model: str
    whisper_model: str
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "pood-thai.sqlite3"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            azure_key=env.get("AZURE_SPEECH_KEY", ""),
            azure_region=env.get("AZURE_SPEECH_REGION", "southeastasia"),
            claude_model=env.get("POOD_CLAUDE_MODEL", "sonnet"),
            whisper_model=env.get("POOD_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
            data_dir=Path(env.get("POOD_DATA_DIR", "./data")),
        )
