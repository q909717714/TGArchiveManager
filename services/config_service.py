"""Configuration loading for TGArchiveManager."""

from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import yaml


class ConfigError(RuntimeError):
    """Raised when the application configuration cannot be loaded."""


class ConfigService:
    """Load and access YAML configuration.

    The service reads `config/config.yaml` when present and falls back to
    `config/config.yaml.example` so a fresh checkout can start without secrets.
    """

    def __init__(self, project_root: Path):
        self._project_root = Path(project_root)
        self._config_dir = self._project_root / "config"
        self._user_config_path = self._config_dir / "config.yaml"
        self._example_config_path = self._config_dir / "config.yaml.example"
        self._config: Dict[str, Any] = {}

    @property
    def config_path(self) -> Path:
        if self._user_config_path.exists():
            return self._user_config_path
        return self._example_config_path

    def load(self) -> Dict[str, Any]:
        """Load configuration from disk and ensure writable app directories."""
        self._restore_example_config_if_available()
        path = self.config_path
        if not path.exists():
            raise ConfigError("Missing config/config.yaml.example")

        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        if not isinstance(data, dict):
            raise ConfigError("Configuration root must be a YAML mapping")

        self._config = data
        self._ensure_runtime_directories()
        return self.as_dict()

    def as_dict(self) -> Dict[str, Any]:
        """Return a defensive copy of the loaded configuration."""
        return copy.deepcopy(self._config)

    def save_telegram_api_credentials(self, api_id: str, api_hash: str) -> None:
        """Persist Telegram API credentials to `config/config.yaml`.

        The example configuration remains unchanged, so real credentials are
        stored only in the user-local configuration file ignored by git.
        """
        clean_api_id = str(api_id).strip()
        clean_api_hash = str(api_hash).strip()
        if not clean_api_id:
            raise ConfigError("api_id cannot be empty")
        if not clean_api_id.isdigit():
            raise ConfigError("api_id must be numeric")
        if not clean_api_hash:
            raise ConfigError("api_hash cannot be empty")

        config = self.as_dict()
        telegram_config = config.setdefault("telegram", {})
        if not isinstance(telegram_config, dict):
            raise ConfigError("Configuration key 'telegram' must be a mapping")

        telegram_config["api_id"] = clean_api_id
        telegram_config["api_hash"] = clean_api_hash

        self._save_user_config(config)
        self._config = config

    def save_config(self, config: Dict[str, Any]) -> None:
        """Persist a complete application configuration to `config/config.yaml`.

        Callers should start from :meth:`as_dict`, change only supported keys,
        and pass the resulting mapping back here. The service writes the user
        config file and refreshes runtime directories that depend on paths.
        """
        if not isinstance(config, dict):
            raise ConfigError("Configuration root must be a YAML mapping")

        stored_config = copy.deepcopy(config)
        self._save_user_config(stored_config)
        self._config = stored_config
        self._ensure_runtime_directories()

    def get(self, dotted_key: str, default: Optional[Any] = None) -> Any:
        """Return a nested setting using a dotted path such as `logs.level`."""
        current: Any = self._config
        for part in dotted_key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def resolve_path(self, dotted_key: str, default: str) -> Path:
        """Resolve a configured relative path against the project root."""
        value = self.get(dotted_key, default)
        path = Path(str(value))
        if path.is_absolute():
            return path
        return self._project_root / path

    def _ensure_runtime_directories(self) -> None:
        directories = [
            self._project_root / "config",
            self.resolve_path("telegram.session_dir", "sessions"),
            self.resolve_path("database.path", "data/tg_archive.db").parent,
            self.resolve_path("logs.root_dir", "logs"),
            self.resolve_path("logs.root_dir", "logs") / "tasks",
            self.resolve_path("download.root_dir", "downloads"),
            self.resolve_path("export.root_dir", "exports"),
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    def _save_user_config(self, config: Dict[str, Any]) -> None:
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with self._user_config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)

    def _restore_example_config_if_available(self) -> None:
        if self._example_config_path.exists():
            return

        bundled_value = getattr(sys, "_MEIPASS", None)
        if not bundled_value:
            return

        bundled_root = Path(str(bundled_value))
        bundled_example = bundled_root / "config" / "config.yaml.example"
        if not bundled_example.exists():
            return

        self._config_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundled_example, self._example_config_path)
