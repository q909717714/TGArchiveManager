"""Telegram chat list page."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from database.repositories import AccountRepository, ChatRepository
from services.config_service import ConfigService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from services.telegram_service import TelegramService
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.chat_worker import ChatSyncWorker


class ChatPage(QWidget):
    """Display and synchronize Telegram chats accessible to the logged-in account."""

    target_chat_changed = Signal(object)

    CHAT_ID_COLUMN = 4
    TAG_COLUMN = 5
    FOLDER_COLUMN = 6

    COLUMNS = [
        "选择",
        "名称",
        "类型",
        "username",
        "chat_id",
        "标签",
        "官方分组",
        "最后同步时间",
        "最后备份消息ID",
    ]

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
        self._logger = log_service.get_logger("chat_page")
        self._account_repository = AccountRepository(database)
        self._chat_repository = ChatRepository(database)
        self._telegram_service = TelegramService(
            project_root=self._project_root,
            config=self._config_service.as_dict(),
            account_repository=self._account_repository,
            logger=log_service.get_logger("telegram"),
            chat_repository=self._chat_repository,
        )
        self._thread: Optional[QThread] = None
        self._worker: Optional[ChatSyncWorker] = None
        self._loading_table = False

        self._build_ui()
        self._reload_table()

    def _build_ui(self) -> None:
        title = QLabel("聊天列表")
        title.setObjectName("pageTitle")

        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("按名称、username、标签或官方分组过滤")
        self._filter_edit.textChanged.connect(self._reload_table)

        self._sync_button = QPushButton("同步聊天列表")
        self._sync_button.clicked.connect(self._sync_chats)
        self._refresh_button = QPushButton("刷新本地列表")
        self._refresh_button.clicked.connect(self._reload_table)
        self._save_tag_button = QPushButton("保存当前标签")
        self._save_tag_button.clicked.connect(self._save_current_tag)
        self._select_target_button = QPushButton("选择为转发目标")
        self._select_target_button.clicked.connect(self._select_current_target)
        self._delete_button = QPushButton("删除勾选本地记录")
        self._delete_button.clicked.connect(self._delete_selected_chats)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("过滤"))
        toolbar.addWidget(self._filter_edit, 1)
        toolbar.addWidget(self._sync_button)
        toolbar.addWidget(self._refresh_button)
        toolbar.addWidget(self._save_tag_button)
        toolbar.addWidget(self._select_target_button)
        toolbar.addWidget(self._delete_button)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.itemChanged.connect(self._on_table_item_changed)

        self._status_label = QLabel("未同步")
        self._target_label = QLabel("当前目标：-")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addLayout(toolbar)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._status_label)
        layout.addWidget(self._target_label)

    def _sync_chats(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前聊天同步任务尚未完成。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = ChatSyncWorker(
            service=self._telegram_service,
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
            },
            logger=self._log_service.get_logger("chat_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.chats_synced.connect(self._on_chats_synced)
        self._worker.failed.connect(self._on_sync_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _reload_table(self) -> None:
        chats = self._chat_repository.list_chats(self._filter_edit.text() if hasattr(self, "_filter_edit") else "")
        chats = self._sort_chats_by_folder(chats)
        self._populate_table(chats)
        self._set_status(f"本地聊天：{len(chats)} 个")

    def _populate_table(self, chats: List[Chat]) -> None:
        self._loading_table = True
        try:
            self._table.setRowCount(len(chats))
            for row, chat in enumerate(chats):
                self._set_item(row, 0, "", chat, editable=False, checkable=True)
                self._set_item(row, 1, chat.title, chat, editable=False)
                self._set_item(row, 2, chat.type, chat, editable=False)
                self._set_item(row, 3, chat.username, chat, editable=False)
                self._set_item(row, self.CHAT_ID_COLUMN, str(chat.tg_chat_id), chat, editable=False)
                self._set_item(row, self.TAG_COLUMN, chat.tag, chat, editable=True)
                self._set_item(row, self.FOLDER_COLUMN, chat.telegram_folder_names or "", chat, editable=False)
                self._set_item(row, 7, chat.updated_at, chat, editable=False)
                self._set_item(
                    row,
                    8,
                    "" if chat.last_backup_message_id is None else str(chat.last_backup_message_id),
                    chat,
                    editable=False,
                )
            self._table.resizeColumnsToContents()
            self._table.horizontalHeader().setStretchLastSection(True)
        finally:
            self._loading_table = False

    def _set_item(self, row: int, column: int, text: str, chat: Chat, editable: bool, checkable: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if editable:
            flags |= Qt.ItemIsEditable
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(Qt.Unchecked)
        item.setFlags(flags)
        item.setData(Qt.UserRole, chat.tg_chat_id)
        self._table.setItem(row, column, item)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading_table or item.column() != self.TAG_COLUMN:
            return
        tg_chat_id = self._chat_id_for_row(item.row())
        if tg_chat_id is None:
            return
        self._chat_repository.update_tag(tg_chat_id, item.text())
        self._set_status("标签已保存")

    def _save_current_tag(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择聊天", "请先选择一个聊天。")
            return
        tag_item = self._table.item(row, self.TAG_COLUMN)
        tg_chat_id = self._chat_id_for_row(row)
        if tg_chat_id is None or tag_item is None:
            return
        self._chat_repository.update_tag(tg_chat_id, tag_item.text())
        self._set_status("标签已保存")

    def _select_current_target(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择聊天", "请先选择一个聊天。")
            return
        tg_chat_id = self._chat_id_for_row(row)
        if tg_chat_id is None:
            return
        chat = self._chat_repository.get_by_tg_chat_id(tg_chat_id)
        if chat is None:
            return
        self._target_label.setText(f"当前目标：{chat.title} ({chat.tg_chat_id})")
        self.target_chat_changed.emit(chat)
        self._set_status("已选择转发目标")

    def _delete_selected_chats(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "请等待当前聊天同步任务结束后再删除。")
            return

        chat_ids = self._selected_chat_ids()
        if not chat_ids:
            QMessageBox.information(self, "未勾选聊天", "请至少勾选一条本地聊天记录。")
            return

        reply = QMessageBox.question(
            self,
            "确认删除本地聊天记录",
            f"将删除 {len(chat_ids)} 条本地聊天记录及对应自建群登记，不会退出或删除 Telegram 远端聊天。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = self._chat_repository.delete_chats_by_tg_chat_ids(chat_ids)
        self._target_label.setText("当前目标：-")
        self._reload_table()
        self._set_status(f"已删除本地聊天记录：{deleted} 条")

    def _selected_chat_ids(self) -> List[int]:
        chat_ids: List[int] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, 0)
            tg_chat_id = self._chat_id_for_row(row)
            if check_item is None or tg_chat_id is None or check_item.checkState() != Qt.Checked:
                continue
            chat_ids.append(tg_chat_id)
        return chat_ids

    def _chat_id_for_row(self, row: int) -> Optional[int]:
        item = self._table.item(row, self.CHAT_ID_COLUMN)
        if item is None:
            return None
        try:
            return int(item.text())
        except ValueError:
            return None

    def _on_chats_synced(self, chats) -> None:
        self._populate_table(list(chats))
        self._set_status(f"同步完成：{len(chats)} 个聊天")

    def _on_sync_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "聊天同步失败", f"{error_code}: {message}")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_buttons_enabled(True)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._sync_button.setEnabled(enabled)
        self._refresh_button.setEnabled(enabled)
        self._save_tag_button.setEnabled(enabled)
        self._select_target_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)

    @staticmethod
    def _sort_chats_by_folder(chats: List[Chat]) -> List[Chat]:
        return sorted(
            chats,
            key=lambda chat: (
                str(chat.telegram_folder_names or "未分组").lower(),
                str(chat.title or "").lower(),
                int(chat.tg_chat_id),
            ),
        )
