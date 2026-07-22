"""QThread worker wrappers for Telegram login operations."""

from __future__ import annotations

import logging
from typing import Any, Dict

from PySide6.QtCore import QObject, Signal, Slot

from services.telegram_service import (
    TelegramDependencyError,
    TelegramLoginError,
    TelegramPasswordRequired,
    TelegramService,
    TelegramServiceError,
)


class TelegramLoginWorker(QObject):
    """Run one Telegram login operation in a Qt worker thread."""

    status_changed = Signal(str)
    code_sent = Signal(object)
    account_ready = Signal(object)
    password_required = Signal(str)
    logout_completed = Signal()
    failed = Signal(str, str)
    finished = Signal()

    def __init__(
        self,
        service: TelegramService,
        operation: str,
        payload: Dict[str, Any],
        logger: logging.Logger,
        parent=None,
    ):
        super().__init__(parent)
        self._service = service
        self._operation = operation
        self._payload = dict(payload)
        self._logger = logger

    @Slot()
    def run(self) -> None:
        """Execute the requested operation and emit Qt signals for the UI."""
        try:
            if self._operation == "send_code":
                self.status_changed.emit("正在发送验证码...")
                result = self._service.send_code(
                    api_id=self._payload.get("api_id", ""),
                    api_hash=self._payload.get("api_hash", ""),
                    phone=self._payload.get("phone", ""),
                )
                self.code_sent.emit(result)
                self.status_changed.emit("验证码已发送")
            elif self._operation == "sign_in_code":
                self.status_changed.emit("正在验证验证码...")
                account = self._service.sign_in_with_code(self._payload.get("code", ""))
                self.account_ready.emit(account)
                self.status_changed.emit("登录成功")
            elif self._operation == "sign_in_password":
                self.status_changed.emit("正在提交二步验证...")
                account = self._service.sign_in_with_password(self._payload.get("password", ""))
                self.account_ready.emit(account)
                self.status_changed.emit("登录成功")
            elif self._operation == "restore_session":
                self.status_changed.emit("正在恢复 session...")
                account = self._service.restore_session(
                    api_id=self._payload.get("api_id", ""),
                    api_hash=self._payload.get("api_hash", ""),
                )
                self.account_ready.emit(account)
                self.status_changed.emit("session 已恢复")
            elif self._operation == "logout":
                self.status_changed.emit("正在退出登录...")
                self._service.logout(
                    api_id=self._payload.get("api_id", ""),
                    api_hash=self._payload.get("api_hash", ""),
                )
                self.logout_completed.emit()
                self.status_changed.emit("已退出登录")
            else:
                raise TelegramLoginError(f"未知 Telegram 操作：{self._operation}")
        except TelegramPasswordRequired as exc:
            self.password_required.emit(str(exc))
        except TelegramDependencyError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except TelegramServiceError as exc:
            self.failed.emit(exc.error_code, str(exc))
        except Exception as exc:
            self._logger.exception("Unexpected Telegram worker failure: %s", exc)
            self.failed.emit("TG000", "Telegram 操作异常，请查看 error.log")
        finally:
            self.finished.emit()
