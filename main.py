"""Application entry point for TGArchiveManager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from database.db import DatabaseManager
from services.config_service import ConfigService
from services.log_service import LogService


def project_root() -> Path:
    """Return the absolute project root path."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def initialize_core(root: Path):
    """Initialize configuration, logging, and the SQLite database."""
    config_service = ConfigService(root)
    config = config_service.load()

    log_service = LogService(root, config)
    logger = log_service.configure()

    database = DatabaseManager(root, config, log_service.get_logger("database"))
    database.initialize()

    logger.info("TGArchiveManager started")
    return config_service, log_service, database


def run_gui(root: Path, config_service: ConfigService, log_service: LogService, database: DatabaseManager) -> int:
    """Start the PySide6 GUI."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print("PySide6 is not installed. Install dependencies with: pip install -r requirements.txt")
        print(str(exc))
        return 1

    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("TGArchiveManager")
    app.setOrganizationName("TGArchiveManager")

    window = MainWindow(root, config_service, log_service, database)
    window.show()

    return app.exec()


def check_gui(root: Path, config_service: ConfigService, log_service: LogService, database: DatabaseManager) -> int:
    """Instantiate the main window without entering the GUI event loop."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        print("PySide6 is not installed. Install dependencies with: pip install -r requirements.txt")
        print(str(exc))
        return 1

    from ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow(root, config_service, log_service, database)
    print(f"GUI check ok: {window.windowTitle()} {window.size().width()}x{window.size().height()}")
    window.deleteLater() 
    app.processEvents()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TGArchiveManager")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Initialize config, logging, and database without starting the GUI.",
    )
    parser.add_argument(
        "--check-gui",
        action="store_true",
        help="Initialize config, logging, database, and main window without starting the event loop.",
    )
    args = parser.parse_args()

    root = project_root()
    config_service, log_service, database = initialize_core(root)

    if args.check:
        return 0
    if args.check_gui:
        return check_gui(root, config_service, log_service, database)

    return run_gui(root, config_service, log_service, database)


if __name__ == "__main__":
    raise SystemExit(main())
