"""Media download service for backed-up Telegram messages."""

from __future__ import annotations

import inspect
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, unquote, urlparse
from urllib.request import Request, urlopen

from database.models import DownloadRecord, FileRecord, MessageRecord, SearchResult, TelegraphImage
from database.repositories import DownloadRecordRepository, FileRepository, MessageRepository, TelegraphRepository
from services.cancellation import CancellationToken, OperationCancelled, check_cancelled
from services.telegram_service import TelegramMediaDownloadResult, TelegramService, TelegramServiceError
from services.telegraph_service import TelegraphService
from utils.error_codes import DL001, DL002


class DownloadServiceError(RuntimeError):
    """Raised when media download setup is invalid."""

    error_code = DL001


@dataclass(frozen=True)
class DownloadProgressSnapshot:
    """In-flight byte or image progress for the current download item."""

    status: str = "downloading"
    message: str = ""
    downloaded_bytes: int = 0
    current_bytes: int = 0
    current_total_bytes: Optional[int] = None
    image_done_count: int = 0
    image_total_count: int = 0
    item_progress: float = 0.0


@dataclass(frozen=True)
class SearchResultDownloadProgress:
    """Progress emitted after one search result media download attempt."""

    task_id: str
    source_id: Optional[int]
    done_count: int
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    status: str
    message: str
    current_index: int = 0
    progress_percent: int = 0
    downloaded_bytes: int = 0
    current_bytes: int = 0
    current_total_bytes: Optional[int] = None
    image_done_count: int = 0
    image_total_count: int = 0


@dataclass(frozen=True)
class SearchResultDownloadReport:
    """Final report for selected search result media downloads."""

    task_id: str
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int


@dataclass(frozen=True)
class MessageMediaDownloadProgress:
    """Progress emitted after one selected backed-up message download attempt."""

    task_id: str
    message_id: Optional[int]
    done_count: int
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    status: str
    message: str
    current_index: int = 0
    progress_percent: int = 0
    downloaded_bytes: int = 0
    current_bytes: int = 0
    current_total_bytes: Optional[int] = None
    image_done_count: int = 0
    image_total_count: int = 0


@dataclass(frozen=True)
class MessageMediaDownloadReport:
    """Final report for selected backed-up message media downloads."""

    task_id: str
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int


def format_download_megabytes(byte_count: int) -> str:
    """Return a stable MB display string for download progress."""
    try:
        safe_count = max(0, int(byte_count))
    except (TypeError, ValueError):
        safe_count = 0
    return f"{safe_count / 1024 / 1024:.2f} MB"


def format_download_progress_metrics(progress: object) -> str:
    """Format task index, bytes, and Telegraph image counts for UI status text."""
    parts: list[str] = []
    current_index = int(getattr(progress, "current_index", 0) or getattr(progress, "done_count", 0) or 0)
    total_count = int(getattr(progress, "total_count", 0) or 0)
    if current_index > 0 and total_count > 0:
        parts.append(f"当前第 {current_index}/{total_count} 个")
    elif total_count > 0:
        parts.append(f"已处理 {int(getattr(progress, 'done_count', 0) or 0)}/{total_count}")

    downloaded_bytes = int(getattr(progress, "downloaded_bytes", 0) or 0)
    parts.append(f"已下载 {format_download_megabytes(downloaded_bytes)}")

    current_bytes = int(getattr(progress, "current_bytes", 0) or 0)
    current_total_bytes = getattr(progress, "current_total_bytes", None)
    if current_total_bytes:
        parts.append(
            f"当前文件 {format_download_megabytes(current_bytes)}/{format_download_megabytes(int(current_total_bytes))}"
        )
    elif current_bytes > 0:
        parts.append(f"当前文件 {format_download_megabytes(current_bytes)}")

    image_total_count = int(getattr(progress, "image_total_count", 0) or 0)
    if image_total_count > 0:
        image_done_count = int(getattr(progress, "image_done_count", 0) or 0)
        parts.append(f"图片 {image_done_count}/{image_total_count} 张")

    return "，".join(parts)


class DownloadService:
    """Download media for backed-up messages."""

    DOWNLOAD_LINK_PATTERN = re.compile(r"tgarchive://download\?[^\s<>()\"']+", re.IGNORECASE)

    def __init__(
        self,
        telegram_service: TelegramService,
        message_repository: MessageRepository,
        file_repository: FileRepository,
        download_record_repository: DownloadRecordRepository,
        download_root: Path,
        logger: logging.Logger,
        telegraph_repository: Optional[TelegraphRepository] = None,
        telegraph_service: Optional[TelegraphService] = None,
        download_options: Optional[dict[str, object]] = None,
    ):
        self._telegram_service = telegram_service
        self._message_repository = message_repository
        self._file_repository = file_repository
        self._download_record_repository = download_record_repository
        self._download_root = Path(download_root)
        self._logger = logger
        self._telegraph_repository = telegraph_repository
        self._telegraph_service = telegraph_service
        options = dict(download_options or {})
        self._download_images = bool(options.get("download_images", True))
        self._download_videos = bool(options.get("download_videos", True))
        self._download_documents = bool(options.get("download_documents", True))
        self._download_audio = bool(options.get("download_audio", True))
        self._skip_existing = bool(options.get("skip_existing", True))
        self._max_file_size_bytes = self._max_file_size_bytes_from_options(options.get("max_file_size_mb", 0))

    def download_message_media(
        self,
        api_id: str,
        api_hash: str,
        task_id: str,
        message: MessageRecord,
        file_record: FileRecord,
        retry_count: int = 3,
        progress_callback: Optional[Callable[[DownloadProgressSnapshot], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        """Download one message media file and persist file/download status."""
        check_cancelled(cancel_token)
        if not message.has_media:
            result = TelegramMediaDownloadResult(
                tg_chat_id=message.tg_chat_id,
                message_id=message.message_id,
                status="skipped",
                error_code=DL002,
                error_message="消息没有媒体",
            )
            self._persist_result(task_id, message, file_record, result)
            return result

        policy_skip = self._policy_skip_reason(
            media_type=message.media_type or message.message_type,
            file_name=message.file_name or file_record.file_name,
            file_size=message.file_size if message.file_size is not None else file_record.file_size,
            local_path=file_record.local_path,
        )
        if policy_skip:
            result = TelegramMediaDownloadResult(
                tg_chat_id=message.tg_chat_id,
                message_id=message.message_id,
                status="skipped",
                local_path=file_record.local_path,
                file_name=file_record.file_name,
                file_size=file_record.file_size,
                error_code=DL002,
                error_message=policy_skip,
            )
            self._persist_result(task_id, message, file_record, result)
            return result

        target_dir = self._download_root / str(message.tg_chat_id)
        attempts = max(1, int(retry_count))
        last_result = TelegramMediaDownloadResult(
            tg_chat_id=message.tg_chat_id,
            message_id=message.message_id,
            status="failed",
            error_code=DL001,
            error_message="未执行下载",
        )

        for attempt in range(1, attempts + 1):
            check_cancelled(cancel_token)
            self._logger.info(
                "Downloading media attempt %s/%s: chat_id=%s message_id=%s",
                attempt,
                attempts,
                message.tg_chat_id,
                message.message_id,
            )
            try:
                def on_bytes(current: int, total: Optional[int]) -> None:
                    check_cancelled(cancel_token)
                    if progress_callback is None:
                        return
                    current_bytes = max(0, int(current or 0))
                    total_bytes = int(total) if total else None
                    progress_callback(
                        DownloadProgressSnapshot(
                            status="downloading",
                            message=f"正在下载媒体：#{message.message_id} {message.file_name or message.text_preview}",
                            downloaded_bytes=current_bytes,
                            current_bytes=current_bytes,
                            current_total_bytes=total_bytes,
                            item_progress=self._item_progress(current_bytes, total_bytes),
                        )
                    )

                last_result = self._download_archived_message_media(
                    api_id=api_id,
                    api_hash=api_hash,
                    tg_chat_id=message.tg_chat_id,
                    message_id=message.message_id,
                    download_dir=str(target_dir),
                    progress_callback=on_bytes,
                    cancel_token=cancel_token,
                )
            except OperationCancelled:
                raise
            except TelegramServiceError:
                raise
            if last_result.status == "success":
                break
            if last_result.status == "skipped":
                break

        self._persist_result(task_id, message, file_record, last_result)
        return last_result

    def _download_referenced_message_media(
        self,
        api_id: str,
        api_hash: str,
        task_id: str,
        proxy_message: MessageRecord,
        file_record: FileRecord,
        retry_count: int = 3,
        progress_callback: Optional[Callable[[DownloadProgressSnapshot], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        """Download media referenced by a forwarded TGArchiveManager download URI."""
        check_cancelled(cancel_token)
        attempts = max(1, int(retry_count))
        target_dir = self._download_root / str(file_record.tg_chat_id)
        last_result = TelegramMediaDownloadResult(
            tg_chat_id=file_record.tg_chat_id,
            message_id=file_record.message_id,
            status="failed",
            error_code=DL001,
            error_message="未执行下载",
        )
        policy_skip = self._policy_skip_reason(
            media_type=file_record.file_ext,
            file_name=file_record.file_name,
            file_size=file_record.file_size,
            local_path=file_record.local_path,
        )
        if policy_skip:
            result = TelegramMediaDownloadResult(
                tg_chat_id=file_record.tg_chat_id,
                message_id=file_record.message_id,
                status="skipped",
                local_path=file_record.local_path,
                file_name=file_record.file_name,
                file_size=file_record.file_size,
                error_code=DL002,
                error_message=policy_skip,
            )
            self._persist_result(task_id, proxy_message, file_record, result)
            return result

        for attempt in range(1, attempts + 1):
            check_cancelled(cancel_token)
            self._logger.info(
                "Downloading referenced media attempt %s/%s: source_chat_id=%s source_message_id=%s proxy_chat_id=%s proxy_message_id=%s",
                attempt,
                attempts,
                file_record.tg_chat_id,
                file_record.message_id,
                proxy_message.tg_chat_id,
                proxy_message.message_id,
            )
            try:
                def on_bytes(current: int, total: Optional[int]) -> None:
                    check_cancelled(cancel_token)
                    if progress_callback is None:
                        return
                    current_bytes = max(0, int(current or 0))
                    total_bytes = int(total) if total else None
                    progress_callback(
                        DownloadProgressSnapshot(
                            status="downloading",
                            message=(
                                f"正在下载引用媒体：#{file_record.message_id} "
                                f"{file_record.file_name or proxy_message.text_preview}"
                            ),
                            downloaded_bytes=current_bytes,
                            current_bytes=current_bytes,
                            current_total_bytes=total_bytes,
                            item_progress=self._item_progress(current_bytes, total_bytes),
                        )
                    )

                last_result = self._download_archived_message_media(
                    api_id=api_id,
                    api_hash=api_hash,
                    tg_chat_id=file_record.tg_chat_id,
                    message_id=file_record.message_id,
                    download_dir=str(target_dir),
                    progress_callback=on_bytes,
                    cancel_token=cancel_token,
                )
            except OperationCancelled:
                raise
            except TelegramServiceError:
                raise
            if last_result.status in {"success", "skipped"}:
                break

        self._persist_result(task_id, proxy_message, file_record, last_result)
        return last_result

    def download_search_results_media(
        self,
        api_id: str,
        api_hash: str,
        results: list[SearchResult],
        retry_count: int = 3,
        telegraph_image_limit: int = 0,
        progress_callback: Optional[Callable[[SearchResultDownloadProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> SearchResultDownloadReport:
        """Download media referenced by selected Telegram-native search results."""
        check_cancelled(cancel_token)
        selected_results = [result for result in results if result.id is not None]
        if not selected_results:
            raise DownloadServiceError("请选择要下载媒体的搜索结果")

        task_id = self._new_task_id()
        total_count = len(selected_results)
        state = {
            "done": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "bytes": 0,
            "image_done": 0,
            "image_total": sum(1 for result in selected_results if self._is_image_result(result)),
        }
        attempts = max(1, int(retry_count))
        image_limit = max(0, int(telegraph_image_limit))

        for result in selected_results:
            check_cancelled(cancel_token)
            current_index = state["done"] + 1
            current_is_image = self._is_image_result(result)

            def on_item_progress(snapshot: DownloadProgressSnapshot) -> None:
                check_cancelled(cancel_token)
                if progress_callback is None:
                    return
                image_done_count = max(0, int(snapshot.image_done_count))
                image_total_count = max(0, int(snapshot.image_total_count))
                if image_total_count == 0 and state["image_total"] > 0:
                    image_done_count = state["image_done"]
                    image_total_count = state["image_total"]
                progress_callback(
                    SearchResultDownloadProgress(
                        task_id=task_id,
                        source_id=result.id,
                        done_count=state["done"],
                        total_count=total_count,
                        success_count=state["success"],
                        failed_count=state["failed"],
                        skipped_count=state["skipped"],
                        status=snapshot.status,
                        message=snapshot.message or self._download_progress_message(result, TelegramMediaDownloadResult(
                            tg_chat_id=int(result.tg_chat_id or 0),
                            message_id=int(result.tg_message_id or 0),
                            status=snapshot.status,
                        )),
                        current_index=current_index,
                        progress_percent=self._overall_progress_percent(
                            state["done"],
                            total_count,
                            snapshot.item_progress,
                        ),
                        downloaded_bytes=state["bytes"] + max(0, int(snapshot.downloaded_bytes)),
                        current_bytes=max(0, int(snapshot.current_bytes)),
                        current_total_bytes=snapshot.current_total_bytes,
                        image_done_count=image_done_count,
                        image_total_count=image_total_count,
                    )
                )

            try:
                download_result = self._download_search_result_media(
                    api_id,
                    api_hash,
                    task_id,
                    result,
                    attempts,
                    image_limit,
                    progress_callback=on_item_progress,
                    cancel_token=cancel_token,
                )
            except OperationCancelled:
                raise
            except TelegramServiceError:
                raise
            except Exception as exc:
                self._logger.exception("Search result media download failed: result_id=%s %s", result.id, exc)
                download_result = TelegramMediaDownloadResult(
                    tg_chat_id=int(result.tg_chat_id or 0),
                    message_id=int(result.tg_message_id or 0),
                    status="failed",
                    error_code=DL001,
                    error_message=str(exc)[:240],
                )
                self._persist_search_result_download(task_id, None, download_result)

            if download_result.status == "success":
                state["success"] += 1
            elif download_result.status == "skipped":
                state["skipped"] += 1
            else:
                state["failed"] += 1
            downloaded_bytes = self._result_downloaded_bytes(download_result)
            state["bytes"] += downloaded_bytes
            if current_is_image and download_result.status == "success":
                state["image_done"] += 1
            state["done"] += 1
            image_done_count = download_result.downloaded_image_count
            image_total_count = download_result.image_count
            if image_total_count == 0 and state["image_total"] > 0:
                image_done_count = state["image_done"]
                image_total_count = state["image_total"]

            if progress_callback is not None:
                progress_callback(
                    SearchResultDownloadProgress(
                        task_id=task_id,
                        source_id=result.id,
                        done_count=state["done"],
                        total_count=total_count,
                        success_count=state["success"],
                        failed_count=state["failed"],
                        skipped_count=state["skipped"],
                        status=download_result.status,
                        message=self._download_progress_message(result, download_result),
                        current_index=state["done"],
                        progress_percent=self._overall_progress_percent(state["done"], total_count),
                        downloaded_bytes=state["bytes"],
                        current_bytes=downloaded_bytes,
                        current_total_bytes=download_result.file_size,
                        image_done_count=image_done_count,
                        image_total_count=image_total_count,
                    )
                )

        return SearchResultDownloadReport(
            task_id=task_id,
            total_count=total_count,
            success_count=state["success"],
            failed_count=state["failed"],
            skipped_count=state["skipped"],
        )

    def download_message_records_media(
        self,
        api_id: str,
        api_hash: str,
        messages: list[MessageRecord],
        retry_count: int = 3,
        telegraph_image_limit: int = 0,
        progress_callback: Optional[Callable[[MessageMediaDownloadProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> MessageMediaDownloadReport:
        """Download media for selected locally backed-up messages."""
        check_cancelled(cancel_token)
        selected_messages = [message for message in messages if message.id is not None]
        if not selected_messages:
            raise DownloadServiceError("请选择需要下载媒体的本地消息")

        task_id = self._new_task_id()
        total_count = len(selected_messages)
        state = {
            "done": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "bytes": 0,
            "image_done": 0,
            "image_total": sum(1 for message in selected_messages if self._is_image_message(message)),
        }
        attempts = max(1, int(retry_count))
        image_limit = max(0, int(telegraph_image_limit))

        for message in selected_messages:
            check_cancelled(cancel_token)
            current_index = state["done"] + 1
            current_is_image = self._is_image_message(message)

            def on_item_progress(snapshot: DownloadProgressSnapshot) -> None:
                check_cancelled(cancel_token)
                if progress_callback is None:
                    return
                image_done_count = max(0, int(snapshot.image_done_count))
                image_total_count = max(0, int(snapshot.image_total_count))
                if image_total_count == 0 and state["image_total"] > 0:
                    image_done_count = state["image_done"]
                    image_total_count = state["image_total"]
                progress_callback(
                    MessageMediaDownloadProgress(
                        task_id=task_id,
                        message_id=message.message_id,
                        done_count=state["done"],
                        total_count=total_count,
                        success_count=state["success"],
                        failed_count=state["failed"],
                        skipped_count=state["skipped"],
                        status=snapshot.status,
                        message=snapshot.message,
                        current_index=current_index,
                        progress_percent=self._overall_progress_percent(
                            state["done"],
                            total_count,
                            snapshot.item_progress,
                        ),
                        downloaded_bytes=state["bytes"] + max(0, int(snapshot.downloaded_bytes)),
                        current_bytes=max(0, int(snapshot.current_bytes)),
                        current_total_bytes=snapshot.current_total_bytes,
                        image_done_count=image_done_count,
                        image_total_count=image_total_count,
                    )
                )

            try:
                if not message.has_media:
                    telegraph_url = self._telegraph_url_from_message(message)
                    if telegraph_url:
                        download_result = self._download_message_telegraph_page_images(
                            task_id=task_id,
                            message=message,
                            telegraph_url=telegraph_url,
                            retry_count=attempts,
                            telegraph_image_limit=image_limit,
                            progress_callback=on_item_progress,
                            cancel_token=cancel_token,
                        )
                    else:
                        download_reference = self._download_reference_from_message(message)
                        if download_reference is None:
                            download_result = TelegramMediaDownloadResult(
                                tg_chat_id=message.tg_chat_id,
                                message_id=message.message_id,
                                status="skipped",
                                error_code=DL002,
                                error_message="消息没有媒体",
                            )
                            self._persist_message_download_without_file(task_id, message, download_result)
                        else:
                            reference_chat_id, reference_message_id = download_reference
                            file_record = self._file_record_for_download_reference(
                                message,
                                reference_chat_id,
                                reference_message_id,
                            )
                            download_result = self._download_referenced_message_media(
                                api_id=api_id,
                                api_hash=api_hash,
                                task_id=task_id,
                                proxy_message=message,
                                file_record=file_record,
                                retry_count=attempts,
                                progress_callback=on_item_progress,
                                cancel_token=cancel_token,
                            )
                else:
                    file_record = self._file_record_for_message(message)
                    download_result = self.download_message_media(
                        api_id=api_id,
                        api_hash=api_hash,
                        task_id=task_id,
                        message=message,
                        file_record=file_record,
                        retry_count=attempts,
                        progress_callback=on_item_progress,
                        cancel_token=cancel_token,
                    )
            except OperationCancelled:
                raise
            except TelegramServiceError:
                raise
            except Exception as exc:
                self._logger.exception(
                    "Selected message media download failed: chat_id=%s message_id=%s %s",
                    message.tg_chat_id,
                    message.message_id,
                    exc,
                )
                download_result = TelegramMediaDownloadResult(
                    tg_chat_id=message.tg_chat_id,
                    message_id=message.message_id,
                    status="failed",
                    error_code=DL001,
                    error_message=str(exc)[:240],
                )
                file_record = self._file_record_for_message(message)
                self._persist_result(task_id, message, file_record, download_result)

            if download_result.status == "success":
                state["success"] += 1
            elif download_result.status == "skipped":
                state["skipped"] += 1
            else:
                state["failed"] += 1
            downloaded_bytes = self._result_downloaded_bytes(download_result)
            state["bytes"] += downloaded_bytes
            if current_is_image and download_result.status == "success":
                state["image_done"] += 1
            state["done"] += 1
            image_done_count = download_result.downloaded_image_count
            image_total_count = download_result.image_count
            if image_total_count == 0 and state["image_total"] > 0:
                image_done_count = state["image_done"]
                image_total_count = state["image_total"]

            if progress_callback is not None:
                progress_callback(
                    MessageMediaDownloadProgress(
                        task_id=task_id,
                        message_id=message.message_id,
                        done_count=state["done"],
                        total_count=total_count,
                        success_count=state["success"],
                        failed_count=state["failed"],
                        skipped_count=state["skipped"],
                        status=download_result.status,
                        message=self._message_download_progress_message(message, download_result),
                        current_index=state["done"],
                        progress_percent=self._overall_progress_percent(state["done"], total_count),
                        downloaded_bytes=state["bytes"],
                        current_bytes=downloaded_bytes,
                        current_total_bytes=download_result.file_size,
                        image_done_count=image_done_count,
                        image_total_count=image_total_count,
                    )
                )

        return MessageMediaDownloadReport(
            task_id=task_id,
            total_count=total_count,
            success_count=state["success"],
            failed_count=state["failed"],
            skipped_count=state["skipped"],
        )

    def _persist_result(
        self,
        task_id: str,
        message: MessageRecord,
        file_record: FileRecord,
        result: TelegramMediaDownloadResult,
    ) -> None:
        local_path = result.local_path or file_record.local_path
        file_name = result.file_name or file_record.file_name
        file_ext = Path(file_name).suffix if file_name else file_record.file_ext
        stored_file = FileRecord(
            id=file_record.id,
            message_db_id=file_record.message_db_id if file_record.message_db_id is not None else message.id,
            tg_chat_id=file_record.tg_chat_id,
            message_id=file_record.message_id,
            file_name=file_name,
            file_ext=file_ext,
            file_size=result.file_size if result.file_size is not None else file_record.file_size,
            local_path=local_path,
            file_hash=file_record.file_hash,
            download_status=result.status,
            error_code=result.error_code,
            error_message=result.error_message,
        )
        stored_file = self._file_repository.upsert_file_for_message(stored_file)
        if result.status == "success" and message.id is not None:
            self._message_repository.mark_downloaded(message.id, local_path)

        self._download_record_repository.create_record(
            DownloadRecord(
                id=None,
                task_id=task_id,
                message_db_id=message.id,
                file_id=stored_file.id,
                status=result.status,
                local_path=local_path,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        )

    def _download_search_result_media(
        self,
        api_id: str,
        api_hash: str,
        task_id: str,
        result: SearchResult,
        retry_count: int,
        telegraph_image_limit: int = 0,
        progress_callback: Optional[Callable[[DownloadProgressSnapshot], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        check_cancelled(cancel_token)
        if result.result_type == "telegraph_page" or TelegraphService.is_telegraph_url(result.url or result.normalized_url):
            return self._download_telegraph_page_images(
                task_id,
                result,
                retry_count,
                telegraph_image_limit,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
            )

        if result.tg_chat_id is None or result.tg_message_id is None:
            download_result = TelegramMediaDownloadResult(
                tg_chat_id=0,
                message_id=0,
                status="skipped",
                error_code=DL002,
                error_message="搜索结果缺少原始消息定位信息",
            )
            self._persist_search_result_download(task_id, None, download_result)
            return download_result

        existing_file_record = self._file_repository.get_by_message(int(result.tg_chat_id), int(result.tg_message_id))
        file_record = existing_file_record or self._file_repository.upsert_file_for_message(
            FileRecord(
                id=None,
                message_db_id=None,
                tg_chat_id=int(result.tg_chat_id),
                message_id=int(result.tg_message_id),
                file_name=self._file_name_from_search_result(result),
                file_ext="",
                file_size=None,
                local_path="",
                file_hash="",
                download_status="pending",
            )
        )
        policy_skip = self._policy_skip_reason(
            media_type=result.result_type,
            file_name=file_record.file_name,
            file_size=file_record.file_size,
            local_path=file_record.local_path,
        )
        if policy_skip:
            download_result = TelegramMediaDownloadResult(
                tg_chat_id=int(result.tg_chat_id),
                message_id=int(result.tg_message_id),
                status="skipped",
                local_path=file_record.local_path,
                file_name=file_record.file_name,
                file_size=file_record.file_size,
                error_code=DL002,
                error_message=policy_skip,
            )
            self._persist_search_result_download(task_id, file_record, download_result)
            return download_result

        target_dir = self._download_root / str(result.tg_chat_id)
        last_result = TelegramMediaDownloadResult(
            tg_chat_id=int(result.tg_chat_id),
            message_id=int(result.tg_message_id),
            status="failed",
            error_code=DL001,
            error_message="未执行下载",
        )
        for attempt in range(1, retry_count + 1):
            check_cancelled(cancel_token)
            self._logger.info(
                "Downloading search result media attempt %s/%s: result_id=%s chat_id=%s message_id=%s",
                attempt,
                retry_count,
                result.id,
                result.tg_chat_id,
                result.tg_message_id,
            )
            def on_bytes(current: int, total: Optional[int]) -> None:
                check_cancelled(cancel_token)
                if progress_callback is None:
                    return
                current_bytes = max(0, int(current or 0))
                total_bytes = int(total) if total else None
                progress_callback(
                    DownloadProgressSnapshot(
                        status="downloading",
                        message=f"正在下载搜索结果媒体：{result.title or result.normalized_url or result.id}",
                        downloaded_bytes=current_bytes,
                        current_bytes=current_bytes,
                        current_total_bytes=total_bytes,
                        item_progress=self._item_progress(current_bytes, total_bytes),
                    )
                )

            last_result = self._download_archived_message_media(
                api_id=api_id,
                api_hash=api_hash,
                tg_chat_id=int(result.tg_chat_id),
                message_id=int(result.tg_message_id),
                download_dir=str(target_dir),
                progress_callback=on_bytes,
                cancel_token=cancel_token,
            )
            if last_result.status in {"success", "skipped"}:
                break

        self._persist_search_result_download(task_id, file_record, last_result)
        return last_result

    def _download_telegraph_page_images(
        self,
        task_id: str,
        result: SearchResult,
        retry_count: int,
        telegraph_image_limit: int,
        progress_callback: Optional[Callable[[DownloadProgressSnapshot], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        """Download images extracted from a Telegraph page card."""
        check_cancelled(cancel_token)
        if not self._download_images:
            download_result = TelegramMediaDownloadResult(
                tg_chat_id=int(result.tg_chat_id or 0),
                message_id=int(result.tg_message_id or 0),
                status="skipped",
                error_code=DL002,
                error_message="当前下载配置已禁用图片下载",
            )
            self._persist_search_result_download(task_id, None, download_result)
            return download_result

        if result.id is None:
            download_result = TelegramMediaDownloadResult(
                tg_chat_id=0,
                message_id=0,
                status="skipped",
                error_code=DL002,
                error_message="Telegraph result has not been saved",
            )
            self._persist_search_result_download(task_id, None, download_result)
            return download_result
        if self._telegraph_repository is None:
            download_result = TelegramMediaDownloadResult(
                tg_chat_id=0,
                message_id=0,
                status="skipped",
                error_code=DL002,
                error_message="Telegraph repository is not configured",
            )
            self._persist_search_result_download(task_id, None, download_result)
            return download_result

        page = self._telegraph_repository.get_page_by_search_result_id(int(result.id))
        if page is None:
            if self._telegraph_service is None:
                download_result = TelegramMediaDownloadResult(
                    tg_chat_id=0,
                    message_id=0,
                    status="skipped",
                    error_code=DL002,
                    error_message="Telegraph page metadata is missing",
                )
                self._persist_search_result_download(task_id, None, download_result)
                return download_result
            try:
                parsed_page = self._telegraph_service.fetch_page(result.url or result.normalized_url)
                page = self._telegraph_repository.upsert_page_for_search_result(
                    int(result.id),
                    parsed_page.page,
                    parsed_page.images,
                    parsed_page.telegram_links,
                )
            except Exception as exc:
                self._logger.exception("Telegraph page request failed: result_id=%s url=%s", result.id, result.url)
                download_result = TelegramMediaDownloadResult(
                    tg_chat_id=0,
                    message_id=0,
                    status="failed",
                    error_code=DL001,
                    error_message=str(exc)[:240],
                )
                self._persist_search_result_download(task_id, None, download_result)
                return download_result

        image_limit = None if int(telegraph_image_limit) <= 0 else int(telegraph_image_limit)
        images = self._telegraph_repository.list_images_for_search_result(int(result.id), limit=image_limit)
        if not images:
            download_result = TelegramMediaDownloadResult(
                tg_chat_id=0,
                message_id=0,
                status="skipped",
                error_code=DL002,
                error_message="Telegraph page has no downloadable images",
            )
            self._persist_search_result_download(task_id, None, download_result)
            return download_result

        target_dir = self._download_root / self._safe_path_name(page.title or result.title, "telegraph_page")
        target_dir.mkdir(parents=True, exist_ok=True)
        attempts = max(1, int(retry_count))
        success_count = 0
        failed_count = 0
        downloaded_bytes = 0
        last_error = ""

        for image_index, image in enumerate(images, start=1):
            check_cancelled(cancel_token)
            image_error = ""
            local_path = ""
            current_image_bytes = 0
            current_image_total: Optional[int] = None
            for attempt in range(1, attempts + 1):
                check_cancelled(cancel_token)
                try:
                    self._logger.info(
                        "Downloading Telegraph image attempt %s/%s: result_id=%s image_id=%s url=%s",
                        attempt,
                        attempts,
                        result.id,
                        image.id,
                        image.url,
                    )
                    def on_image_bytes(current: int, total: Optional[int]) -> None:
                        check_cancelled(cancel_token)
                        nonlocal current_image_bytes, current_image_total
                        current_image_bytes = max(0, int(current or 0))
                        current_image_total = int(total) if total else None
                        if progress_callback is not None:
                            progress_callback(
                                DownloadProgressSnapshot(
                                    status="downloading",
                                    message=f"正在下载 Telegraph 图片页面：{page.title or result.title}",
                                    downloaded_bytes=downloaded_bytes + current_image_bytes,
                                    current_bytes=current_image_bytes,
                                    current_total_bytes=current_image_total,
                                    image_done_count=image_index,
                                    image_total_count=len(images),
                                    item_progress=self._image_item_progress(
                                        image_index,
                                        len(images),
                                        current_image_bytes,
                                        current_image_total,
                                    ),
                                )
                            )

                    local_path = str(self._download_telegraph_image(image, target_dir, on_image_bytes, cancel_token))
                    break
                except OperationCancelled:
                    raise
                except Exception as exc:
                    image_error = str(exc)[:500]
                    self._logger.warning(
                        "Telegraph image download failed: result_id=%s image_id=%s attempt=%s/%s url=%s error=%s",
                        result.id,
                        image.id,
                        attempt,
                        attempts,
                        image.url,
                        exc,
                    )

            if local_path:
                image_size = self._path_size(local_path) or current_image_bytes
                downloaded_bytes += image_size
                success_count += 1
                if image.id is not None:
                    self._telegraph_repository.update_image_download_status(
                        int(image.id),
                        "success",
                        local_path=local_path,
                    )
                self._create_telegraph_download_record(task_id, "success", local_path)
            else:
                failed_count += 1
                last_error = image_error or "Telegraph image download failed"
                if image.id is not None:
                    self._telegraph_repository.update_image_download_status(
                        int(image.id),
                        "failed",
                        error_message=last_error,
                    )
                self._create_telegraph_download_record(task_id, "failed", "", DL001, last_error)
            if progress_callback is not None:
                progress_callback(
                    DownloadProgressSnapshot(
                        status="downloading",
                        message=f"正在下载 Telegraph 图片页面：{page.title or result.title}",
                        downloaded_bytes=downloaded_bytes,
                        current_bytes=self._path_size(local_path) if local_path else current_image_bytes,
                        current_total_bytes=current_image_total,
                        image_done_count=image_index,
                        image_total_count=len(images),
                        item_progress=self._bounded_fraction(image_index, len(images)),
                    )
                )

        if failed_count == 0 and success_count > 0:
            status = "success"
            error_code = ""
            error_message = f"Downloaded {success_count}/{len(images)} Telegraph images"
        elif success_count > 0:
            status = "failed"
            error_code = DL001
            error_message = f"Downloaded {success_count}/{len(images)} Telegraph images, failed {failed_count}"
        else:
            status = "failed"
            error_code = DL001
            error_message = last_error or "Telegraph images download failed"

        return TelegramMediaDownloadResult(
            tg_chat_id=0,
            message_id=0,
            status=status,
            local_path=str(target_dir),
            file_name=page.title or result.title,
            file_size=downloaded_bytes,
            downloaded_image_count=success_count,
            image_count=len(images),
            error_code=error_code,
            error_message=error_message,
        )

    def _download_message_telegraph_page_images(
        self,
        task_id: str,
        message: MessageRecord,
        telegraph_url: str,
        retry_count: int,
        telegraph_image_limit: int,
        progress_callback: Optional[Callable[[DownloadProgressSnapshot], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        """Download Telegraph images referenced by a locally backed-up message."""
        check_cancelled(cancel_token)
        if not self._download_images:
            result = TelegramMediaDownloadResult(
                tg_chat_id=message.tg_chat_id,
                message_id=message.message_id,
                status="skipped",
                error_code=DL002,
                error_message="当前下载配置已禁用图片下载",
            )
            self._persist_message_download_without_file(task_id, message, result)
            return result

        if message.id is None or self._telegraph_repository is None or self._telegraph_service is None:
            result = TelegramMediaDownloadResult(
                tg_chat_id=message.tg_chat_id,
                message_id=message.message_id,
                status="skipped",
                error_code=DL002,
                error_message="Telegraph page download is not configured",
            )
            self._persist_message_download_without_file(task_id, message, result)
            return result

        normalized_url = TelegraphService.normalize_url(telegraph_url)
        page = self._telegraph_repository.get_page_by_message_id(int(message.id), normalized_url)
        if page is None:
            try:
                parsed_page = self._telegraph_service.fetch_page(normalized_url)
                page = self._telegraph_repository.upsert_page_for_message(
                    int(message.id),
                    parsed_page.page,
                    parsed_page.images,
                    parsed_page.telegram_links,
                )
            except Exception as exc:
                self._logger.exception(
                    "Telegraph page request failed for message: message_db_id=%s url=%s",
                    message.id,
                    normalized_url,
                )
                result = TelegramMediaDownloadResult(
                    tg_chat_id=message.tg_chat_id,
                    message_id=message.message_id,
                    status="failed",
                    error_code=DL001,
                    error_message=str(exc)[:240],
                )
                self._persist_message_download_without_file(task_id, message, result)
                return result

        if page.id is None:
            result = TelegramMediaDownloadResult(
                tg_chat_id=message.tg_chat_id,
                message_id=message.message_id,
                status="skipped",
                error_code=DL002,
                error_message="Telegraph page metadata is incomplete",
            )
            self._persist_message_download_without_file(task_id, message, result)
            return result

        image_limit = None if int(telegraph_image_limit) <= 0 else int(telegraph_image_limit)
        images = self._telegraph_repository.list_images_for_page(int(page.id), limit=image_limit)
        if not images:
            result = TelegramMediaDownloadResult(
                tg_chat_id=message.tg_chat_id,
                message_id=message.message_id,
                status="skipped",
                error_code=DL002,
                error_message="Telegraph page has no downloadable images",
            )
            self._persist_message_download_without_file(task_id, message, result)
            return result

        target_dir = self._download_root / self._safe_path_name(page.title or message.text_preview, "telegraph_page")
        target_dir.mkdir(parents=True, exist_ok=True)
        attempts = max(1, int(retry_count))
        success_count = 0
        failed_count = 0
        downloaded_bytes = 0
        last_error = ""

        for image_index, image in enumerate(images, start=1):
            check_cancelled(cancel_token)
            local_path = ""
            image_error = ""
            current_image_bytes = 0
            current_image_total: Optional[int] = None
            for attempt in range(1, attempts + 1):
                check_cancelled(cancel_token)
                try:
                    self._logger.info(
                        "Downloading message Telegraph image attempt %s/%s: message_db_id=%s image_id=%s url=%s",
                        attempt,
                        attempts,
                        message.id,
                        image.id,
                        image.url,
                    )
                    def on_image_bytes(current: int, total: Optional[int]) -> None:
                        check_cancelled(cancel_token)
                        nonlocal current_image_bytes, current_image_total
                        current_image_bytes = max(0, int(current or 0))
                        current_image_total = int(total) if total else None
                        if progress_callback is not None:
                            progress_callback(
                                DownloadProgressSnapshot(
                                    status="downloading",
                                    message=f"正在下载 Telegraph 图片页面：{page.title or message.text_preview}",
                                    downloaded_bytes=downloaded_bytes + current_image_bytes,
                                    current_bytes=current_image_bytes,
                                    current_total_bytes=current_image_total,
                                    image_done_count=image_index,
                                    image_total_count=len(images),
                                    item_progress=self._image_item_progress(
                                        image_index,
                                        len(images),
                                        current_image_bytes,
                                        current_image_total,
                                    ),
                                )
                            )

                    local_path = str(self._download_telegraph_image(image, target_dir, on_image_bytes, cancel_token))
                    break
                except OperationCancelled:
                    raise
                except Exception as exc:
                    image_error = str(exc)[:500]
                    self._logger.warning(
                        "Message Telegraph image download failed: message_db_id=%s image_id=%s attempt=%s/%s url=%s error=%s",
                        message.id,
                        image.id,
                        attempt,
                        attempts,
                        image.url,
                        exc,
                    )

            if local_path:
                image_size = self._path_size(local_path) or current_image_bytes
                downloaded_bytes += image_size
                success_count += 1
                if image.id is not None:
                    self._telegraph_repository.update_image_download_status(
                        int(image.id),
                        "success",
                        local_path=local_path,
                    )
                self._create_telegraph_download_record(task_id, "success", local_path, message_db_id=message.id)
            else:
                failed_count += 1
                last_error = image_error or "Telegraph image download failed"
                if image.id is not None:
                    self._telegraph_repository.update_image_download_status(
                        int(image.id),
                        "failed",
                        error_message=last_error,
                    )
                self._create_telegraph_download_record(
                    task_id,
                    "failed",
                    "",
                    DL001,
                    last_error,
                    message_db_id=message.id,
                )
            if progress_callback is not None:
                progress_callback(
                    DownloadProgressSnapshot(
                        status="downloading",
                        message=f"正在下载 Telegraph 图片页面：{page.title or message.text_preview}",
                        downloaded_bytes=downloaded_bytes,
                        current_bytes=self._path_size(local_path) if local_path else current_image_bytes,
                        current_total_bytes=current_image_total,
                        image_done_count=image_index,
                        image_total_count=len(images),
                        item_progress=self._bounded_fraction(image_index, len(images)),
                    )
                )

        if failed_count == 0 and success_count > 0:
            status = "success"
            error_code = ""
            error_message = f"Downloaded {success_count}/{len(images)} Telegraph images"
        elif success_count > 0:
            status = "failed"
            error_code = DL001
            error_message = f"Downloaded {success_count}/{len(images)} Telegraph images, failed {failed_count}"
        else:
            status = "failed"
            error_code = DL001
            error_message = last_error or "Telegraph images download failed"

        if status == "success" and message.id is not None:
            self._message_repository.mark_downloaded(int(message.id), str(target_dir))
        return TelegramMediaDownloadResult(
            tg_chat_id=message.tg_chat_id,
            message_id=message.message_id,
            status=status,
            local_path=str(target_dir),
            file_name=page.title or message.text_preview,
            file_size=downloaded_bytes,
            downloaded_image_count=success_count,
            image_count=len(images),
            error_code=error_code,
            error_message=error_message,
        )

    def _download_telegraph_image(
        self,
        image: TelegraphImage,
        target_dir: Path,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> Path:
        check_cancelled(cancel_token)
        filename = f"{max(1, int(image.position)):03d}{self._image_suffix(image.url, '')}"
        target_path = target_dir / filename
        if self._skip_existing and target_path.is_file() and self._path_size(str(target_path)) > 0:
            return target_path

        data, content_type = self._fetch_binary_url(
            image.url,
            progress_callback=progress_callback,
            cancel_token=cancel_token,
        )
        check_cancelled(cancel_token)
        if not data:
            raise DownloadServiceError("Empty Telegraph image response")
        suffix = self._image_suffix(image.url, content_type)
        filename = f"{max(1, int(image.position)):03d}{suffix}"
        target_path = target_dir / filename
        target_path.write_bytes(data)
        return target_path

    def _fetch_binary_url(
        self,
        url: str,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> tuple[bytes, str]:
        check_cancelled(cancel_token)
        request = Request(
            url,
            headers={
                "User-Agent": "TGArchiveManager/1.0 (+https://telegra.ph image downloader)",
                "Accept": "image/*,*/*;q=0.8",
            },
        )
        with urlopen(request, timeout=30) as response:
            content_type = str(response.headers.get("Content-Type", "") or "")
            total_header = response.headers.get("Content-Length")
            try:
                total_size = int(total_header) if total_header else None
            except (TypeError, ValueError):
                total_size = None
            if self._max_file_size_bytes > 0 and total_size is not None and total_size > self._max_file_size_bytes:
                raise DownloadServiceError("Telegraph 图片超过下载大小上限")
            chunks: list[bytes] = []
            downloaded = 0
            while True:
                check_cancelled(cancel_token)
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                downloaded += len(chunk)
                if self._max_file_size_bytes > 0 and downloaded > self._max_file_size_bytes:
                    raise DownloadServiceError("Telegraph 图片超过下载大小上限")
                if progress_callback is not None:
                    progress_callback(downloaded, total_size)
            check_cancelled(cancel_token)
            return b"".join(chunks), content_type

    def _download_archived_message_media(
        self,
        api_id: str,
        api_hash: str,
        tg_chat_id: int,
        message_id: int,
        download_dir: str,
        progress_callback: Optional[Callable[[int, Optional[int]], None]],
        cancel_token: Optional[CancellationToken],
    ) -> TelegramMediaDownloadResult:
        check_cancelled(cancel_token)
        download_method = self._telegram_service.download_archived_message_media
        if self._callable_accepts_cancel_token(download_method):
            return download_method(
                api_id=api_id,
                api_hash=api_hash,
                tg_chat_id=tg_chat_id,
                message_id=message_id,
                download_dir=download_dir,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
            )
        return download_method(
            api_id=api_id,
            api_hash=api_hash,
            tg_chat_id=tg_chat_id,
            message_id=message_id,
            download_dir=download_dir,
            progress_callback=progress_callback,
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

    @staticmethod
    def _image_suffix(url: str, content_type: str) -> str:
        suffix = Path(unquote(urlparse(str(url)).path)).suffix.lower()
        if suffix and len(suffix) <= 8:
            return suffix
        guessed = mimetypes.guess_extension(str(content_type).split(";", 1)[0].strip().lower())
        return guessed or ".jpg"

    @staticmethod
    def _safe_path_name(value: str, default: str) -> str:
        raw = str(value or "").strip() or default
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", raw).strip(" ._")
        return (safe or default)[:120]

    def _create_telegraph_download_record(
        self,
        task_id: str,
        status: str,
        local_path: str = "",
        error_code: str = "",
        error_message: str = "",
        message_db_id: Optional[int] = None,
    ) -> None:
        self._download_record_repository.create_record(
            DownloadRecord(
                id=None,
                task_id=task_id,
                message_db_id=message_db_id,
                file_id=None,
                status=status,
                local_path=local_path,
                error_code=error_code,
                error_message=error_message,
            )
        )

    def _file_record_for_message(self, message: MessageRecord) -> FileRecord:
        existing = self._file_repository.get_by_message(message.tg_chat_id, message.message_id)
        if existing is not None:
            return existing

        file_name = message.file_name or f"{message.tg_chat_id}_{message.message_id}"
        return self._file_repository.upsert_file_for_message(
            FileRecord(
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
        )

    def _file_record_for_download_reference(
        self,
        proxy_message: MessageRecord,
        tg_chat_id: int,
        message_id: int,
    ) -> FileRecord:
        existing = self._file_repository.get_by_message(tg_chat_id, message_id)
        if existing is not None:
            return existing

        file_name = f"{tg_chat_id}_{message_id}"
        return self._file_repository.upsert_file_for_message(
            FileRecord(
                id=None,
                message_db_id=proxy_message.id,
                tg_chat_id=int(tg_chat_id),
                message_id=int(message_id),
                file_name=file_name,
                file_ext="",
                file_size=None,
                local_path="",
                file_hash="",
                download_status="pending",
            )
        )

    @classmethod
    def _download_reference_from_message(cls, message: MessageRecord) -> Optional[tuple[int, int]]:
        text = f"{message.text or ''}\n{message.text_preview or ''}\n{message.external_urls or ''}"
        for match in cls.DOWNLOAD_LINK_PATTERN.finditer(text):
            reference = cls._download_reference_from_url(match.group(0).rstrip(".,;，。；)"))
            if reference is not None:
                return reference
        return None

    @staticmethod
    def _telegraph_url_from_message(message: MessageRecord) -> str:
        text = "\n".join(
            [
                message.text or "",
                message.text_preview or "",
                message.source_link or "",
                message.external_urls or "",
            ]
        )
        urls = TelegraphService.extract_telegraph_urls(text)
        return urls[0] if urls else ""

    @staticmethod
    def _download_reference_from_url(url: str) -> Optional[tuple[int, int]]:
        parsed = urlparse(str(url).strip())
        if parsed.scheme.lower() != "tgarchive" or parsed.netloc.lower() != "download":
            return None
        query = dict(parse_qsl(parsed.query, keep_blank_values=False))
        chat_id = query.get("chat_id") or query.get("tg_chat_id")
        message_id = query.get("message_id")
        try:
            parsed_chat_id = int(chat_id)
            parsed_message_id = int(message_id)
        except (TypeError, ValueError):
            return None
        if parsed_chat_id == 0 or parsed_message_id <= 0:
            return None
        return parsed_chat_id, parsed_message_id

    def _persist_message_download_without_file(
        self,
        task_id: str,
        message: MessageRecord,
        result: TelegramMediaDownloadResult,
    ) -> None:
        self._download_record_repository.create_record(
            DownloadRecord(
                id=None,
                task_id=task_id,
                message_db_id=message.id,
                file_id=None,
                status=result.status,
                local_path=result.local_path,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        )

    def _persist_search_result_download(
        self,
        task_id: str,
        file_record: Optional[FileRecord],
        result: TelegramMediaDownloadResult,
    ) -> None:
        stored_file = None
        if file_record is not None:
            file_name = result.file_name or file_record.file_name
            stored_file = self._file_repository.upsert_file_for_message(
                FileRecord(
                    id=file_record.id,
                    message_db_id=file_record.message_db_id,
                    tg_chat_id=file_record.tg_chat_id,
                    message_id=file_record.message_id,
                    file_name=file_name,
                    file_ext=Path(file_name).suffix if file_name else file_record.file_ext,
                    file_size=result.file_size if result.file_size is not None else file_record.file_size,
                    local_path=result.local_path or file_record.local_path,
                    file_hash=file_record.file_hash,
                    download_status=result.status,
                    error_code=result.error_code,
                    error_message=result.error_message,
                )
            )

        self._download_record_repository.create_record(
            DownloadRecord(
                id=None,
                task_id=task_id,
                message_db_id=None,
                file_id=stored_file.id if stored_file is not None else None,
                status=result.status,
                local_path=result.local_path,
                error_code=result.error_code,
                error_message=result.error_message,
            )
        )

    @staticmethod
    def _file_name_from_search_result(result: SearchResult) -> str:
        suffix = str(result.result_type or "").strip()
        base = f"{result.tg_chat_id}_{result.tg_message_id}"
        return f"{base}_{suffix}" if suffix else base

    @staticmethod
    def _is_image_result(result: SearchResult) -> bool:
        return str(result.result_type or "").strip().lower() in {"photo", "image"}

    @staticmethod
    def _is_image_message(message: MessageRecord) -> bool:
        media_type = str(message.media_type or message.message_type or "").strip().lower()
        return media_type in {"photo", "image"}

    def _policy_skip_reason(
        self,
        media_type: str,
        file_name: str = "",
        file_size: Optional[int] = None,
        local_path: str = "",
    ) -> str:
        if self._skip_existing and self._path_size(local_path) > 0:
            return "本地文件已存在"

        category = self._media_category(media_type, file_name)
        if category == "image" and not self._download_images:
            return "当前下载配置已禁用图片下载"
        if category == "video" and not self._download_videos:
            return "当前下载配置已禁用视频下载"
        if category == "audio" and not self._download_audio:
            return "当前下载配置已禁用音频下载"
        if category == "document" and not self._download_documents:
            return "当前下载配置已禁用文件下载"

        try:
            size_value = int(file_size) if file_size is not None else 0
        except (TypeError, ValueError):
            size_value = 0
        if self._max_file_size_bytes > 0 and size_value > self._max_file_size_bytes:
            return f"文件大小超过下载上限 {self._max_file_size_bytes // 1024 // 1024} MB"
        return ""

    @staticmethod
    def _media_category(media_type: str, file_name: str = "") -> str:
        raw = f"{media_type or ''} {Path(str(file_name or '')).suffix.lower()}".lower()
        if any(token in raw for token in ("photo", "image", "jpg", "jpeg", "png", "gif", "webp", "bmp")):
            return "image"
        if any(token in raw for token in ("video", "mp4", "mkv", "mov", "avi", "webm")):
            return "video"
        if any(token in raw for token in ("audio", "voice", "music", "mp3", "m4a", "wav", "ogg", "flac")):
            return "audio"
        if any(token in raw for token in ("document", "file", "pdf", "doc", "xls", "ppt", "zip", "rar", "7z")):
            return "document"
        return "document" if raw.strip() else "media"

    @staticmethod
    def _max_file_size_bytes_from_options(value: object) -> int:
        try:
            mb = float(value)
        except (TypeError, ValueError):
            return 0
        if mb <= 0:
            return 0
        return int(mb * 1024 * 1024)

    @staticmethod
    def _download_progress_message(result: SearchResult, download_result: TelegramMediaDownloadResult) -> str:
        title = result.title or result.normalized_url or str(result.id or "")
        if result.result_type == "telegraph_page":
            if download_result.status == "success":
                return f"已下载 Telegraph 图片页面：{title}（{download_result.error_message}）"
            if download_result.status == "skipped":
                return f"已跳过 Telegraph 图片页面：{title}（{download_result.error_message}）"
            return f"Telegraph 图片页面下载失败：{title}（{download_result.error_message}）"
        if download_result.status == "success":
            return f"已下载：{title}"
        if download_result.status == "skipped":
            return f"已跳过：{title}（{download_result.error_message}）"
        return f"下载失败：{title}（{download_result.error_message}）"

    @staticmethod
    def _message_download_progress_message(
        message: MessageRecord,
        download_result: TelegramMediaDownloadResult,
    ) -> str:
        title = message.text_preview or message.file_name or str(message.message_id)
        if download_result.status == "success":
            return f"已下载：#{message.message_id} {title}"
        if download_result.status == "skipped":
            return f"已跳过：#{message.message_id} {download_result.error_message}"
        return f"下载失败：#{message.message_id} {download_result.error_message}"

    @staticmethod
    def _new_task_id() -> str:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"download_{timestamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _result_downloaded_bytes(result: TelegramMediaDownloadResult) -> int:
        if result.file_size is not None:
            try:
                return max(0, int(result.file_size))
            except (TypeError, ValueError):
                return 0
        return DownloadService._path_size(result.local_path)

    @staticmethod
    def _path_size(path: str) -> int:
        if not path:
            return 0
        target = Path(path)
        if target.is_file():
            try:
                return max(0, int(target.stat().st_size))
            except OSError:
                return 0
        if target.is_dir():
            total_size = 0
            try:
                for child in target.rglob("*"):
                    if child.is_file():
                        total_size += max(0, int(child.stat().st_size))
            except OSError:
                return total_size
            return total_size
        return 0

    @staticmethod
    def _item_progress(current_bytes: int, total_bytes: Optional[int]) -> float:
        if total_bytes is None or total_bytes <= 0:
            return 0.0
        return DownloadService._bounded_fraction(current_bytes, total_bytes)

    @staticmethod
    def _image_item_progress(
        image_index: int,
        total_images: int,
        current_bytes: int,
        total_bytes: Optional[int],
    ) -> float:
        if total_images <= 0:
            return 0.0
        completed_images = max(0, int(image_index) - 1)
        image_fraction = DownloadService._item_progress(current_bytes, total_bytes)
        return DownloadService._bounded_fraction(completed_images + image_fraction, total_images)

    @staticmethod
    def _overall_progress_percent(done_count: int, total_count: int, current_item_progress: float = 0.0) -> int:
        if total_count <= 0:
            return 100
        done = max(0.0, float(done_count))
        current = max(0.0, min(1.0, float(current_item_progress)))
        return int(max(0.0, min(100.0, ((done + current) / float(total_count)) * 100.0)))

    @staticmethod
    def _bounded_fraction(numerator: int | float, denominator: int | float) -> float:
        if denominator <= 0:
            return 0.0
        return max(0.0, min(1.0, float(numerator) / float(denominator)))
