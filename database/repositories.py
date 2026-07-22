"""Repository classes for SQLite persistence."""

from __future__ import annotations

import json
from typing import List, Optional, Sequence

from database.db import DatabaseManager
from database.models import (
    Account,
    Chat,
    DownloadRecord,
    FileRecord,
    ForwardRecord,
    MessageRecord,
    PublicSearchTask,
    SearchResult,
    TaskSummary,
    TelegraphImage,
    TelegraphLink,
    TelegraphPage,
)


def _unique_positive_ints(values: Sequence[int]) -> List[int]:
    """Return positive integer ids without duplicates, preserving order."""
    ids: List[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id <= 0 or item_id in seen:
            continue
        ids.append(item_id)
        seen.add(item_id)
    return ids


def _unique_ints(values: Sequence[int]) -> List[int]:
    """Return integer values without duplicates, preserving order."""
    ids: List[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            item_id = int(value)
        except (TypeError, ValueError):
            continue
        if item_id in seen:
            continue
        ids.append(item_id)
        seen.add(item_id)
    return ids


def _unique_message_keys(values: Sequence[tuple[int, int]]) -> List[tuple[int, int]]:
    """Return unique Telegram message identities, preserving order."""
    keys: List[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for value in values:
        try:
            tg_chat_id, message_id = value
            key = (int(tg_chat_id), int(message_id))
        except (TypeError, ValueError):
            continue
        if key in seen:
            continue
        keys.append(key)
        seen.add(key)
    return keys


def _sql_placeholders(values: Sequence[object]) -> str:
    """Build a placeholder list for a trusted sequence length."""
    return ",".join("?" for _ in values)


class AccountRepository:
    """Persist Telegram account metadata through the database layer."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def upsert_account(self, phone: str, display_name: str, username: str, session_path: str) -> Account:
        """Insert or update the current Telegram account by phone number."""
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts(phone, display_name, username, session_path, last_login_at, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(phone) DO UPDATE SET
                    display_name = excluded.display_name,
                    username = excluded.username,
                    session_path = excluded.session_path,
                    last_login_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (phone, display_name, username, session_path),
            )
            connection.commit()

        account = self.get_by_phone(phone)
        if account is None:
            raise RuntimeError("Account upsert completed without a readable account row")
        return account

    def get_by_phone(self, phone: str) -> Optional[Account]:
        """Return an account by phone number."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, phone, display_name, username, session_path, last_login_at
                FROM accounts
                WHERE phone = ?
                """,
                (phone,),
            ).fetchone()

        if row is None:
            return None

        return Account(
            id=row["id"],
            phone=row["phone"] or "",
            display_name=row["display_name"] or "",
            username=row["username"] or "",
            session_path=row["session_path"] or "",
            last_login_at=row["last_login_at"] or "",
        )

    def latest_account(self) -> Optional[Account]:
        """Return the most recently logged-in account."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, phone, display_name, username, session_path, last_login_at
                FROM accounts
                ORDER BY COALESCE(last_login_at, updated_at, created_at) DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            return None

        return Account(
            id=row["id"],
            phone=row["phone"] or "",
            display_name=row["display_name"] or "",
            username=row["username"] or "",
            session_path=row["session_path"] or "",
            last_login_at=row["last_login_at"] or "",
        )


class ChatRepository:
    """Persist Telegram chat metadata through the database layer."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def upsert_chat(self, chat: Chat) -> Chat:
        """Insert or update one chat row by Telegram chat id."""
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO chats(
                    tg_chat_id,
                    title,
                    username,
                    type,
                    tag,
                    telegram_folder_names,
                    is_created_by_tool,
                    last_message_id,
                    last_backup_message_id,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(tg_chat_id) DO UPDATE SET
                    title = excluded.title,
                    username = excluded.username,
                    type = excluded.type,
                    telegram_folder_names = COALESCE(excluded.telegram_folder_names, telegram_folder_names),
                    is_created_by_tool = excluded.is_created_by_tool,
                    last_message_id = excluded.last_message_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chat.tg_chat_id,
                    chat.title,
                    chat.username,
                    chat.type,
                    chat.tag,
                    chat.telegram_folder_names,
                    1 if chat.is_created_by_tool else 0,
                    chat.last_message_id,
                    chat.last_backup_message_id,
                ),
            )
            connection.commit()

        stored = self.get_by_tg_chat_id(chat.tg_chat_id)
        if stored is None:
            raise RuntimeError("Chat upsert completed without a readable chat row")
        return stored

    def upsert_many(self, chats: List[Chat]) -> List[Chat]:
        """Insert or update multiple chats."""
        return [self.upsert_chat(chat) for chat in chats]

    def list_chats(self, search_text: str = "") -> List[Chat]:
        """Return chats sorted by title, optionally filtered by title or username."""
        keyword = str(search_text).strip()
        if keyword:
            pattern = f"%{keyword}%"
            sql = """
                SELECT *
                FROM chats
                WHERE title LIKE ? OR username LIKE ? OR tag LIKE ? OR telegram_folder_names LIKE ?
                ORDER BY title COLLATE NOCASE ASC, tg_chat_id ASC
            """
            params = (pattern, pattern, pattern, pattern)
        else:
            sql = """
                SELECT *
                FROM chats
                ORDER BY title COLLATE NOCASE ASC, tg_chat_id ASC
            """
            params = ()

        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_chat(row) for row in rows]

    def get_by_tg_chat_id(self, tg_chat_id: int) -> Optional[Chat]:
        """Return one chat by Telegram chat id."""
        with self._database.connect() as connection:
            row = connection.execute("SELECT * FROM chats WHERE tg_chat_id = ?", (tg_chat_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_chat(row)

    def update_tag(self, tg_chat_id: int, tag: str) -> None:
        """Update a user-defined local tag for one chat."""
        with self._database.connect() as connection:
            connection.execute(
                "UPDATE chats SET tag = ?, updated_at = CURRENT_TIMESTAMP WHERE tg_chat_id = ?",
                (str(tag).strip(), tg_chat_id),
            )
            connection.commit()

    def update_last_backup_message_id(self, tg_chat_id: int, last_backup_message_id: int) -> None:
        """Update the latest backed-up Telegram message id for one chat."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE chats
                SET last_backup_message_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE tg_chat_id = ?
                """,
                (int(last_backup_message_id), int(tg_chat_id)),
            )
            connection.commit()

    def delete_chats_by_tg_chat_ids(self, tg_chat_ids: Sequence[int]) -> int:
        """Delete local chat metadata rows by Telegram chat id.

        This only removes local software records. It does not leave or delete
        Telegram chats remotely, and it does not remove archived message rows.
        """
        ids = _unique_ints(tg_chat_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        with self._database.connect() as connection:
            connection.execute(
                f"DELETE FROM groups WHERE tg_group_id IN ({placeholders})",
                tuple(ids),
            )
            cursor = connection.execute(
                f"DELETE FROM chats WHERE tg_chat_id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _row_to_chat(row) -> Chat:
        return Chat(
            id=row["id"],
            tg_chat_id=row["tg_chat_id"],
            title=row["title"] or "",
            username=row["username"] or "",
            type=row["type"] or "unknown",
            tag=row["tag"] or "",
            telegram_folder_names=row["telegram_folder_names"] or "",
            is_created_by_tool=bool(row["is_created_by_tool"]),
            last_message_id=row["last_message_id"],
            last_backup_message_id=row["last_backup_message_id"],
            updated_at=row["updated_at"] or "",
        )


class PublicSearchRepository:
    """Persist public search tasks and normalized results."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def create_task(self, keyword: str, engines: str, max_results: int, log_file: str) -> int:
        """Create a public search task and return its database id."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO public_search_tasks(keyword, engines, max_results, status, log_file)
                VALUES (?, ?, ?, 'running', ?)
                """,
                (keyword, engines, max_results, log_file),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def update_task_log_file(self, task_id: int, log_file: str) -> None:
        """Update the diagnostic log file path for an existing public search task."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE public_search_tasks
                SET log_file = ?
                WHERE id = ?
                """,
                (log_file, int(task_id)),
            )
            connection.commit()

    def complete_task(
        self,
        task_id: int,
        status: str,
        total_found: int,
        total_saved: int,
        success_count: int,
        failed_count: int,
        skipped_count: int,
    ) -> None:
        """Mark a public search task as completed or failed."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE public_search_tasks
                SET status = ?,
                    total_found = ?,
                    total_saved = ?,
                    success_count = ?,
                    failed_count = ?,
                    skipped_count = ?,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, total_found, total_saved, success_count, failed_count, skipped_count, task_id),
            )
            connection.commit()

    def save_results(self, task_id: int, results: List[SearchResult], duplicate_check: bool = True) -> List[SearchResult]:
        """Save search results and mark historical duplicates."""
        saved: List[SearchResult] = []
        with self._database.connect() as connection:
            for result in results:
                is_duplicate = bool(duplicate_check) and self._normalized_url_exists(connection, result.normalized_url)
                cursor = connection.execute(
                    """
                    INSERT INTO public_search_results(
                        task_id,
                        engine_name,
                        rank_no,
                        keyword,
                        result_type,
                        title,
                        summary,
                        url,
                        normalized_url,
                        tg_username,
                        tg_message_id,
                        tg_chat_id,
                        is_duplicate,
                        is_accessible,
                        is_protected,
                        can_forward,
                        forward_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        result.engine_name,
                        result.rank_no,
                        result.keyword,
                        result.result_type,
                        result.title,
                        result.summary,
                        result.url,
                        result.normalized_url,
                        result.tg_username,
                        result.tg_message_id,
                        result.tg_chat_id,
                        1 if is_duplicate else 0,
                        self._optional_bool_to_int(result.is_accessible),
                        1 if result.is_protected else 0,
                        self._optional_bool_to_int(result.can_forward),
                        result.forward_status,
                    ),
                )
                saved.append(self.get_result_by_id_with_connection(connection, int(cursor.lastrowid)))
            connection.commit()
        return saved

    def list_results_for_task(self, task_id: int) -> List[SearchResult]:
        """Return all results for a search task."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM public_search_results
                WHERE task_id = ?
                ORDER BY rank_no ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_search_result(row) for row in rows]

    def list_recent_results(self, limit: int = 200) -> List[SearchResult]:
        """Return recent saved public search results."""
        capped_limit = max(1, min(int(limit), 1000))
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM public_search_results
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (capped_limit,),
            ).fetchall()
        return [self._row_to_search_result(row) for row in rows]

    def list_filtered_results(
        self,
        task_id: Optional[int] = None,
        keyword: str = "",
        result_type: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 500,
    ) -> List[SearchResult]:
        """Return saved public search results with optional advanced-forward filters."""
        clauses = []
        params: list[object] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(int(task_id))

        clean_keyword = str(keyword).strip()
        if clean_keyword:
            pattern = f"%{clean_keyword}%"
            clauses.append("(keyword LIKE ? OR title LIKE ? OR summary LIKE ? OR url LIKE ?)")
            params.extend([pattern, pattern, pattern, pattern])

        clean_result_type = str(result_type).strip()
        if clean_result_type:
            clauses.append("result_type = ?")
            params.append(clean_result_type)

        if str(created_from).strip():
            clauses.append("created_at >= ?")
            params.append(str(created_from).strip())
        if str(created_to).strip():
            clauses.append("created_at <= ?")
            params.append(str(created_to).strip())

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_by = "rank_no ASC, id ASC" if task_id is not None else "created_at DESC, id DESC"
        params.append(max(1, min(int(limit), 1000)))
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM public_search_results
                {where}
                ORDER BY {order_by}
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_search_result(row) for row in rows]

    def distinct_result_types(self, task_id: Optional[int] = None) -> List[str]:
        """Return result types currently present in saved search results."""
        if task_id is None:
            sql = """
                SELECT DISTINCT result_type
                FROM public_search_results
                WHERE COALESCE(result_type, '') <> ''
                ORDER BY result_type COLLATE NOCASE ASC
            """
            params = ()
        else:
            sql = """
                SELECT DISTINCT result_type
                FROM public_search_results
                WHERE task_id = ? AND COALESCE(result_type, '') <> ''
                ORDER BY result_type COLLATE NOCASE ASC
            """
            params = (int(task_id),)
        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [row["result_type"] for row in rows if row["result_type"]]

    def get_results_by_ids(self, result_ids: Sequence[int]) -> List[SearchResult]:
        """Return search results by database ids, preserving the given order."""
        ids = [int(result_id) for result_id in result_ids if int(result_id) > 0]
        if not ids:
            return []

        placeholders = ",".join("?" for _ in ids)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM public_search_results
                WHERE id IN ({placeholders})
                """,
                tuple(ids),
            ).fetchall()

        by_id = {int(row["id"]): self._row_to_search_result(row) for row in rows}
        return [by_id[result_id] for result_id in ids if result_id in by_id]

    def update_forward_status(self, result_id: int, status: str) -> None:
        """Update forward status for one public search result."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE public_search_results
                SET forward_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(status), int(result_id)),
            )
            connection.commit()

    def delete_results_by_ids(self, result_ids: Sequence[int]) -> int:
        """Delete saved public search results and directly related local rows."""
        ids = _unique_positive_ints(result_ids)
        if not ids:
            return 0

        with self._database.connect() as connection:
            deleted = self._delete_results_by_ids_with_connection(connection, ids)
            connection.commit()
        return deleted

    def delete_tasks_by_ids(self, task_ids: Sequence[int]) -> int:
        """Delete public search tasks and all result rows owned by them."""
        ids = _unique_positive_ints(task_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id
                FROM public_search_results
                WHERE task_id IN ({placeholders})
                """,
                tuple(ids),
            ).fetchall()
            result_ids = [int(row["id"]) for row in rows]
            if result_ids:
                self._delete_results_by_ids_with_connection(connection, result_ids, refresh_tasks=False)
            cursor = connection.execute(
                f"DELETE FROM public_search_tasks WHERE id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    def latest_tasks(self, limit: int = 20) -> List[PublicSearchTask]:
        """Return recent public search tasks."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM public_search_tasks
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_result_by_id_with_connection(self, connection, result_id: int) -> SearchResult:
        row = connection.execute("SELECT * FROM public_search_results WHERE id = ?", (result_id,)).fetchone()
        return self._row_to_search_result(row)

    def _delete_results_by_ids_with_connection(
        self,
        connection,
        result_ids: Sequence[int],
        refresh_tasks: bool = True,
    ) -> int:
        ids = _unique_positive_ints(result_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        task_rows = connection.execute(
            f"""
            SELECT DISTINCT task_id
            FROM public_search_results
            WHERE id IN ({placeholders}) AND task_id IS NOT NULL
            """,
            tuple(ids),
        ).fetchall()
        task_ids = [int(row["task_id"]) for row in task_rows if row["task_id"] is not None]

        page_rows = connection.execute(
            f"""
            SELECT id
            FROM telegraph_pages
            WHERE search_result_id IN ({placeholders})
            """,
            tuple(ids),
        ).fetchall()
        page_ids = [int(row["id"]) for row in page_rows]
        if page_ids:
            page_placeholders = _sql_placeholders(page_ids)
            connection.execute(
                f"DELETE FROM telegraph_page_images WHERE page_id IN ({page_placeholders})",
                tuple(page_ids),
            )
            connection.execute(
                f"DELETE FROM telegraph_page_links WHERE page_id IN ({page_placeholders})",
                tuple(page_ids),
            )

        connection.execute(
            f"DELETE FROM telegraph_pages WHERE search_result_id IN ({placeholders})",
            tuple(ids),
        )
        connection.execute(
            f"""
            DELETE FROM forward_records
            WHERE source_type = 'public_search_result' AND source_id IN ({placeholders})
            """,
            tuple(ids),
        )
        cursor = connection.execute(
            f"DELETE FROM public_search_results WHERE id IN ({placeholders})",
            tuple(ids),
        )
        deleted = int(cursor.rowcount or 0)
        if refresh_tasks and task_ids:
            self._refresh_task_result_counts(connection, task_ids)
        return deleted

    @staticmethod
    def _refresh_task_result_counts(connection, task_ids: Sequence[int]) -> None:
        ids = _unique_positive_ints(task_ids)
        for task_id in ids:
            row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_saved,
                    SUM(CASE WHEN is_duplicate = 0 THEN 1 ELSE 0 END) AS success_count,
                    SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) AS skipped_count
                FROM public_search_results
                WHERE task_id = ?
                """,
                (int(task_id),),
            ).fetchone()
            connection.execute(
                """
                UPDATE public_search_tasks
                SET total_saved = ?,
                    success_count = ?,
                    skipped_count = ?
                WHERE id = ?
                """,
                (
                    int(row["total_saved"] or 0),
                    int(row["success_count"] or 0),
                    int(row["skipped_count"] or 0),
                    int(task_id),
                ),
            )

    @staticmethod
    def _normalized_url_exists(connection, normalized_url: str) -> bool:
        if not normalized_url:
            return False
        row = connection.execute(
            "SELECT 1 FROM public_search_results WHERE normalized_url = ? LIMIT 1",
            (normalized_url,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _optional_bool_to_int(value: Optional[bool]) -> Optional[int]:
        if value is None:
            return None
        return 1 if value else 0

    @classmethod
    def _row_to_search_result(cls, row) -> SearchResult:
        return SearchResult(
            id=row["id"],
            task_id=row["task_id"],
            engine_name=row["engine_name"] or "",
            rank_no=row["rank_no"] or 0,
            keyword=row["keyword"] or "",
            result_type=row["result_type"] or "unknown",
            title=row["title"] or "",
            summary=row["summary"] or "",
            url=row["url"] or "",
            normalized_url=row["normalized_url"] or "",
            tg_username=row["tg_username"] or "",
            tg_message_id=row["tg_message_id"],
            tg_chat_id=row["tg_chat_id"],
            is_duplicate=bool(row["is_duplicate"]),
            is_accessible=cls._optional_int_to_bool(row["is_accessible"]),
            is_protected=bool(row["is_protected"]),
            can_forward=cls._optional_int_to_bool(row["can_forward"]),
            forward_status=row["forward_status"] or "",
            created_at=row["created_at"] or "",
        )

    @staticmethod
    def _optional_int_to_bool(value: Optional[int]) -> Optional[bool]:
        if value is None:
            return None
        return bool(value)

    @staticmethod
    def _row_to_task(row) -> PublicSearchTask:
        return PublicSearchTask(
            id=row["id"],
            keyword=row["keyword"] or "",
            engines=row["engines"] or "",
            max_results=row["max_results"] or 0,
            status=row["status"] or "",
            total_found=row["total_found"] or 0,
            total_saved=row["total_saved"] or 0,
            success_count=row["success_count"] or 0,
            failed_count=row["failed_count"] or 0,
            skipped_count=row["skipped_count"] or 0,
            log_file=row["log_file"] or "",
            created_at=row["created_at"] or "",
            finished_at=row["finished_at"] or "",
        )


class TelegraphRepository:
    """Persist parsed Telegraph page cards and their extracted assets."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def upsert_page_for_search_result(
        self,
        search_result_id: int,
        page: TelegraphPage,
        images: Sequence[TelegraphImage],
        telegram_links: Sequence[TelegraphLink],
    ) -> TelegraphPage:
        """Insert or update one parsed Telegraph page for a public search result."""
        return self._upsert_page(
            search_result_id=int(search_result_id),
            message_db_id=page.message_db_id,
            page=page,
            images=images,
            telegram_links=telegram_links,
        )

    def upsert_page_for_message(
        self,
        message_db_id: int,
        page: TelegraphPage,
        images: Sequence[TelegraphImage],
        telegram_links: Sequence[TelegraphLink],
    ) -> TelegraphPage:
        """Insert or update one parsed Telegraph page for a backed-up message."""
        return self._upsert_page(
            search_result_id=page.search_result_id,
            message_db_id=int(message_db_id),
            page=page,
            images=images,
            telegram_links=telegram_links,
        )

    def get_page_by_message_id(self, message_db_id: int, normalized_url: str) -> Optional[TelegraphPage]:
        """Return one parsed Telegraph page by local message id and page URL."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM telegraph_pages
                WHERE message_db_id = ? AND normalized_url = ?
                """,
                (int(message_db_id), str(normalized_url)),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_page(row)

    def get_page_by_search_result_id(self, search_result_id: int) -> Optional[TelegraphPage]:
        """Return parsed Telegraph page metadata by search result id."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM telegraph_pages WHERE search_result_id = ?",
                (int(search_result_id),),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_page(row)

    def list_images_for_search_result(
        self,
        search_result_id: int,
        limit: Optional[int] = None,
    ) -> List[TelegraphImage]:
        """Return extracted Telegraph page images for a search result."""
        page = self.get_page_by_search_result_id(search_result_id)
        if page is None or page.id is None:
            return []
        return self.list_images_for_page(int(page.id), limit=limit)

    def list_images_for_page(self, page_id: int, limit: Optional[int] = None) -> List[TelegraphImage]:
        """Return extracted Telegraph page images ordered by page position."""
        capped_limit = None if limit is None else max(0, int(limit))
        sql = """
            SELECT *
            FROM telegraph_page_images
            WHERE page_id = ?
            ORDER BY position ASC, id ASC
        """
        params: tuple[object, ...] = (int(page_id),)
        if capped_limit is not None:
            sql = f"{sql} LIMIT ?"
            params = (int(page_id), capped_limit)
        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_image(row) for row in rows]

    def list_links_for_page(self, page_id: int) -> List[TelegraphLink]:
        """Return Telegram links extracted from one Telegraph page."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM telegraph_page_links
                WHERE page_id = ?
                ORDER BY position ASC, id ASC
                """,
                (int(page_id),),
            ).fetchall()
        return [self._row_to_link(row) for row in rows]

    def update_image_download_status(
        self,
        image_id: int,
        status: str,
        local_path: str = "",
        error_message: str = "",
    ) -> None:
        """Update one Telegraph image download outcome."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE telegraph_page_images
                SET download_status = ?,
                    local_path = ?,
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(status), str(local_path), str(error_message)[:500], int(image_id)),
            )
            connection.commit()

    def _upsert_page(
        self,
        search_result_id: Optional[int],
        message_db_id: Optional[int],
        page: TelegraphPage,
        images: Sequence[TelegraphImage],
        telegram_links: Sequence[TelegraphLink],
    ) -> TelegraphPage:
        with self._database.connect() as connection:
            existing = None
            if search_result_id is not None:
                existing = connection.execute(
                    "SELECT id FROM telegraph_pages WHERE search_result_id = ?",
                    (int(search_result_id),),
                ).fetchone()
            if existing is None and message_db_id is not None:
                existing = connection.execute(
                    """
                    SELECT id
                    FROM telegraph_pages
                    WHERE message_db_id = ? AND normalized_url = ?
                    """,
                    (int(message_db_id), page.normalized_url),
                ).fetchone()

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO telegraph_pages(
                        search_result_id,
                        message_db_id,
                        url,
                        normalized_url,
                        title,
                        published_at,
                        author_name,
                        author_url,
                        image_count,
                        telegram_link_count
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        search_result_id,
                        message_db_id,
                        page.url,
                        page.normalized_url,
                        page.title,
                        page.published_at,
                        page.author_name,
                        page.author_url,
                        int(page.image_count),
                        int(page.telegram_link_count),
                    ),
                )
                page_id = int(cursor.lastrowid)
            else:
                page_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE telegraph_pages
                    SET search_result_id = COALESCE(?, search_result_id),
                        message_db_id = COALESCE(?, message_db_id),
                        url = ?,
                        normalized_url = ?,
                        title = ?,
                        published_at = ?,
                        author_name = ?,
                        author_url = ?,
                        image_count = ?,
                        telegram_link_count = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        search_result_id,
                        message_db_id,
                        page.url,
                        page.normalized_url,
                        page.title,
                        page.published_at,
                        page.author_name,
                        page.author_url,
                        int(page.image_count),
                        int(page.telegram_link_count),
                        page_id,
                    ),
                )

            connection.execute("DELETE FROM telegraph_page_images WHERE page_id = ?", (page_id,))
            connection.execute("DELETE FROM telegraph_page_links WHERE page_id = ?", (page_id,))

            for image in images:
                connection.execute(
                    """
                    INSERT INTO telegraph_page_images(
                        page_id,
                        position,
                        url,
                        normalized_url,
                        local_path,
                        download_status,
                        error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        int(image.position),
                        image.url,
                        image.normalized_url,
                        image.local_path,
                        image.download_status or "pending",
                        image.error_message,
                    ),
                )

            for link in telegram_links:
                connection.execute(
                    """
                    INSERT INTO telegraph_page_links(
                        page_id,
                        position,
                        url,
                        normalized_url,
                        link_type,
                        text
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        page_id,
                        int(link.position),
                        link.url,
                        link.normalized_url,
                        link.link_type,
                        link.text,
                    ),
                )

            connection.commit()
            row = connection.execute("SELECT * FROM telegraph_pages WHERE id = ?", (page_id,)).fetchone()

        return self._row_to_page(row)

    @staticmethod
    def _row_to_page(row) -> TelegraphPage:
        return TelegraphPage(
            id=row["id"],
            search_result_id=row["search_result_id"],
            message_db_id=row["message_db_id"],
            url=row["url"] or "",
            normalized_url=row["normalized_url"] or "",
            title=row["title"] or "",
            published_at=row["published_at"] or "",
            author_name=row["author_name"] or "",
            author_url=row["author_url"] or "",
            image_count=row["image_count"] or 0,
            telegram_link_count=row["telegram_link_count"] or 0,
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    @staticmethod
    def _row_to_image(row) -> TelegraphImage:
        return TelegraphImage(
            id=row["id"],
            page_id=row["page_id"],
            position=row["position"] or 0,
            url=row["url"] or "",
            normalized_url=row["normalized_url"] or "",
            local_path=row["local_path"] or "",
            download_status=row["download_status"] or "",
            error_message=row["error_message"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )

    @staticmethod
    def _row_to_link(row) -> TelegraphLink:
        return TelegraphLink(
            id=row["id"],
            page_id=row["page_id"],
            position=row["position"] or 0,
            url=row["url"] or "",
            normalized_url=row["normalized_url"] or "",
            link_type=row["link_type"] or "",
            text=row["text"] or "",
            created_at=row["created_at"] or "",
        )


class GroupRepository:
    """Persist tool-created Telegram groups."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def upsert_group(self, chat: Chat, category: str = "", name_rule: str = "") -> Chat:
        """Insert or update a tool-managed group row and return its chat metadata."""
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO groups(tg_group_id, title, category, name_rule, is_created_by_tool, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(tg_group_id) DO UPDATE SET
                    title = excluded.title,
                    category = excluded.category,
                    name_rule = excluded.name_rule,
                    is_created_by_tool = excluded.is_created_by_tool,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    chat.tg_chat_id,
                    chat.title,
                    str(category).strip(),
                    str(name_rule).strip(),
                    1 if chat.is_created_by_tool else 0,
                ),
            )
            connection.commit()
        return chat

    def list_groups(self) -> List[Chat]:
        """Return locally known tool-created groups."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT tg_group_id, title, category, is_created_by_tool, updated_at
                FROM groups
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        return [
            Chat(
                id=None,
                tg_chat_id=row["tg_group_id"],
                title=row["title"] or "",
                username="",
                type="group",
                tag=row["category"] or "",
                is_created_by_tool=bool(row["is_created_by_tool"]),
                updated_at=row["updated_at"] or "",
            )
            for row in rows
        ]

    def delete_groups_by_chat_ids(self, tg_group_ids: Sequence[int]) -> int:
        """Delete locally registered tool-group rows by Telegram group id."""
        ids = _unique_ints(tg_group_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        with self._database.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM groups WHERE tg_group_id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)


class TaskRepository:
    """Persist long-running task summaries."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def create_task(
        self,
        task_id: str,
        task_type: str,
        title: str,
        source_config: dict,
        target_config: dict,
        total_count: int,
        log_file: str,
    ) -> None:
        """Create a running task summary row."""
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id,
                    task_type,
                    title,
                    status,
                    source_config_json,
                    target_config_json,
                    total_count,
                    success_count,
                    failed_count,
                    skipped_count,
                    progress,
                    log_file,
                    started_at
                )
                VALUES (?, ?, ?, 'running', ?, ?, ?, 0, 0, 0, 0, ?, CURRENT_TIMESTAMP)
                """,
                (
                    task_id,
                    task_type,
                    title,
                    json.dumps(source_config, ensure_ascii=False),
                    json.dumps(target_config, ensure_ascii=False),
                    int(total_count),
                    log_file,
                ),
            )
            connection.commit()

    def update_progress(
        self,
        task_id: str,
        status: str,
        success_count: int,
        failed_count: int,
        skipped_count: int,
        progress: int,
        finished: bool = False,
    ) -> None:
        """Update task counts and optionally mark it finished."""
        finished_sql = ", finished_at = CURRENT_TIMESTAMP" if finished else ""
        with self._database.connect() as connection:
            connection.execute(
                f"""
                UPDATE tasks
                SET status = ?,
                    success_count = ?,
                    failed_count = ?,
                    skipped_count = ?,
                    progress = ?
                    {finished_sql}
                WHERE task_id = ?
                """,
                (status, int(success_count), int(failed_count), int(skipped_count), int(progress), task_id),
            )
            connection.commit()

    def latest_tasks(self, task_type: str = "", limit: int = 20) -> List[TaskSummary]:
        """Return recent task summaries."""
        params: tuple = (int(limit),)
        where = ""
        if task_type:
            where = "WHERE task_type = ?"
            params = (task_type, int(limit))
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT task_id, task_type, title, status, total_count, success_count,
                       failed_count, skipped_count, progress, log_file
                FROM tasks
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_task_summary(row) for row in rows]

    def delete_tasks_by_task_ids(self, task_ids: Sequence[str]) -> int:
        """Delete task summaries and their forward/download detail records."""
        ids: List[str] = []
        seen: set[str] = set()
        for value in task_ids:
            task_id = str(value or "").strip()
            if not task_id or task_id in seen:
                continue
            ids.append(task_id)
            seen.add(task_id)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        params = tuple(ids)
        with self._database.connect() as connection:
            connection.execute(
                f"DELETE FROM forward_records WHERE task_id IN ({placeholders})",
                params,
            )
            connection.execute(
                f"DELETE FROM download_records WHERE task_id IN ({placeholders})",
                params,
            )
            cursor = connection.execute(
                f"DELETE FROM tasks WHERE task_id IN ({placeholders})",
                params,
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _row_to_task_summary(row) -> TaskSummary:
        return TaskSummary(
            task_id=row["task_id"] or "",
            task_type=row["task_type"] or "",
            title=row["title"] or "",
            status=row["status"] or "",
            total_count=row["total_count"] or 0,
            success_count=row["success_count"] or 0,
            failed_count=row["failed_count"] or 0,
            skipped_count=row["skipped_count"] or 0,
            progress=row["progress"] or 0,
            log_file=row["log_file"] or "",
        )


class ForwardRepository:
    """Persist card-forwarding records."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def create_record(self, record: ForwardRecord) -> ForwardRecord:
        """Insert one forward record and return the stored row."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO forward_records(
                    task_id,
                    source_type,
                    source_id,
                    source_chat_id,
                    source_message_id,
                    target_chat_id,
                    target_message_id,
                    forward_mode,
                    status,
                    reason,
                    error_code
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.source_type,
                    record.source_id,
                    record.source_chat_id,
                    record.source_message_id,
                    record.target_chat_id,
                    record.target_message_id,
                    record.forward_mode,
                    record.status,
                    record.reason,
                    record.error_code,
                ),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM forward_records WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        return self._row_to_forward_record(row)

    def list_records_for_task(self, task_id: str) -> List[ForwardRecord]:
        """Return forward records for one task."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM forward_records
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_forward_record(row) for row in rows]

    def delete_records_by_ids(self, record_ids: Sequence[int]) -> int:
        """Delete forward detail records by database id."""
        ids = _unique_positive_ints(record_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        with self._database.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM forward_records WHERE id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _row_to_forward_record(row) -> ForwardRecord:
        return ForwardRecord(
            id=row["id"],
            task_id=row["task_id"] or "",
            source_type=row["source_type"] or "",
            source_id=row["source_id"],
            source_chat_id=row["source_chat_id"],
            source_message_id=row["source_message_id"],
            target_chat_id=row["target_chat_id"],
            target_message_id=row["target_message_id"],
            forward_mode=row["forward_mode"] or "",
            status=row["status"] or "",
            reason=row["reason"] or "",
            error_code=row["error_code"] or "",
            created_at=row["created_at"] or "",
        )


class MessageRepository:
    """Persist locally backed-up Telegram messages."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def upsert_message(self, message: MessageRecord) -> MessageRecord:
        """Insert or update a backed-up Telegram message."""
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO messages(
                    tg_chat_id,
                    message_id,
                    sender_id,
                    sender_name,
                    date,
                    text,
                    text_preview,
                    message_type,
                    has_media,
                    media_type,
                    media_id,
                    file_name,
                    file_size,
                    local_path,
                    is_downloaded,
                    is_protected,
                    is_forwarded,
                    source_link,
                    external_urls,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(tg_chat_id, message_id) DO UPDATE SET
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    date = excluded.date,
                    text = excluded.text,
                    text_preview = excluded.text_preview,
                    message_type = excluded.message_type,
                    has_media = excluded.has_media,
                    media_type = excluded.media_type,
                    media_id = excluded.media_id,
                    file_name = excluded.file_name,
                    file_size = excluded.file_size,
                    is_protected = excluded.is_protected,
                    source_link = excluded.source_link,
                    external_urls = excluded.external_urls,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    message.tg_chat_id,
                    message.message_id,
                    message.sender_id,
                    message.sender_name,
                    message.date,
                    message.text,
                    message.text_preview,
                    message.message_type,
                    1 if message.has_media else 0,
                    message.media_type,
                    message.media_id,
                    message.file_name,
                    message.file_size,
                    message.local_path,
                    1 if message.is_downloaded else 0,
                    1 if message.is_protected else 0,
                    1 if message.is_forwarded else 0,
                    message.source_link,
                    message.external_urls,
                ),
            )
            connection.commit()

        stored = self.get_by_chat_and_message_id(message.tg_chat_id, message.message_id)
        if stored is None:
            raise RuntimeError("Message upsert completed without a readable message row")
        return stored

    def get_by_chat_and_message_id(self, tg_chat_id: int, message_id: int) -> Optional[MessageRecord]:
        """Return one backed-up message by Telegram chat id and message id."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE tg_chat_id = ? AND message_id = ?",
                (int(tg_chat_id), int(message_id)),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    def mark_downloaded(self, message_db_id: int, local_path: str) -> None:
        """Mark a message's media as downloaded."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET is_downloaded = 1, local_path = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (str(local_path), int(message_db_id)),
            )
            connection.commit()

    def mark_forwarded(self, message_db_id: int) -> None:
        """Mark a backed-up message as forwarded at least once."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE messages
                SET is_forwarded = 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (int(message_db_id),),
            )
            connection.commit()

    def list_messages(self, tg_chat_id: Optional[int] = None, limit: int = 200) -> List[MessageRecord]:
        """Return recent local messages, optionally filtered by chat."""
        if tg_chat_id is None:
            sql = """
                SELECT *
                FROM messages
                ORDER BY date DESC, id DESC
                LIMIT ?
            """
            params = (max(1, min(int(limit), 1000)),)
        else:
            sql = """
                SELECT *
                FROM messages
                WHERE tg_chat_id = ?
                ORDER BY message_id DESC
                LIMIT ?
            """
            params = (int(tg_chat_id), max(1, min(int(limit), 1000)))
        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._row_to_message(row) for row in rows]

    def count_messages(self, tg_chat_id: int) -> int:
        """Return the number of locally backed-up messages for one chat."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM messages WHERE tg_chat_id = ?",
                (int(tg_chat_id),),
            ).fetchone()
        return int(row["count"] or 0)

    def delete_messages_by_keys(self, message_keys: Sequence[tuple[int, int]]) -> int:
        """Delete backed-up messages and directly related local metadata.

        The method deletes local SQLite rows only. Downloaded files on disk are
        intentionally preserved so users can decide separately whether to remove
        local media artifacts.
        """
        keys = _unique_message_keys(message_keys)
        if not keys:
            return 0

        key_where = " OR ".join("(tg_chat_id = ? AND message_id = ?)" for _ in keys)
        key_params: list[int] = []
        for tg_chat_id, message_id in keys:
            key_params.extend([tg_chat_id, message_id])

        with self._database.connect() as connection:
            message_rows = connection.execute(
                f"SELECT id FROM messages WHERE {key_where}",
                tuple(key_params),
            ).fetchall()
            message_ids = [int(row["id"]) for row in message_rows]
            if not message_ids:
                return 0

            message_placeholders = _sql_placeholders(message_ids)
            file_rows = connection.execute(
                f"""
                SELECT id
                FROM files
                WHERE message_db_id IN ({message_placeholders}) OR {key_where}
                """,
                tuple(message_ids + key_params),
            ).fetchall()
            file_ids = [int(row["id"]) for row in file_rows]

            page_rows = connection.execute(
                f"""
                SELECT id
                FROM telegraph_pages
                WHERE message_db_id IN ({message_placeholders})
                """,
                tuple(message_ids),
            ).fetchall()
            page_ids = [int(row["id"]) for row in page_rows]
            if page_ids:
                page_placeholders = _sql_placeholders(page_ids)
                connection.execute(
                    f"DELETE FROM telegraph_page_images WHERE page_id IN ({page_placeholders})",
                    tuple(page_ids),
                )
                connection.execute(
                    f"DELETE FROM telegraph_page_links WHERE page_id IN ({page_placeholders})",
                    tuple(page_ids),
                )

            if file_ids:
                file_placeholders = _sql_placeholders(file_ids)
                connection.execute(
                    f"DELETE FROM download_records WHERE file_id IN ({file_placeholders})",
                    tuple(file_ids),
                )

            connection.execute(
                f"DELETE FROM download_records WHERE message_db_id IN ({message_placeholders})",
                tuple(message_ids),
            )
            connection.execute(
                f"DELETE FROM telegraph_pages WHERE message_db_id IN ({message_placeholders})",
                tuple(message_ids),
            )
            connection.execute(
                f"""
                DELETE FROM forward_records
                WHERE source_type = 'message_record' AND source_id IN ({message_placeholders})
                """,
                tuple(message_ids),
            )
            if file_ids:
                file_placeholders = _sql_placeholders(file_ids)
                connection.execute(
                    f"DELETE FROM files WHERE id IN ({file_placeholders})",
                    tuple(file_ids),
                )
            cursor = connection.execute(
                f"DELETE FROM messages WHERE id IN ({message_placeholders})",
                tuple(message_ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    def search_messages(
        self,
        keyword: str = "",
        tg_chat_id: Optional[int] = None,
        date_from: str = "",
        date_to: str = "",
        message_type: str = "",
        media_filter: str = "all",
        limit: int = 500,
    ) -> List[MessageRecord]:
        """Search locally backed-up messages with optional filters."""
        clauses = []
        params: list[object] = []

        clean_keyword = str(keyword).strip()
        if clean_keyword:
            pattern = f"%{clean_keyword}%"
            clauses.append("(text LIKE ? OR text_preview LIKE ? OR sender_name LIKE ? OR file_name LIKE ? OR source_link LIKE ? OR external_urls LIKE ?)")
            params.extend([pattern, pattern, pattern, pattern, pattern, pattern])

        if tg_chat_id is not None:
            clauses.append("tg_chat_id = ?")
            params.append(int(tg_chat_id))

        if str(date_from).strip():
            clauses.append("date >= ?")
            params.append(str(date_from).strip())
        if str(date_to).strip():
            clauses.append("date <= ?")
            params.append(str(date_to).strip())

        clean_type = str(message_type).strip()
        if clean_type:
            clauses.append("message_type = ?")
            params.append(clean_type)

        clean_media_filter = str(media_filter or "all").strip()
        if clean_media_filter == "media":
            clauses.append("has_media = 1")
        elif clean_media_filter == "downloaded":
            clauses.append("is_downloaded = 1")
        elif clean_media_filter == "text":
            clauses.append("has_media = 0")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        with self._database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM messages
                {where}
                ORDER BY date DESC, tg_chat_id ASC, message_id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def distinct_message_types(self) -> List[str]:
        """Return local message types present in the message table."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT message_type
                FROM messages
                WHERE COALESCE(message_type, '') <> ''
                ORDER BY message_type COLLATE NOCASE ASC
                """
            ).fetchall()
        return [row["message_type"] for row in rows if row["message_type"]]

    @staticmethod
    def _row_to_message(row) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            tg_chat_id=row["tg_chat_id"],
            message_id=row["message_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"] or "",
            date=row["date"] or "",
            text=row["text"] or "",
            text_preview=row["text_preview"] or "",
            message_type=row["message_type"] or "",
            has_media=bool(row["has_media"]),
            media_type=row["media_type"] or "",
            media_id=row["media_id"] or "",
            file_name=row["file_name"] or "",
            file_size=row["file_size"],
            local_path=row["local_path"] or "",
            is_downloaded=bool(row["is_downloaded"]),
            is_protected=bool(row["is_protected"]),
            is_forwarded=bool(row["is_forwarded"]),
            source_link=row["source_link"] or "",
            external_urls=row["external_urls"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


class FileRepository:
    """Persist local file metadata for backed-up messages."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def upsert_file_for_message(self, file_record: FileRecord) -> FileRecord:
        """Insert or update file metadata for one message."""
        existing = self.get_by_message(file_record.tg_chat_id, file_record.message_id)
        with self._database.connect() as connection:
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO files(
                        message_db_id,
                        tg_chat_id,
                        message_id,
                        file_name,
                        file_ext,
                        file_size,
                        local_path,
                        file_hash,
                        download_status,
                        error_code,
                        error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file_record.message_db_id,
                        file_record.tg_chat_id,
                        file_record.message_id,
                        file_record.file_name,
                        file_record.file_ext,
                        file_record.file_size,
                        file_record.local_path,
                        file_record.file_hash,
                        file_record.download_status,
                        file_record.error_code,
                        file_record.error_message,
                    ),
                )
                file_id = int(cursor.lastrowid)
            else:
                file_id = int(existing.id)
                connection.execute(
                    """
                    UPDATE files
                    SET message_db_id = ?,
                        file_name = ?,
                        file_ext = ?,
                        file_size = ?,
                        local_path = ?,
                        file_hash = ?,
                        download_status = ?,
                        error_code = ?,
                        error_message = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        file_record.message_db_id,
                        file_record.file_name,
                        file_record.file_ext,
                        file_record.file_size,
                        file_record.local_path,
                        file_record.file_hash,
                        file_record.download_status,
                        file_record.error_code,
                        file_record.error_message,
                        file_id,
                    ),
                )
            connection.commit()
        stored = self.get_by_id(file_id)
        if stored is None:
            raise RuntimeError("File upsert completed without a readable file row")
        return stored

    def update_download_status(
        self,
        file_id: int,
        status: str,
        local_path: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        """Update file download status."""
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE files
                SET download_status = ?,
                    local_path = ?,
                    error_code = ?,
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, local_path, error_code, error_message, int(file_id)),
            )
            connection.commit()

    def get_by_id(self, file_id: int) -> Optional[FileRecord]:
        """Return a file record by database id."""
        with self._database.connect() as connection:
            row = connection.execute("SELECT * FROM files WHERE id = ?", (int(file_id),)).fetchone()
        if row is None:
            return None
        return self._row_to_file(row)

    def get_by_message(self, tg_chat_id: int, message_id: int) -> Optional[FileRecord]:
        """Return one file record by Telegram message identity."""
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM files
                WHERE tg_chat_id = ? AND message_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(tg_chat_id), int(message_id)),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_file(row)

    def delete_files_by_ids(self, file_ids: Sequence[int]) -> int:
        """Delete local file metadata rows and their download records."""
        ids = _unique_positive_ints(file_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        with self._database.connect() as connection:
            connection.execute(
                f"DELETE FROM download_records WHERE file_id IN ({placeholders})",
                tuple(ids),
            )
            cursor = connection.execute(
                f"DELETE FROM files WHERE id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _row_to_file(row) -> FileRecord:
        return FileRecord(
            id=row["id"],
            message_db_id=row["message_db_id"],
            tg_chat_id=row["tg_chat_id"],
            message_id=row["message_id"],
            file_name=row["file_name"] or "",
            file_ext=row["file_ext"] or "",
            file_size=row["file_size"],
            local_path=row["local_path"] or "",
            file_hash=row["file_hash"] or "",
            download_status=row["download_status"] or "",
            error_code=row["error_code"] or "",
            error_message=row["error_message"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


class DownloadRecordRepository:
    """Persist media download attempts and outcomes."""

    def __init__(self, database: DatabaseManager):
        self._database = database

    def create_record(self, record: DownloadRecord) -> DownloadRecord:
        """Insert one download record and return the stored row."""
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO download_records(
                    task_id,
                    message_db_id,
                    file_id,
                    status,
                    local_path,
                    error_code,
                    error_message
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.message_db_id,
                    record.file_id,
                    record.status,
                    record.local_path,
                    record.error_code,
                    record.error_message,
                ),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM download_records WHERE id = ?", (int(cursor.lastrowid),)).fetchone()
        return self._row_to_download_record(row)

    def list_records_for_task(self, task_id: str) -> List[DownloadRecord]:
        """Return download records for one task."""
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM download_records
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_download_record(row) for row in rows]

    def delete_records_by_ids(self, record_ids: Sequence[int]) -> int:
        """Delete download detail records by database id."""
        ids = _unique_positive_ints(record_ids)
        if not ids:
            return 0

        placeholders = _sql_placeholders(ids)
        with self._database.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM download_records WHERE id IN ({placeholders})",
                tuple(ids),
            )
            connection.commit()
        return int(cursor.rowcount or 0)

    @staticmethod
    def _row_to_download_record(row) -> DownloadRecord:
        return DownloadRecord(
            id=row["id"],
            task_id=row["task_id"] or "",
            message_db_id=row["message_db_id"],
            file_id=row["file_id"],
            status=row["status"] or "",
            local_path=row["local_path"] or "",
            error_code=row["error_code"] or "",
            error_message=row["error_message"] or "",
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )
