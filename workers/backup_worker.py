"""QThread worker for message backup and media download tasks."""

from __future__ import annotations

import logging
from typing import Any, Dict

from PySide6.QtCore import QObject, Signal, Slot

from services.backup_service import BackupProgress, BackupService, BackupServiceError
from services.cancellation import CancellationToken, OperationCancelled
from services.download_service import (
    DownloadService,
    DownloadServiceError,
    MessageMediaDownloadProgress,
    format_download_progress_metrics,
)
from services.telegram_service import TelegramServiceError
from utils.error_codes import BK000, DL001


class BackupWorker(QObject):
    """Run one chat backup task in a background Qt thread."""

    status_changed = Signal(str)
    progress_changed = Signal(object)
    backup_completed = Signal(object)
    cancelled = Signal(str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: BackupService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger
        self._cancel_token = CancellationToken()

    @Slot()
    def cancel(self) -> None:
        """Request cooperative cancellation for the running backup task."""
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        """Execute backup and emit progress to the UI thread."""
        try:
            downloads_enabled = bool(self._payload.get("download_media", False))
            action_label = "备份下载" if downloads_enabled else "读取预览"
            self.status_changed.emit(f"正在{action_label}消息...")

            def on_progress(progress: BackupProgress) -> None:
                self.progress_changed.emit(progress)
                self.status_changed.emit(
                    f"{action_label}进度：{progress.done_count}/{progress.total_count}，"
                    f"已保存 {progress.saved_count}，已下载 {progress.downloaded_count}，"
                    f"失败 {progress.failed_count}，跳过 {progress.skipped_count}"
                )

            report = self._service.backup_chat(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
                tg_chat_id=int(self._payload.get("tg_chat_id", 0)),
                limit=int(self._payload.get("limit", 100)),
                date_from=self._payload.get("date_from", ""),
                date_to=self._payload.get("date_to", ""),
                incremental=bool(self._payload.get("incremental", True)),
                download_media=bool(self._payload.get("download_media", False)),
                retry_count=int(self._payload.get("retry_count", 3)),
                selected_message_ids=list(self._payload.get("selected_message_ids", []) or []),
                progress_callback=on_progress,
                cancel_token=self._cancel_token,
            )
            self.backup_completed.emit(report)
            self.status_changed.emit(
                f"{action_label}完成：保存 {report.saved_count}，下载 {report.downloaded_count}，"
                f"失败 {report.failed_count}，跳过 {report.skipped_count}"
            )
        except OperationCancelled as exc:
            self.status_changed.emit(str(exc))
            self.cancelled.emit(exc.error_code, str(exc))
        except BackupServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected backup failure: %s", exc)
            self.failed.emit(BK000, "消息备份异常，请查看 download.log")
        finally:
            self.finished.emit()


class MessageMediaDownloadWorker(QObject):
    """Run selected backed-up message media downloads in a background Qt thread."""

    status_changed = Signal(str)
    progress_changed = Signal(object)
    download_completed = Signal(object)
    cancelled = Signal(str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: DownloadService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger
        self._cancel_token = CancellationToken()

    @Slot()
    def cancel(self) -> None:
        """Request cooperative cancellation for the running download task."""
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        """Download selected backed-up message media and emit progress to the UI thread."""
        try:
            self.status_changed.emit("正在下载勾选消息媒体...")

            def on_progress(progress: MessageMediaDownloadProgress) -> None:
                self.progress_changed.emit(progress)
                metrics = format_download_progress_metrics(progress)
                self.status_changed.emit(
                    f"下载进度：{metrics}，已处理 {progress.done_count}/{progress.total_count}，"
                    f"成功 {progress.success_count}，失败 {progress.failed_count}，跳过 {progress.skipped_count}"
                )

            report = self._service.download_message_records_media(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
                messages=list(self._payload.get("messages", []) or []),
                retry_count=int(self._payload.get("retry_count", 3)),
                telegraph_image_limit=int(self._payload.get("telegraph_image_limit", 0)),
                progress_callback=on_progress,
                cancel_token=self._cancel_token,
            )
            self.download_completed.emit(report)
            self.status_changed.emit(
                f"下载完成：成功 {report.success_count}，失败 {report.failed_count}，跳过 {report.skipped_count}"
            )
        except OperationCancelled as exc:
            self.status_changed.emit(str(exc))
            self.cancelled.emit(exc.error_code, str(exc))
        except DownloadServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected selected message media download failure: %s", exc)
            self.failed.emit(DL001, "勾选消息媒体下载异常，请查看 download.log")
        finally:
            self.finished.emit()
