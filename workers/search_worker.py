"""QThread worker for public search tasks."""

from __future__ import annotations

import logging
from typing import Any, Dict

from PySide6.QtCore import QObject, Signal, Slot

from providers.base_provider import SearchProviderError, SearchProviderVerificationRequired
from services.cancellation import CancellationToken, OperationCancelled
from services.download_service import (
    DownloadService,
    DownloadServiceError,
    SearchResultDownloadProgress,
    format_download_progress_metrics,
)
from services.public_search_service import PublicSearchError, PublicSearchService
from services.telegram_service import TelegramServiceError


class PublicSearchWorker(QObject):
    """Run a public search task in a background Qt thread."""

    status_changed = Signal(str)
    search_completed = Signal(object)
    verification_required = Signal(object)
    cancelled = Signal(str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: PublicSearchService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger
        self._cancel_token = CancellationToken()

    @Slot()
    def cancel(self) -> None:
        """Request cooperative cancellation for the running search task."""
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        """Execute public search and emit results to the UI thread."""
        try:
            self.status_changed.emit("正在执行公开搜索...")
            report = self._service.search(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
                engine_name=self._payload.get("engine_name", "jisou"),
                keyword=self._payload.get("keyword", ""),
                max_results=int(self._payload.get("max_results", 100)),
                cancel_token=self._cancel_token,
            )
            self.search_completed.emit(report)
            self.status_changed.emit(f"搜索完成：保存 {report.total_saved} 条")
        except SearchProviderVerificationRequired as exc:
            self.status_changed.emit("搜索 Bot 要求人机验证")
            self.verification_required.emit(self._verification_payload(exc))
        except OperationCancelled as exc:
            self.status_changed.emit(str(exc))
            self.cancelled.emit(exc.error_code, str(exc))
        except PublicSearchError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except SearchProviderError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected public search failure: %s", exc)
            self.failed.emit("SE000", "公开搜索异常，请查看 error.log")
        finally:
            self.finished.emit()

    @staticmethod
    def _verification_payload(exc: SearchProviderVerificationRequired) -> Dict[str, Any]:
        return {
            "error_code": exc.error_code,
            "message": str(exc),
            "bot_username": exc.bot_username,
            "message_id": exc.message_id,
            "prompt": exc.prompt,
            "options": list(exc.options),
            "media_path": exc.media_path,
            "task_id": exc.task_id,
            "keyword": exc.keyword,
            "engine_name": exc.engine_name,
            "max_results": exc.max_results,
            "log_file": exc.log_file,
        }


class VerificationClickWorker(QObject):
    """Submit one user-selected bot verification button in a background thread."""

    status_changed = Signal(str)
    verification_completed = Signal(object)
    verification_required = Signal(object)
    cancelled = Signal(str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: PublicSearchService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger
        self._cancel_token = CancellationToken()

    @Slot()
    def cancel(self) -> None:
        """Request cooperative cancellation for the running verification task."""
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        """Submit the selected bot button and emit search results when verification succeeds."""
        try:
            self.status_changed.emit("正在提交 Bot 人机验证...")
            report = self._service.submit_verification(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
                engine_name=self._payload.get("engine_name", "jisou"),
                keyword=self._payload.get("keyword", ""),
                max_results=int(self._payload.get("max_results", 100)),
                task_id=int(self._payload.get("task_id", 0)),
                message_id=int(self._payload.get("message_id", 0)),
                button_text=self._payload.get("button_text", ""),
                cancel_token=self._cancel_token,
            )
            self.verification_completed.emit(report)
            self.status_changed.emit(f"验证通过，搜索完成：保存 {report.total_saved} 条")
        except SearchProviderVerificationRequired as exc:
            self.status_changed.emit("Bot 仍要求人机验证")
            self.verification_required.emit(PublicSearchWorker._verification_payload(exc))
        except OperationCancelled as exc:
            self.status_changed.emit(str(exc))
            self.cancelled.emit(exc.error_code, str(exc))
        except PublicSearchError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except SearchProviderError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected bot verification submit failure: %s", exc)
            self.failed.emit("SE000", "Bot 验证提交异常，请查看 error.log")
        finally:
            self.finished.emit()


class SearchResultDownloadWorker(QObject):
    """Download media for selected search results in a background thread."""

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
        """Download selected result media and emit progress to the UI thread."""
        try:
            self.status_changed.emit("正在下载选中搜索结果媒体...")

            def on_progress(progress: SearchResultDownloadProgress) -> None:
                self.progress_changed.emit(progress)
                metrics = format_download_progress_metrics(progress)
                self.status_changed.emit(
                    f"下载进度：{metrics}，已处理 {progress.done_count}/{progress.total_count}，"
                    f"成功 {progress.success_count}，失败 {progress.failed_count}，跳过 {progress.skipped_count}"
                )

            report = self._service.download_search_results_media(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
                results=list(self._payload.get("results", []) or []),
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
            self._logger.exception("Unexpected search result media download failure: %s", exc)
            self.failed.emit("DL001", "搜索结果媒体下载异常，请查看 download.log")
        finally:
            self.finished.emit()
