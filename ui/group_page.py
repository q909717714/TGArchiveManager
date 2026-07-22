"""Tool-created Telegram group management page."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.models import Chat
from database.repositories import AccountRepository, ChatRepository, GroupRepository
from services.config_service import ConfigService
from services.group_service import GroupService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from services.telegram_service import TelegramService
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.forward_worker import GroupCreateWorker


class GroupPage(QWidget):
    """Create and list Telegram groups managed by TGArchiveManager."""

    COLUMNS = ["勾选", "名称", "chat_id", "分类", "工具创建", "更新时间"]

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
        self._chat_repository = ChatRepository(database)
        self._group_repository = GroupRepository(database)
        self._thread: Optional[QThread] = None
        self._worker: Optional[GroupCreateWorker] = None

        self._build_ui()
        self._reload_groups()

    def _build_ui(self) -> None:
        title = QLabel("自建群管理")
        title.setObjectName("pageTitle")

        create_group = QGroupBox("新建目标群")
        self._title_edit = QLineEdit()
        self._title_edit.setPlaceholderText("目标群名称")
        self._category_edit = QLineEdit()
        self._category_edit.setPlaceholderText("分类，可选")
        self._create_button = QPushButton("新建目标群")
        self._create_button.clicked.connect(self._create_group)
        create_layout = QHBoxLayout(create_group)
        create_layout.addWidget(self._title_edit, 2)
        create_layout.addWidget(self._category_edit, 1)
        create_layout.addWidget(self._create_button)

        self._refresh_button = QPushButton("刷新列表")
        self._refresh_button.clicked.connect(self._reload_groups)
        self._delete_button = QPushButton("删除勾选登记")
        self._delete_button.clicked.connect(self._delete_selected_groups)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self._refresh_button)
        toolbar.addWidget(self._delete_button)
        toolbar.addStretch(1)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)

        self._status_label = QLabel("未创建")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(create_group)
        layout.addLayout(toolbar)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._status_label)

    def _reload_groups(self) -> None:
        groups = self._group_repository.list_groups()
        self._table.setRowCount(len(groups))
        for row, chat in enumerate(groups):
            self._set_item(row, 0, "", checkable=True)
            self._set_item(row, 1, chat.title)
            self._set_item(row, 2, str(chat.tg_chat_id))
            self._set_item(row, 3, chat.tag)
            self._set_item(row, 4, "是" if chat.is_created_by_tool else "否")
            self._set_item(row, 5, chat.updated_at)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)
        self._set_status(f"本地自建群：{len(groups)} 个")

    def _set_item(self, row: int, column: int, text: str, checkable: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(Qt.Unchecked)
        item.setFlags(flags)
        self._table.setItem(row, column, item)

    def _delete_selected_groups(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "请等待当前目标群创建任务结束后再删除。")
            return

        group_ids = self._selected_group_ids()
        if not group_ids:
            QMessageBox.information(self, "未勾选群登记", "请至少勾选一条本地自建群登记。")
            return

        reply = QMessageBox.question(
            self,
            "确认删除本地群登记",
            f"将删除 {len(group_ids)} 条本地自建群登记，不会删除 Telegram 远端群。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = self._group_repository.delete_groups_by_chat_ids(group_ids)
        self._reload_groups()
        self._set_status(f"已删除本地自建群登记：{deleted} 条")

    def _selected_group_ids(self) -> list[int]:
        group_ids: list[int] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, 0)
            id_item = self._table.item(row, 2)
            if check_item is None or id_item is None or check_item.checkState() != Qt.Checked:
                continue
            try:
                group_ids.append(int(id_item.text()))
            except ValueError:
                continue
        return group_ids

    def _create_group(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前目标群创建任务尚未完成。")
            return
        title = self._title_edit.text().strip()
        if not title:
            QMessageBox.information(self, "缺少群名称", "请输入目标群名称。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = GroupCreateWorker(
            service=self._build_group_service(),
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "title": title,
                "category": self._category_edit.text(),
            },
            logger=self._log_service.get_logger("group_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.group_created.connect(self._on_group_created)
        self._worker.failed.connect(self._on_group_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _build_group_service(self) -> GroupService:
        telegram_service = TelegramService(
            project_root=self._project_root,
            config=self._config_service.as_dict(),
            account_repository=AccountRepository(self._database),
            logger=self._log_service.get_logger("telegram"),
            chat_repository=self._chat_repository,
        )
        return GroupService(
            telegram_service=telegram_service,
            chat_repository=self._chat_repository,
            group_repository=self._group_repository,
            logger=self._log_service.get_logger("group_service"),
        )

    def _on_group_created(self, chat: Chat) -> None:
        self._title_edit.clear()
        self._category_edit.clear()
        self._reload_groups()
        QMessageBox.information(self, "目标群已创建", f"已创建目标群：{chat.title}")

    def _on_group_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "目标群创建失败", f"{error_code}: {message}")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_buttons_enabled(True)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._create_button.setEnabled(enabled)
        self._refresh_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)
