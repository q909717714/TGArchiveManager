"""Telegram login page for stage 2."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.repositories import AccountRepository
from services.config_service import ConfigError, ConfigService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from services.telegram_service import TelegramAccountInfo, TelegramService
from workers.telegram_worker import TelegramLoginWorker


class LoginPage(QWidget):
    """UI for Telegram user account login and session restore."""

    login_status_changed = Signal(str)
    account_changed = Signal(str)

    def __init__(
        self,
        project_root: Path,
        config_service: ConfigService,
        log_service: LogService,
        database: DatabaseManager,
        runtime_state: RuntimeState,
        parent=None,
    ):
        super().__init__(parent)
        self._project_root = Path(project_root)
        self._config_service = config_service
        self._log_service = log_service
        self._database = database
        self._runtime_state = runtime_state
        self._logger = log_service.get_logger("telegram_login_ui")
        self._account_repository = AccountRepository(database)
        self._telegram_service = TelegramService(
            project_root=self._project_root,
            config=self._config_service.as_dict(),
            account_repository=self._account_repository,
            logger=log_service.get_logger("telegram"),
        )
        self._thread: Optional[QThread] = None
        self._worker: Optional[TelegramLoginWorker] = None
        self._latest_account = None
        self._auto_restore_active = False

        self._build_ui()
        self._load_initial_values()
        QTimer.singleShot(0, self._restore_session_if_available)

    def _build_ui(self) -> None:
        title = QLabel("账号登录")
        title.setObjectName("pageTitle")

        account_group = QGroupBox("Telegram API")
        self._api_id_edit = QLineEdit()
        self._api_id_edit.setPlaceholderText("Telegram api_id")
        self._api_hash_edit = QLineEdit()
        self._api_hash_edit.setPlaceholderText("Telegram api_hash")
        self._api_hash_edit.setEchoMode(QLineEdit.Password)
        self._save_api_button = QPushButton("保存 API 配置")
        self._save_api_button.clicked.connect(lambda: self._save_api_credentials())
        self._phone_edit = QLineEdit()
        self._phone_edit.setPlaceholderText("+8613800000000")

        account_form = QFormLayout(account_group)
        account_form.addRow("API ID", self._api_id_edit)
        account_form.addRow("API Hash", self._api_hash_edit)
        account_form.addRow("", self._save_api_button)
        account_form.addRow("手机号", self._phone_edit)

        auth_group = QGroupBox("登录验证")
        self._code_edit = QLineEdit()
        self._code_edit.setPlaceholderText("短信或 Telegram 验证码")
        self._password_edit = QLineEdit()
        self._password_edit.setEchoMode(QLineEdit.Password)
        self._password_edit.setPlaceholderText("二步验证密码")

        auth_form = QFormLayout(auth_group)
        auth_form.addRow("验证码", self._code_edit)
        auth_form.addRow("二步验证密码", self._password_edit)

        session_group = QGroupBox("当前状态")
        self._status_label = QLabel("未登录")
        self._display_name_label = QLabel("-")
        self._username_label = QLabel("-")
        self._phone_label = QLabel("-")
        self._session_path_label = QLabel(str(self._telegram_service.session_file_path))
        self._session_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        session_form = QFormLayout(session_group)
        session_form.addRow("登录状态", self._status_label)
        session_form.addRow("账号名称", self._display_name_label)
        session_form.addRow("Username", self._username_label)
        session_form.addRow("手机号", self._phone_label)
        session_form.addRow("Session 路径", self._session_path_label)

        self._send_code_button = QPushButton("发送验证码")
        self._sign_in_button = QPushButton("验证码登录")
        self._password_button = QPushButton("提交二步验证")
        self._restore_button = QPushButton("恢复 Session")
        self._logout_button = QPushButton("退出登录")

        self._send_code_button.clicked.connect(self._send_code)
        self._sign_in_button.clicked.connect(self._sign_in_with_code)
        self._password_button.clicked.connect(self._sign_in_with_password)
        self._restore_button.clicked.connect(self._restore_session)
        self._logout_button.clicked.connect(self._logout)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self._send_code_button)
        button_layout.addWidget(self._sign_in_button)
        button_layout.addWidget(self._password_button)
        button_layout.addWidget(self._restore_button)
        button_layout.addWidget(self._logout_button)
        button_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addWidget(account_group)
        layout.addWidget(auth_group)
        layout.addLayout(button_layout)
        layout.addWidget(session_group)
        layout.addStretch(1)

    def _load_initial_values(self) -> None:
        self._api_id_edit.setText(str(self._config_service.get("telegram.api_id", "") or ""))
        self._api_hash_edit.setText(str(self._config_service.get("telegram.api_hash", "") or ""))
        if self._runtime_state.api_id:
            self._api_id_edit.setText(self._runtime_state.api_id)
        if self._runtime_state.api_hash:
            self._api_hash_edit.setText(self._runtime_state.api_hash)
        self._latest_account = self._account_repository.latest_account()
        if self._latest_account:
            self._phone_edit.setText(self._latest_account.phone)
            self._phone_label.setText(self._latest_account.phone)
            self._display_name_label.setText(self._latest_account.display_name or "-")
            self._username_label.setText(self._latest_account.username or "-")
            self._status_label.setText("检测到历史登录记录，可尝试恢复 session")

    def _restore_session_if_available(self) -> None:
        if self._thread is not None:
            return

        api_id = self._api_id_edit.text().strip()
        api_hash = self._api_hash_edit.text().strip()
        if not api_id or not api_hash:
            return
        if not self._known_session_file_exists():
            return

        self._auto_restore_active = True
        self._set_status("检测到历史 session，正在自动恢复...")
        self._start_worker(
            "restore_session",
            {
                "api_id": api_id,
                "api_hash": api_hash,
            },
        )

    def _known_session_file_exists(self) -> bool:
        candidates = [self._telegram_service.session_file_path]
        if self._latest_account and self._latest_account.session_path:
            account_session_path = Path(self._latest_account.session_path)
            if not account_session_path.is_absolute():
                account_session_path = self._project_root / account_session_path
            candidates.append(account_session_path)

        return any(path.exists() for path in candidates)

    def _send_code(self) -> None:
        self._auto_restore_active = False
        if not self._save_api_credentials(show_success=False):
            return
        self._start_worker(
            "send_code",
            {
                "api_id": self._api_id_edit.text(),
                "api_hash": self._api_hash_edit.text(),
                "phone": self._phone_edit.text(),
            },
        )

    def _sign_in_with_code(self) -> None:
        self._auto_restore_active = False
        self._start_worker("sign_in_code", {"code": self._code_edit.text()})

    def _sign_in_with_password(self) -> None:
        self._auto_restore_active = False
        self._start_worker("sign_in_password", {"password": self._password_edit.text()})

    def _restore_session(self) -> None:
        self._auto_restore_active = False
        if not self._save_api_credentials(show_success=False):
            return
        self._start_worker(
            "restore_session",
            {
                "api_id": self._api_id_edit.text(),
                "api_hash": self._api_hash_edit.text(),
            },
        )

    def _logout(self) -> None:
        self._auto_restore_active = False
        if not self._save_api_credentials(show_success=False):
            return
        self._start_worker(
            "logout",
            {
                "api_id": self._api_id_edit.text(),
                "api_hash": self._api_hash_edit.text(),
            },
        )

    def _save_api_credentials(self, show_success: bool = True) -> bool:
        try:
            self._config_service.save_telegram_api_credentials(
                self._api_id_edit.text(),
                self._api_hash_edit.text(),
            )
        except ConfigError as exc:
            QMessageBox.warning(self, "API 配置未保存", str(exc))
            return False
        except OSError as exc:
            self._logger.exception("Failed to save Telegram API configuration: %s", exc)
            QMessageBox.warning(self, "API 配置未保存", "写入 config/config.yaml 失败，请检查文件权限。")
            return False

        self._logger.info("Telegram API configuration saved")
        self._runtime_state.update_credentials(
            self._api_id_edit.text(),
            self._api_hash_edit.text(),
            self._phone_edit.text(),
        )
        if show_success:
            QMessageBox.information(self, "API 配置已保存", "api_id 和 api_hash 已保存到 config/config.yaml。")
        return True

    def _start_worker(self, operation: str, payload: dict) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前登录任务尚未完成。")
            return

        self._set_buttons_enabled(False)
        self._runtime_state.update_credentials(
            self._api_id_edit.text(),
            self._api_hash_edit.text(),
            self._phone_edit.text(),
        )
        self._thread = QThread(self)
        self._worker = TelegramLoginWorker(
            service=self._telegram_service,
            operation=operation,
            payload=payload,
            logger=self._log_service.get_logger("telegram_worker"),
        )
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.code_sent.connect(self._on_code_sent)
        self._worker.account_ready.connect(self._on_account_ready)
        self._worker.password_required.connect(self._on_password_required)
        self._worker.logout_completed.connect(self._on_logout_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)
        self.login_status_changed.emit(message)

    def _on_code_sent(self, result) -> None:
        self._phone_label.setText(result.phone)
        self._session_path_label.setText(result.session_path)
        self._logger.info("Telegram verification code sent")

    def _on_account_ready(self, account: TelegramAccountInfo) -> None:
        self._display_name_label.setText(account.display_name or "-")
        self._username_label.setText(account.username or "-")
        self._phone_label.setText(account.phone or "-")
        self._session_path_label.setText(account.session_path)
        self._code_edit.clear()
        self._password_edit.clear()
        self._set_status("已登录")
        account_text = account.username or account.display_name or account.phone
        self._runtime_state.update_account(account_text, account.phone)
        self.account_changed.emit(account_text)

    def _on_password_required(self, message: str) -> None:
        self._set_status(message)
        self._password_edit.setFocus()
        QMessageBox.information(self, "需要二步验证", "请输入 Telegram 二步验证密码后点击“提交二步验证”。")

    def _on_logout_completed(self) -> None:
        self._display_name_label.setText("-")
        self._username_label.setText("-")
        self._phone_label.setText("-")
        self._code_edit.clear()
        self._password_edit.clear()
        self._set_status("未登录")
        self._runtime_state.update_account("-")
        self.account_changed.emit("-")

    def _on_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        if self._auto_restore_active:
            self._logger.warning("Automatic Telegram session restore failed: %s: %s", error_code, message)
            return
        QMessageBox.warning(self, "Telegram 登录失败", f"{error_code}: {message}")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._auto_restore_active = False
        self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._save_api_button.setEnabled(enabled)
        self._send_code_button.setEnabled(enabled)
        self._sign_in_button.setEnabled(enabled)
        self._password_button.setEnabled(enabled)
        self._restore_button.setEnabled(enabled)
        self._logout_button.setEnabled(enabled)
