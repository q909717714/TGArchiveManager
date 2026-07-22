"""QThread worker for Telegram chat synchronization."""

from __future__ import annotations

import logging
from typing import Any, Dict

from PySide6.QtCore import QObject, Signal, Slot

from services.telegram_service import TelegramDependencyError, TelegramService, TelegramServiceError


class ChatSyncWorker(QObject):
    """Synchronize Telegram dialogs in a background Qt thread."""

    status_changed = Signal(str)
    chats_synced = Signal(object)
    failed = Signal(str, str)
    finished = Signal()

    def __init__(self, service: TelegramService, payload: Dict[str, Any], logger: logging.Logger, parent=None):
        super().__init__(parent)
        self._service = service
        self._payload = dict(payload)
        self._logger = logger

    @Slot()
    def run(self) -> None:
        """Run chat synchronization and emit results to the UI thread."""
        try:
            self.status_changed.emit("正在同步聊天列表...")
            chats = self._service.sync_chats(
                api_id=self._payload.get("api_id", ""),
                api_hash=self._payload.get("api_hash", ""),
            )
            self.chats_synced.emit(chats)
            self.status_changed.emit(f"同步完成：{len(chats)} 个聊天")
        except TelegramDependencyError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected chat sync failure: %s", exc)
            self.failed.emit("TG000", "聊天同步异常，请查看 error.log")
        finally:
            self.finished.emit()
