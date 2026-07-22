"""SQLite initialization for TGArchiveManager."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        display_name TEXT,
        username TEXT,
        session_path TEXT,
        last_login_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "DROP INDEX IF EXISTS idx_accounts_phone",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_phone ON accounts(phone)",
    """
    CREATE TABLE IF NOT EXISTS chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_chat_id INTEGER UNIQUE,
        title TEXT NOT NULL,
        username TEXT,
        type TEXT,
        tag TEXT,
        telegram_folder_names TEXT,
        is_created_by_tool INTEGER DEFAULT 0,
        last_message_id INTEGER,
        last_backup_message_id INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public_search_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        engines TEXT,
        max_results INTEGER DEFAULT 100,
        status TEXT NOT NULL,
        total_found INTEGER DEFAULT 0,
        total_saved INTEGER DEFAULT 0,
        success_count INTEGER DEFAULT 0,
        failed_count INTEGER DEFAULT 0,
        skipped_count INTEGER DEFAULT 0,
        log_file TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS public_search_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        engine_name TEXT,
        rank_no INTEGER,
        keyword TEXT,
        result_type TEXT,
        title TEXT,
        summary TEXT,
        url TEXT,
        normalized_url TEXT,
        tg_username TEXT,
        tg_message_id INTEGER,
        tg_chat_id INTEGER,
        is_duplicate INTEGER DEFAULT 0,
        is_accessible INTEGER,
        is_protected INTEGER DEFAULT 0,
        can_forward INTEGER,
        forward_status TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(task_id) REFERENCES public_search_tasks(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_public_search_results_task_id ON public_search_results(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_public_search_results_normalized_url ON public_search_results(normalized_url)",
    """
    CREATE TABLE IF NOT EXISTS telegraph_pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        search_result_id INTEGER UNIQUE,
        message_db_id INTEGER,
        url TEXT,
        normalized_url TEXT,
        title TEXT,
        published_at TEXT,
        author_name TEXT,
        author_url TEXT,
        image_count INTEGER DEFAULT 0,
        telegram_link_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(search_result_id) REFERENCES public_search_results(id),
        FOREIGN KEY(message_db_id) REFERENCES messages(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_telegraph_pages_search_result_id ON telegraph_pages(search_result_id)",
    "CREATE INDEX IF NOT EXISTS idx_telegraph_pages_normalized_url ON telegraph_pages(normalized_url)",
    """
    CREATE TABLE IF NOT EXISTS telegraph_page_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id INTEGER NOT NULL,
        position INTEGER,
        url TEXT,
        normalized_url TEXT,
        local_path TEXT,
        download_status TEXT,
        error_message TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(page_id) REFERENCES telegraph_pages(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_telegraph_page_images_page_id ON telegraph_page_images(page_id)",
    """
    CREATE TABLE IF NOT EXISTS telegraph_page_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        page_id INTEGER NOT NULL,
        position INTEGER,
        url TEXT,
        normalized_url TEXT,
        link_type TEXT,
        text TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(page_id) REFERENCES telegraph_pages(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_telegraph_page_links_page_id ON telegraph_page_links(page_id)",
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_chat_id INTEGER,
        message_id INTEGER,
        sender_id INTEGER,
        sender_name TEXT,
        date TEXT,
        text TEXT,
        text_preview TEXT,
        message_type TEXT,
        has_media INTEGER DEFAULT 0,
        media_type TEXT,
        media_id TEXT,
        file_name TEXT,
        file_size INTEGER,
        local_path TEXT,
        is_downloaded INTEGER DEFAULT 0,
        is_protected INTEGER DEFAULT 0,
        is_forwarded INTEGER DEFAULT 0,
        source_link TEXT,
        external_urls TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(tg_chat_id, message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_db_id INTEGER,
        tg_chat_id INTEGER,
        message_id INTEGER,
        file_name TEXT,
        file_ext TEXT,
        file_size INTEGER,
        local_path TEXT,
        file_hash TEXT,
        download_status TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(message_db_id) REFERENCES messages(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT,
        source_id INTEGER,
        url TEXT,
        normalized_url TEXT,
        domain TEXT,
        link_type TEXT,
        tag TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_group_id INTEGER UNIQUE,
        title TEXT NOT NULL,
        category TEXT,
        name_rule TEXT,
        is_created_by_tool INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT UNIQUE,
        task_type TEXT,
        title TEXT,
        status TEXT,
        source_config_json TEXT,
        target_config_json TEXT,
        total_count INTEGER DEFAULT 0,
        success_count INTEGER DEFAULT 0,
        failed_count INTEGER DEFAULT 0,
        skipped_count INTEGER DEFAULT 0,
        progress INTEGER DEFAULT 0,
        log_file TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        started_at TEXT,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forward_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        source_type TEXT,
        source_id INTEGER,
        source_chat_id INTEGER,
        source_message_id INTEGER,
        target_chat_id INTEGER,
        target_message_id INTEGER,
        forward_mode TEXT,
        status TEXT,
        reason TEXT,
        error_code TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS download_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT,
        message_db_id INTEGER,
        file_id INTEGER,
        status TEXT,
        local_path TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "INSERT OR IGNORE INTO schema_version(version) VALUES (1)",
]


class ClosingConnection(sqlite3.Connection):
    """SQLite connection that closes when leaving a context manager."""

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        self.close()
        return result


class DatabaseManager:
    """Manage SQLite connection setup and schema initialization."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: logging.Logger):
        self._project_root = Path(project_root)
        self._config = config
        self._logger = logger
        self._db_path = self._resolve_database_path()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def initialize(self) -> None:
        """Create the SQLite database and all stage-1 baseline tables."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            self._ensure_compatible_schema(connection)
            connection.commit()
        self._logger.info("SQLite database initialized at %s", self._db_path)

    def connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with row access by column name."""
        connection = sqlite3.connect(str(self._db_path), factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    def table_names(self) -> Iterable[str]:
        """Return database table names, mainly for diagnostics and tests."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [row["name"] for row in rows]

    def _resolve_database_path(self) -> Path:
        value = self._config.get("database", {}).get("path", "data/tg_archive.db")
        path = Path(str(value))
        if path.is_absolute():
            return path
        return self._project_root / path

    def _ensure_compatible_schema(self, connection: sqlite3.Connection) -> None:
        """Apply lightweight additive migrations for existing local databases."""
        self._ensure_columns(
            connection,
            "chats",
            {
                "telegram_folder_names": "TEXT",
            },
        )
        self._ensure_columns(
            connection,
            "messages",
            {
                "external_urls": "TEXT",
            },
        )

    @staticmethod
    def _ensure_columns(connection: sqlite3.Connection, table_name: str, columns: Dict[str, str]) -> None:
        existing_columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        for column_name, definition in columns.items():
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
