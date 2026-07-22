"""Message backup service for Telegram chats."""

from __future__ import annotations

import inspect
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from database.models import FileRecord, MessageRecord
from database.repositories import ChatRepository, FileRepository, MessageRepository, TaskRepository
from services.cancellation import CancellationToken, OperationCancelled, check_cancelled
from services.download_service import DownloadService
from services.telegram_service import TelegramArchivedMessage, TelegramService, TelegramServiceError
from utils.error_codes import BK001


class BackupServiceError(RuntimeError):
    """Raised when a backup task cannot be started or completed."""

    error_code = BK001


@dataclass(frozen=True)
class BackupProgress:
    """Progress emitted after one message is saved or downloaded."""

    task_id: str
    done_count: int
    total_count: int
    saved_count: int
    downloaded_count: int
    failed_count: int
    skipped_count: int
    message: str


@dataclass(frozen=True)
class BackupReport:
    """Final backup task report."""

    task_id: str
    tg_chat_id: int
    total_count: int
    saved_count: int
    downloaded_count: int
    failed_count: int
    skipped_count: int
    log_file: str


class BackupService:
    """Back up Telegram message metadata and optionally download media."""

    def __init__(
        self,
        telegram_service: TelegramService,
        chat_repository: ChatRepository,
        message_repository: MessageRepository,
        file_repository: FileRepository,
        task_repository: TaskRepository,
        download_service: DownloadService,
        logger: logging.Logger,
        log_file: str,
        task_logger_factory: Optional[Callable[[str, str], logging.Logger]] = None,
        task_log_path_factory: Optional[Callable[[str, str], Path]] = None,
    ):
        self._telegram_service = telegram_service
        self._chat_repository = chat_repository
        self._message_repository = message_repository
        self._file_repository = file_repository
        self._task_repository = task_repository
        self._download_service = download_service
        self._logger = logger
        self._log_file = log_file
        self._task_logger_factory = task_logger_factory
        self._task_log_path_factory = task_log_path_factory

    def backup_chat(
        self,
        api_id: str,
        api_hash: str,
        tg_chat_id: int,
        limit: int = 100,
        date_from: str = "",
        date_to: str = "",
        incremental: bool = True,
        download_media: bool = False,
        retry_count: int = 3,
        selected_message_ids: Optional[list[int]] = None,
        progress_callback: Optional[Callable[[BackupProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> BackupReport:
        """Back up messages from one chat and optionally download allowed media."""
        check_cancelled(cancel_token)
        chat = self._chat_repository.get_by_tg_chat_id(int(tg_chat_id))
        selected_ids = self._normal_message_id_set(selected_message_ids)
        min_message_id = self._min_message_id_for_backup(
            tg_chat_id=int(tg_chat_id),
            limit=int(limit),
            chat_last_backup_message_id=chat.last_backup_message_id if chat is not None else None,
            incremental=bool(incremental),
            has_selected_ids=bool(selected_ids),
        )
        task_id = self._new_task_id()
        task_logger = self._task_logger(task_id)
        task_log_file = self._task_log_file(task_id)

        task_logger.info(
            "Backup task %s started: chat_id=%s limit=%s min_message_id=%s download_media=%s",
            task_id,
            tg_chat_id,
            limit,
            min_message_id,
            download_media,
        )
        try:
            archived_messages = self._fetch_chat_messages(
                api_id=api_id,
                api_hash=api_hash,
                tg_chat_id=int(tg_chat_id),
                limit=int(limit),
                min_message_id=min_message_id,
                date_from=date_from,
                date_to=date_to,
                cancel_token=cancel_token,
            )
        except OperationCancelled:
            task_logger.info("Backup task %s cancelled before message task row was created", task_id)
            raise
        except TelegramServiceError as exc:
            task_logger.exception("Backup task %s telegram fetch failure: %s", task_id, exc)
            raise
        except Exception as exc:
            task_logger.exception("Backup message fetch failed: %s", exc)
            raise BackupServiceError("消息备份读取失败，请查看 download.log") from exc

        if selected_ids:
            archived_messages = [
                message for message in archived_messages if int(message.message_id) in selected_ids
            ]

        total_count = len(archived_messages)
        self._task_repository.create_task(
            task_id=task_id,
            task_type="backup",
            title=f"消息备份 {total_count} 条",
            source_config={
                "tg_chat_id": int(tg_chat_id),
                "limit": int(limit),
                "date_from": date_from,
                "date_to": date_to,
                "incremental": bool(incremental),
                "selected_message_ids": sorted(selected_ids),
            },
            target_config={"download_media": bool(download_media)},
            total_count=total_count,
            log_file=task_log_file,
        )

        state = {"done": 0, "saved": 0, "downloaded": 0, "failed": 0, "skipped": 0}
        latest_message_id = min_message_id
        try:
            for archived in archived_messages:
                check_cancelled(cancel_token)
                message = self._message_record_from_archived(archived)
                stored_message = self._message_repository.upsert_message(message)
                state["saved"] += 1
                latest_message_id = max(latest_message_id, stored_message.message_id)

                file_record = None
                if stored_message.has_media:
                    file_record = self._file_record_from_message(stored_message)
                    file_record = self._file_repository.upsert_file_for_message(file_record)

                if download_media and file_record is not None:
                    result = self._download_service.download_message_media(
                        api_id=api_id,
                        api_hash=api_hash,
                        task_id=task_id,
                        message=stored_message,
                        file_record=file_record,
                        retry_count=retry_count,
                        cancel_token=cancel_token,
                    )
                    if result.status == "success":
                        state["downloaded"] += 1
                    elif result.status == "skipped":
                        state["skipped"] += 1
                    else:
                        state["failed"] += 1
                state["done"] += 1
                self._update_task_progress(task_id, state, total_count, finished=False)
                task_logger.info(
                    "Backup task %s message processed: chat_id=%s message_id=%s saved=%s downloaded=%s failed=%s skipped=%s",
                    task_id,
                    stored_message.tg_chat_id,
                    stored_message.message_id,
                    state["saved"],
                    state["downloaded"],
                    state["failed"],
                    state["skipped"],
                )
                if progress_callback is not None:
                    progress_callback(
                        BackupProgress(
                            task_id=task_id,
                            done_count=state["done"],
                            total_count=total_count,
                            saved_count=state["saved"],
                            downloaded_count=state["downloaded"],
                            failed_count=state["failed"],
                            skipped_count=state["skipped"],
                            message=f"已备份消息 {stored_message.message_id}",
                        )
                    )

            check_cancelled(cancel_token)
            if latest_message_id > min_message_id:
                self._chat_repository.update_last_backup_message_id(int(tg_chat_id), latest_message_id)

            final_status = "completed" if state["failed"] == 0 else "completed_with_errors"
            self._task_repository.update_progress(
                task_id=task_id,
                status=final_status,
                success_count=state["saved"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=100,
                finished=True,
            )
            task_logger.info(
                "Backup task %s finished: saved=%s downloaded=%s failed=%s skipped=%s",
                task_id,
                state["saved"],
                state["downloaded"],
                state["failed"],
                state["skipped"],
            )
            return BackupReport(
                task_id=task_id,
                tg_chat_id=int(tg_chat_id),
                total_count=total_count,
                saved_count=state["saved"],
                downloaded_count=state["downloaded"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                log_file=task_log_file,
            )
        except OperationCancelled:
            self._update_task_progress(task_id, state, total_count, finished=True, status="cancelled")
            task_logger.info("Backup task %s cancelled by user", task_id)
            raise
        except TelegramServiceError as exc:
            self._update_task_progress(task_id, state, total_count, finished=True, status="failed")
            task_logger.exception("Backup task %s telegram failure: %s", task_id, exc)
            raise
        except Exception as exc:
            self._update_task_progress(task_id, state, total_count, finished=True, status="failed")
            task_logger.exception("Backup task %s failed: %s", task_id, exc)
            raise BackupServiceError("消息备份失败，请查看 download.log") from exc

    def _task_logger(self, task_id: str) -> logging.Logger:
        if self._task_logger_factory is None:
            return self._logger
        return self._task_logger_factory(task_id, "backup")

    @staticmethod
    def _normal_message_id_set(message_ids: Optional[list[int]]) -> set[int]:
        selected_ids: set[int] = set()
        for raw_id in message_ids or []:
            try:
                message_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if message_id > 0:
                selected_ids.add(message_id)
        return selected_ids

    def _min_message_id_for_backup(
        self,
        tg_chat_id: int,
        limit: int,
        chat_last_backup_message_id: Optional[int],
        incremental: bool,
        has_selected_ids: bool,
    ) -> int:
        """Return the lower message-id bound for one backup request."""
        if has_selected_ids or not incremental:
            return 0

        last_message_id = int(chat_last_backup_message_id or 0)
        if last_message_id <= 0:
            return 0

        local_count = self._message_repository.count_messages(int(tg_chat_id))
        if local_count < max(1, int(limit)):
            self._logger.info(
                "Incremental backup cursor ignored because local cache is incomplete: chat_id=%s local_count=%s requested_limit=%s last_backup_message_id=%s",
                tg_chat_id,
                local_count,
                limit,
                last_message_id,
            )
            return 0
        return last_message_id

    def _task_log_file(self, task_id: str) -> str:
        if self._task_log_path_factory is None:
            return self._log_file
        return str(self._task_log_path_factory(task_id, "backup"))

    def _fetch_chat_messages(
        self,
        api_id: str,
        api_hash: str,
        tg_chat_id: int,
        limit: int,
        min_message_id: int,
        date_from: str,
        date_to: str,
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramArchivedMessage]:
        check_cancelled(cancel_token)
        fetch_method = self._telegram_service.fetch_chat_messages
        if self._callable_accepts_cancel_token(fetch_method):
            return fetch_method(
                api_id=api_id,
                api_hash=api_hash,
                tg_chat_id=tg_chat_id,
                limit=limit,
                min_message_id=min_message_id,
                date_from=date_from,
                date_to=date_to,
                cancel_token=cancel_token,
            )
        return fetch_method(
            api_id=api_id,
            api_hash=api_hash,
            tg_chat_id=tg_chat_id,
            limit=limit,
            min_message_id=min_message_id,
            date_from=date_from,
            date_to=date_to,
        )

    @staticmethod
    def _callable_accepts_cancel_token(callable_object: Callable) -> bool:
        try:
            parameters = inspect.signature(callable_object).parameters
        except (TypeError, ValueError):
            return False
        if "cancel_token" in parameters:
            return True
        return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())

    def _update_task_progress(
        self,
        task_id: str,
        state: dict[str, int],
        total_count: int,
        finished: bool,
        status: str = "running",
    ) -> None:
        self._task_repository.update_progress(
            task_id=task_id,
            status=status,
            success_count=state["saved"],
            failed_count=state["failed"],
            skipped_count=state["skipped"],
            progress=self._progress_percent(state["done"], total_count),
            finished=finished,
        )

    @staticmethod
    def _message_record_from_archived(archived: TelegramArchivedMessage) -> MessageRecord:
        return MessageRecord(
            id=None,
            tg_chat_id=archived.tg_chat_id,
            message_id=archived.message_id,
            sender_id=archived.sender_id,
            sender_name=archived.sender_name,
            date=archived.date,
            text=archived.text,
            text_preview=archived.text_preview,
            message_type=archived.message_type,
            has_media=archived.has_media,
            media_type=archived.media_type,
            media_id=archived.media_id,
            file_name=archived.file_name,
            file_size=archived.file_size,
            is_protected=archived.is_protected,
            source_link=archived.source_link,
            external_urls=BackupService._join_external_urls(archived),
        )

    @staticmethod
    def _join_external_urls(archived: TelegramArchivedMessage) -> str:
        """Return Telegram message URLs that are not the native source permalink."""
        urls: list[str] = []
        for raw_url in getattr(archived, "external_urls", ()) or ():
            url = str(raw_url or "").strip()
            if url:
                urls.append(url)
        webpage_url = str(getattr(archived, "webpage_url", "") or "").strip()
        if webpage_url:
            urls.append(webpage_url)

        values: list[str] = []
        seen: set[str] = set()
        for url in urls:
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(url)
        return "\n".join(values)

    @staticmethod
    def _file_record_from_message(message: MessageRecord) -> FileRecord:
        file_name = message.file_name or f"{message.tg_chat_id}_{message.message_id}"
        return FileRecord(
            id=None,
            message_db_id=message.id,
            tg_chat_id=message.tg_chat_id,
            message_id=message.message_id,
            file_name=file_name,
            file_ext=Path(file_name).suffix,
            file_size=message.file_size,
            local_path=message.local_path,
            file_hash="",
            download_status="pending",
        )

    @staticmethod
    def _new_task_id() -> str:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"backup_{timestamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _progress_percent(done_count: int, total_count: int) -> int:
        if total_count <= 0:
            return 100
        return int(min(100, max(0, round(done_count * 100 / total_count))))
