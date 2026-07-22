"""Release preflight checks for TGArchiveManager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


def _bootstrap_imports(root: Path) -> None:
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def _touch_probe(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def run(root: Path) -> int:
    root = root.resolve()
    _bootstrap_imports(root)

    try:
        from database.db import DatabaseManager
        from services.config_service import ConfigService
        from services.log_service import LogService
    except ModuleNotFoundError:
        return run_release_root_check(root)

    config_service = ConfigService(root)
    config = config_service.load()
    log_service = LogService(root, config)
    log_service.configure()

    database = DatabaseManager(root, config, log_service.get_logger("database"))
    database.initialize()

    required_dirs = [
        root / "config",
        config_service.resolve_path("telegram.session_dir", "sessions"),
        config_service.resolve_path("logs.root_dir", "logs"),
        config_service.resolve_path("logs.root_dir", "logs") / "tasks",
        config_service.resolve_path("download.root_dir", "downloads"),
        config_service.resolve_path("export.root_dir", "exports"),
        config_service.resolve_path("database.path", "data/tg_archive.db").parent,
    ]

    for directory in required_dirs:
        _touch_probe(directory)
        print(f"writable: {directory}")

    print(f"database: {database.db_path}")
    print("preflight-ok")
    return 0


def run_release_root_check(root: Path) -> int:
    """Check a built release directory where source modules are not importable."""
    config_path = root / "config" / "config.yaml"
    example_path = root / "config" / "config.yaml.example"
    readable_config = config_path if config_path.exists() else example_path
    if not readable_config.exists():
        raise RuntimeError("Missing config/config.yaml.example in release root")

    with readable_config.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    if not isinstance(config, dict):
        raise RuntimeError("Configuration root must be a YAML mapping")

    def resolve(section: str, key: str, default: str) -> Path:
        section_value = config.get(section, {})
        value = default
        if isinstance(section_value, dict):
            value = str(section_value.get(key, default) or default)
        path = Path(value)
        if path.is_absolute():
            return path
        return root / path

    required_dirs = [
        root / "config",
        resolve("telegram", "session_dir", "sessions"),
        resolve("logs", "root_dir", "logs"),
        resolve("logs", "root_dir", "logs") / "tasks",
        resolve("download", "root_dir", "downloads"),
        resolve("export", "root_dir", "exports"),
        resolve("database", "path", "data/tg_archive.db").parent,
    ]

    for directory in required_dirs:
        _touch_probe(directory)
        print(f"writable: {directory}")

    print(f"config: {readable_config}")
    print("preflight-ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TGArchiveManager release preflight check")
    parser.add_argument("--root", default=".", help="Application root or built dist directory")
    args = parser.parse_args()

    try:
        return run(Path(args.root))
    except Exception as exc:
        print(f"preflight-failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
