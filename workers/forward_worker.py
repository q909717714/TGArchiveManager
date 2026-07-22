"""QThread workers for text forwarding and target group creation."""

from __future__ import annotations

import logging
from typing import Any, Dict

from PySide6.QtCore import QObject, Signal, Slot

from services.cancellation import CancellationToken, OperationCancelled
from services.forward_service import ForwardProgress, ForwardService, ForwardServiceError
from services.group_service import GroupService, GroupServiceError
from services.telegram_service import TelegramServiceError
from utils.error_codes import FW000, GP000


class ForwardWorker(QObject):
    """Run a forwarding task in a background Qt thread."""

    status_changed = Signal(str)
    progress_changed = Signal(object)
    forward_completed = Signal(object)
    cancelled = Signal(str, str)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: ForwardService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger
        self._cancel_token = CancellationToken()

    @Slot()
    def cancel(self) -> None:
        """Request cooperative cancellation for the running forwarding task."""
        self._cancel_token.cancel()

    @Slot()
    def run(self) -> None:
        """Forward selected records and emit progress."""
        try:
            source_type = str(self._payload.get("source_type", "search_result") or "search_result")
            action_label = "聊天记录" if source_type == "message_record" else "搜索结果卡片"
            self.status_changed.emit(f"正在转发{action_label}...")

            def on_progress(progress: ForwardProgress) -> None:
                self.progress_changed.emit(progress)
                self.status_changed.emit(
                    f"转发进度：{progress.done_count}/{progress.total_count}，"
                    f"成功 {progress.success_count}，失败 {progress.failed_count}，跳过 {progress.skipped_count}"
                )

            target_strategy = str(self._payload.get("target_strategy", "existing") or "existing")
            if source_type == "message_record":
                if target_strategy == "auto_group":
                    report = self._service.forward_message_records_auto_group(
                        api_id=self._payload.get("api_id", ""),
                        api_hash=self._payload.get("api_hash", ""),
                        messages=list(self._payload.get("messages", []) or []),
                        group_title=self._payload.get("group_title", ""),
                        interval_seconds=int(self._payload.get("interval_seconds", 3)),
                        progress_callback=on_progress,
                        cancel_token=self._cancel_token,
                    )
                else:
                    report = self._service.forward_message_records(
                        api_id=self._payload.get("api_id", ""),
                        api_hash=self._payload.get("api_hash", ""),
                        messages=list(self._payload.get("messages", []) or []),
                        target_chat_id=int(self._payload.get("target_chat_id", 0)),
                        interval_seconds=int(self._payload.get("interval_seconds", 3)),
                        progress_callback=on_progress,
                        cancel_token=self._cancel_token,
                    )
            elif target_strategy == "existing":
                report = self._service.forward_search_result_cards(
                    api_id=self._payload.get("api_id", ""),
                    api_hash=self._payload.get("api_hash", ""),
                    result_ids=list(self._payload.get("result_ids", []) or []),
                    target_chat_id=int(self._payload.get("target_chat_id", 0)),
                    interval_seconds=int(self._payload.get("interval_seconds", 3)),
                    skip_duplicates=bool(self._payload.get("skip_duplicates", True)),
                    progress_callback=on_progress,
                    cancel_token=self._cancel_token,
                )
            else:
                report = self._service.forward_search_result_cards_auto_group(
                    api_id=self._payload.get("api_id", ""),
                    api_hash=self._payload.get("api_hash", ""),
                    result_ids=list(self._payload.get("result_ids", []) or []),
                    group_by=target_strategy,
                    group_title_prefix=self._payload.get("group_title_prefix", "TG整理"),
                    interval_seconds=int(self._payload.get("interval_seconds", 3)),
                    skip_duplicates=bool(self._payload.get("skip_duplicates", True)),
                    progress_callback=on_progress,
                    cancel_token=self._cancel_token,
                )
            self.forward_completed.emit(report)
            self.status_changed.emit(
                f"转发完成：成功 {report.success_count}，失败 {report.failed_count}，跳过 {report.skipped_count}"
            )
        except OperationCancelled as exc:
            self.status_changed.emit(str(exc))
            self.cancelled.emit(exc.error_code, str(exc))
        except ForwardServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected forward failure: %s", exc)
            self.failed.emit(FW000, "转发异常，请查看 forward.log")
        finally:
            self.finished.emit()


class GroupCreateWorker(QObject):
    """Create a Telegram target group in a background Qt thread."""

    status_changed = Signal(str)
    group_created = Signal(object)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: GroupService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger

    @Slot()
    def run(self) -> None:
        """Create one target group and emit the resulting chat."""
        try:
            self.status_changed.emit("正在创建目标群...")
            chat = self._service.create_target_group(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
                title=self._payload.get("title", ""),
                category=self._payload.get("category", ""),
            )
            self.group_created.emit(chat)
            self.status_changed.emit(f"目标群已创建：{chat.title}")
        except GroupServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected group creation failure: %s", exc)
            self.failed.emit(GP000, "目标群创建异常，请查看 error.log")
        finally:
            self.finished.emit()
