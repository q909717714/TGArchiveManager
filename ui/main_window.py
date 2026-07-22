"""Main application window for TGArchiveManager."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from database.db import DatabaseManager
from services.config_service import ConfigService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from ui.backup_page import BackupPage
from ui.chat_page import ChatPage
from ui.export_page import ExportPage
from ui.forward_page import ForwardPage
from ui.group_page import GroupPage
from ui.local_search_page import LocalSearchPage
from ui.log_page import LogPage
from ui.login_page import LoginPage
from ui.public_search_page import PublicSearchPage
from ui.search_result_page import SearchResultPage
from ui.settings_page import SettingsPage
from ui.telegram_native_search_page import TelegramNativeSearchPage


class MainWindow(QMainWindow):
    """Main window with left navigation and staged feature pages."""

    def __init__(
        self,
        project_root: Path,
        config_service: ConfigService,
        log_service: LogService,
        database: DatabaseManager,
        parent=None,
    ):
        super().__init__(parent)
        self._project_root = Path(project_root)
        self._config_service = config_service
        self._log_service = log_service
        self._database = database
        self._runtime_state = RuntimeState(
            api_id=str(config_service.get("telegram.api_id", "") or ""),
            api_hash=str(config_service.get("telegram.api_hash", "") or ""),
        )

        self.setWindowTitle("TGArchiveManager")
        self.resize(1180, 760)

        self._navigation = QListWidget()
        self._navigation.setFixedWidth(190)
        self._navigation.setObjectName("navigation")
        self._navigation.currentRowChanged.connect(self._set_current_page)

        self._pages = QStackedWidget()

        self._build_status_bar()
        self._build_pages()
        self._apply_style()

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._navigation)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setObjectName("divider")
        layout.addWidget(divider)

        layout.addWidget(self._pages, 1)
        self.setCentralWidget(container)

        self._navigation.setCurrentRow(0)

    def _build_pages(self) -> None:
        login_page = LoginPage(
            self._project_root,
            self._config_service,
            self._log_service,
            self._database,
            self._runtime_state,
        )
        login_page.login_status_changed.connect(self._on_login_status_changed)
        login_page.account_changed.connect(self._on_account_changed)

        entries = [
            ("账号登录", login_page),
            (
                "Bot 公开搜索",
                PublicSearchPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "TG 频道搜索",
                TelegramNativeSearchPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "搜索结果",
                SearchResultPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "转发管理",
                ForwardPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "自建群管理",
                GroupPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "聊天列表",
                ChatPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "备份下载",
                BackupPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                    self._runtime_state,
                ),
            ),
            (
                "本地搜索",
                LocalSearchPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                ),
            ),
            (
                "导出",
                ExportPage(
                    self._project_root,
                    self._config_service,
                    self._log_service,
                    self._database,
                ),
            ),
            ("日志", LogPage(self._log_service)),
            ("设置", SettingsPage(self._config_service)),
        ]

        for title, page in entries:
            item = QListWidgetItem(title)
            item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            self._navigation.addItem(item)
            self._pages.addWidget(page)

    def _build_status_bar(self) -> None:
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)

        self._login_status_label = QLabel("登录状态：未登录")
        self._account_label = QLabel("当前账号：-")
        status_bar.addWidget(self._login_status_label)
        status_bar.addWidget(self._account_label)
        status_bar.addPermanentWidget(QLabel("当前任务：无"))
        status_bar.addPermanentWidget(QLabel(f"数据库：{self._database.db_path}"))

    def _set_current_page(self, index: int) -> None:
        if 0 <= index < self._pages.count():
            self._pages.setCurrentIndex(index)

    def _on_login_status_changed(self, status: str) -> None:
        self._login_status_label.setText(f"登录状态：{status}")

    def _on_account_changed(self, account: str) -> None:
        self._account_label.setText(f"当前账号：{account or '-'}")

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f7f8fa;
            }
            QListWidget#navigation {
                background: #202733;
                color: #f5f7fb;
                border: none;
                padding: 8px;
                font-size: 14px;
            }
            QListWidget#navigation::item {
                min-height: 34px;
                padding: 6px 10px;
                border-radius: 4px;
            }
            QListWidget#navigation::item:selected {
                background: #2f80ed;
            }
            QFrame#divider {
                color: #d7dbe2;
            }
            QLabel#pageTitle {
                font-size: 22px;
                font-weight: 600;
                color: #202733;
            }
            QLabel#pageNote {
                font-size: 14px;
                color: #536171;
            }
            QTextEdit {
                background: #111827;
                color: #e5e7eb;
                border: 1px solid #d7dbe2;
                font-family: Consolas, "Courier New", monospace;
                font-size: 12px;
            }
            QPushButton {
                min-height: 28px;
                padding: 4px 12px;
            }
            """
        )
