"""Export page for local Telegram archive data."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.repositories import ChatRepository, MessageRepository
from services.config_service import ConfigService
from services.export_service import ExportService
from services.local_search_service import LocalSearchQuery, LocalSearchService
from services.log_service import LogService
from ui.searchable_combo_box import SearchableComboBox as QComboBox


class ExportPage(QWidget):
    """Export local message archive data to common file formats."""

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

        self._build_ui()
        self._load_initial_values()
        self._reload_filters()

    def _build_ui(self) -> None:
        title = QLabel("导出")
        title.setObjectName("pageTitle")

        filter_group = QGroupBox("导出范围")
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
        self._limit_spin.setValue(1000)

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
        filter_form.addRow("最大导出", self._limit_spin)

        format_group = QGroupBox("导出格式")
        self._csv_checkbox = QCheckBox("CSV")
        self._csv_checkbox.setChecked(True)
        self._xlsx_checkbox = QCheckBox("Excel")
        self._xlsx_checkbox.setChecked(True)
        self._json_checkbox = QCheckBox("JSON")
        self._json_checkbox.setChecked(True)
        self._html_checkbox = QCheckBox("HTML")
        format_layout = QHBoxLayout(format_group)
        format_layout.addWidget(self._csv_checkbox)
        format_layout.addWidget(self._xlsx_checkbox)
        format_layout.addWidget(self._json_checkbox)
        format_layout.addWidget(self._html_checkbox)
        format_layout.addStretch(1)

        self._base_name_edit = QLineEdit()
        self._base_name_edit.setPlaceholderText("导出文件名前缀")
        self._reload_button = QPushButton("刷新条件")
        self._reload_button.clicked.connect(self._reload_filters)
        self._export_button = QPushButton("开始导出")
        self._export_button.clicked.connect(self._run_export)

        action_layout = QHBoxLayout()
        action_layout.addWidget(QLabel("文件前缀"))
        action_layout.addWidget(self._base_name_edit, 1)
        action_layout.addWidget(self._reload_button)
        action_layout.addWidget(self._export_button)

        self._report_text = QTextEdit()
        self._report_text.setReadOnly(True)
        self._report_text.setMinimumHeight(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(filter_group)
        layout.addWidget(format_group)
        layout.addLayout(action_layout)
        layout.addWidget(self._report_text, 1)

    def _load_initial_values(self) -> None:
        self._base_name_edit.setText("tg_archive_export")
        self._limit_spin.setValue(1000)
        self._html_checkbox.setChecked(bool(self._config_service.get("export.enable_html", False)))

    def _reload_filters(self) -> None:
        current_chat_id = self._chat_combo.currentData() if hasattr(self, "_chat_combo") else None
        current_type = self._type_combo.currentData() if hasattr(self, "_type_combo") else ""
        self._chat_combo.clear()
        self._chat_combo.addItem("全部聊天", None)
        for chat in self._chat_repository.list_chats():
            self._chat_combo.addItem(f"{chat.title} ({chat.tg_chat_id})", chat.tg_chat_id)
        if current_chat_id is not None:
            index = self._chat_combo.findData(current_chat_id)
            if index >= 0:
                self._chat_combo.setCurrentIndex(index)

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

    def _run_export(self) -> None:
        formats = self._selected_formats()
        if not formats:
            QMessageBox.information(self, "未选择格式", "请至少选择一种导出格式。")
            return
        report = self._export_service.export_messages(
            query=self._query_from_ui(),
            formats=formats,
            base_name=self._base_name_edit.text() or "tg_archive_export",
        )
        lines = [f"导出完成：{report.total_count} 条"]
        lines.extend(report.files)
        self._report_text.setPlainText("\n".join(lines))
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
