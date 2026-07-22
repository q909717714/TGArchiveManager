"""Local search page for backed-up Telegram messages."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.models import MessageRecord
from database.repositories import ChatRepository, MessageRepository
from services.config_service import ConfigService
from services.export_service import ExportService
from services.local_search_service import LocalSearchQuery, LocalSearchService
from services.log_service import LogService
from ui.searchable_combo_box import SearchableComboBox as QComboBox


class LocalSearchPage(QWidget):
    """Search local SQLite-backed Telegram message archives."""

    COLUMNS = ["勾选", "chat_id", "消息ID", "日期", "发送者", "类型", "媒体", "已下载", "预览", "链接", "本地路径"]

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
        self._message_repository = MessageRepository(database)
        self._chat_repository = ChatRepository(database)
        self._search_service = LocalSearchService(self._message_repository, log_service.get_logger("local_search"))
        self._export_service = ExportService(
            message_repository=self._message_repository,
            search_service=self._search_service,
            export_root=self._config_service.resolve_path("export.root_dir", "exports"),
            logger=log_service.get_logger("export"),
        )
        self._current_results: List[MessageRecord] = []

        self._build_ui()
        self._reload_chats()
        self._reload_types()
        self._run_search()

    def _build_ui(self) -> None:
        title = QLabel("本地搜索")
        title.setObjectName("pageTitle")

        filter_group = QGroupBox("搜索条件")
        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("关键词、发送者、文件名或链接")
        self._chat_combo = QComboBox()
        self._type_combo = QComboBox()
        self._media_combo = QComboBox()
        self._media_combo.addItem("全部", "all")
        self._media_combo.addItem("仅文本", "text")
        self._media_combo.addItem("有媒体", "media")
        self._media_combo.addItem("已下载", "downloaded")
        self._date_filter_checkbox = QCheckBox("启用时间范围")
        self._from_date_edit = QDateEdit()
        self._from_date_edit.setDisplayFormat("yyyy-MM-dd")
        self._from_date_edit.setCalendarPopup(True)
        self._from_date_edit.setDate(QDate.currentDate().addDays(-30))
        self._to_date_edit = QDateEdit()
        self._to_date_edit.setDisplayFormat("yyyy-MM-dd")
        self._to_date_edit.setCalendarPopup(True)
        self._to_date_edit.setDate(QDate.currentDate())
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 5000)
        self._limit_spin.setValue(500)

        filter_form = QFormLayout(filter_group)
        filter_form.addRow("关键词", self._keyword_edit)
        filter_form.addRow("聊天", self._chat_combo)
        filter_form.addRow("类型", self._type_combo)
        filter_form.addRow("媒体", self._media_combo)
        date_row = QHBoxLayout()
        date_row.addWidget(self._date_filter_checkbox)
        date_row.addWidget(self._from_date_edit)
        date_row.addWidget(self._to_date_edit)
        filter_form.addRow("时间", date_row)
        filter_form.addRow("最大结果", self._limit_spin)

        self._search_button = QPushButton("搜索")
        self._search_button.clicked.connect(self._run_search)
        self._reload_button = QPushButton("刷新条件")
        self._reload_button.clicked.connect(self._reload_filters)
        self._check_all_button = QPushButton("全选")
        self._check_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self._uncheck_all_button = QPushButton("全不选")
        self._uncheck_all_button.clicked.connect(lambda: self._set_all_checked(False))
        self._delete_button = QPushButton("删除勾选记录")
        self._delete_button.clicked.connect(self._delete_selected_messages)
        self._csv_checkbox = QCheckBox("CSV")
        self._csv_checkbox.setChecked(True)
        self._xlsx_checkbox = QCheckBox("Excel")
        self._xlsx_checkbox.setChecked(True)
        self._json_checkbox = QCheckBox("JSON")
        self._json_checkbox.setChecked(True)
        self._html_checkbox = QCheckBox("HTML")
        self._export_button = QPushButton("导出当前结果")
        self._export_button.clicked.connect(self._export_current_results)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self._search_button)
        action_layout.addWidget(self._reload_button)
        action_layout.addWidget(self._check_all_button)
        action_layout.addWidget(self._uncheck_all_button)
        action_layout.addWidget(self._delete_button)
        action_layout.addStretch(1)
        action_layout.addWidget(self._csv_checkbox)
        action_layout.addWidget(self._xlsx_checkbox)
        action_layout.addWidget(self._json_checkbox)
        action_layout.addWidget(self._html_checkbox)
        action_layout.addWidget(self._export_button)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)

        self._status_label = QLabel("未搜索")
        self._report_label = QLabel("导出报告：-")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(filter_group)
        layout.addLayout(action_layout)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report_label)

    def _reload_filters(self) -> None:
        self._reload_chats()
        self._reload_types()

    def _reload_chats(self) -> None:
        current_chat_id = self._chat_combo.currentData() if hasattr(self, "_chat_combo") else None
        self._chat_combo.clear()
        self._chat_combo.addItem("全部聊天", None)
        for chat in self._chat_repository.list_chats():
            self._chat_combo.addItem(f"{chat.title} ({chat.tg_chat_id})", chat.tg_chat_id)
        if current_chat_id is not None:
            index = self._chat_combo.findData(current_chat_id)
            if index >= 0:
                self._chat_combo.setCurrentIndex(index)

    def _reload_types(self) -> None:
        current_type = self._type_combo.currentData() if hasattr(self, "_type_combo") else ""
        self._type_combo.clear()
        self._type_combo.addItem("全部类型", "")
        for message_type in self._message_repository.distinct_message_types():
            self._type_combo.addItem(message_type, message_type)
        if current_type:
            index = self._type_combo.findData(current_type)
            if index >= 0:
                self._type_combo.setCurrentIndex(index)

    def _query_from_ui(self) -> LocalSearchQuery:
        date_from = ""
        date_to = ""
        if self._date_filter_checkbox.isChecked():
            date_from = f"{self._from_date_edit.date().toString('yyyy-MM-dd')}T00:00:00"
            date_to = f"{self._to_date_edit.date().toString('yyyy-MM-dd')}T23:59:59"
        return LocalSearchQuery(
            keyword=self._keyword_edit.text(),
            tg_chat_id=self._chat_combo.currentData(),
            date_from=date_from,
            date_to=date_to,
            message_type=self._type_combo.currentData() or "",
            media_filter=self._media_combo.currentData() or "all",
            limit=self._limit_spin.value(),
        )

    def _run_search(self) -> None:
        self._current_results = self._search_service.search(self._query_from_ui())
        self._populate_table(self._current_results)
        self._status_label.setText(f"本地搜索结果：{len(self._current_results)} 条")

    def _populate_table(self, messages: List[MessageRecord]) -> None:
        self._table.setRowCount(len(messages))
        for row, message in enumerate(messages):
            self._set_item(row, 0, "", checkable=True)
            self._set_item(row, 1, str(message.tg_chat_id))
            self._set_item(row, 2, str(message.message_id))
            self._set_item(row, 3, message.date)
            self._set_item(row, 4, message.sender_name)
            self._set_item(row, 5, message.message_type)
            self._set_item(row, 6, "是" if message.has_media else "否")
            self._set_item(row, 7, "是" if message.is_downloaded else "否")
            self._set_item(row, 8, message.text_preview)
            self._set_item(row, 9, message.source_link)
            self._set_item(row, 10, message.local_path)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)

    def _set_item(self, row: int, column: int, text: str, checkable: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(Qt.Unchecked)
        item.setFlags(flags)
        self._table.setItem(row, column, item)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _delete_selected_messages(self) -> None:
        message_keys = self._selected_message_keys()
        if not message_keys:
            QMessageBox.information(self, "未勾选消息", "请至少勾选一条需要删除的本地消息记录。")
            return

        reply = QMessageBox.question(
            self,
            "确认删除本地消息记录",
            f"将删除 {len(message_keys)} 条本地消息记录及其文件/下载/转发元数据，不会删除已下载到磁盘的媒体文件，也不会删除 Telegram 远端消息。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = self._message_repository.delete_messages_by_keys(message_keys)
        self._reload_types()
        self._run_search()
        self._status_label.setText(f"已删除本地消息记录：{deleted} 条")

    def _selected_message_keys(self) -> list[tuple[int, int]]:
        message_keys: list[tuple[int, int]] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, 0)
            chat_item = self._table.item(row, 1)
            id_item = self._table.item(row, 2)
            if check_item is None or chat_item is None or id_item is None or check_item.checkState() != Qt.Checked:
                continue
            try:
                message_keys.append((int(chat_item.text()), int(id_item.text())))
            except ValueError:
                continue
        return message_keys

    def _export_current_results(self) -> None:
        formats = self._selected_formats()
        if not formats:
            QMessageBox.information(self, "未选择格式", "请至少选择一种导出格式。")
            return
        report = self._export_service.export_message_records(
            self._current_results,
            formats,
            base_name="local_search_results",
        )
        self._report_label.setText(f"导出报告：{report.total_count} 条，文件：{'; '.join(report.files)}")
        QMessageBox.information(self, "导出完成", f"已导出 {report.total_count} 条记录。")

    def _selected_formats(self) -> list[str]:
        formats = []
        if self._csv_checkbox.isChecked():
            formats.append("csv")
        if self._xlsx_checkbox.isChecked():
            formats.append("xlsx")
        if self._json_checkbox.isChecked():
            formats.append("json")
        if self._html_checkbox.isChecked():
            formats.append("html")
        return formats
