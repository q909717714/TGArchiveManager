"""Diagnostics log page with filtering and export support."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
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

from services.log_service import LogEntry, LogQuery, LogService
from ui.searchable_combo_box import SearchableComboBox as QComboBox


class LogPage(QWidget):
    """Display application logs with module, level, task, and keyword filters."""

    def __init__(self, log_service: LogService, parent=None):
        super().__init__(parent)
        self._log_service = log_service
        self._last_display_text = ""
        self._current_entries: list[LogEntry] = []

        self._build_ui()
        self._reload_log_files()
        self.refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def refresh(self, *_args) -> None:
        """Refresh visible log entries using the current filters."""
        query = self._build_query()
        entries = self._log_service.read_entries(query)
        text = "\n".join(self._log_service.format_entry(entry, include_source=True) for entry in entries)
        if not text:
            text = "没有匹配的日志。"

        self._current_entries = entries
        if text != self._last_display_text:
            self._last_display_text = text
            self._log_view.setPlainText(text)
            self._log_view.moveCursor(QTextCursor.End)

        self._status_label.setText(f"匹配 {len(entries)} 条，日志目录：{self._log_service.logs_dir}")

    def _build_ui(self) -> None:
        title = QLabel("日志和排查")
        title.setObjectName("pageTitle")

        filter_group = QGroupBox("过滤条件")
        self._file_combo = QComboBox()
        self._file_combo.currentIndexChanged.connect(self.refresh)
        self._module_edit = QLineEdit()
        self._module_edit.setPlaceholderText("例如 public_search / forward / download")
        self._module_edit.returnPressed.connect(self.refresh)
        self._level_combo = QComboBox()
        self._level_combo.addItems(["全部", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self._level_combo.currentIndexChanged.connect(self.refresh)
        self._task_id_edit = QLineEdit()
        self._task_id_edit.setPlaceholderText("task_id 或任务编号")
        self._task_id_edit.returnPressed.connect(self.refresh)
        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("关键词、错误码、异常文本")
        self._keyword_edit.returnPressed.connect(self.refresh)
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(100, 20000)
        self._limit_spin.setValue(2000)
        self._limit_spin.valueChanged.connect(self.refresh)

        filter_form = QFormLayout(filter_group)
        filter_form.addRow("日志文件", self._file_combo)
        filter_form.addRow("模块", self._module_edit)
        filter_form.addRow("等级", self._level_combo)
        filter_form.addRow("任务 ID", self._task_id_edit)
        filter_form.addRow("关键词", self._keyword_edit)
        filter_form.addRow("最大条数", self._limit_spin)

        self._refresh_button = QPushButton("刷新日志")
        self._refresh_button.clicked.connect(self._refresh_all)
        self._open_dir_button = QPushButton("打开日志目录")
        self._open_dir_button.clicked.connect(self._open_log_dir)
        self._export_button = QPushButton("导出当前日志")
        self._export_button.clicked.connect(self._export_current_logs)
        self._copy_error_button = QPushButton("复制错误详情")
        self._copy_error_button.clicked.connect(self._copy_error_detail)
        self._clear_button = QPushButton("清空显示")
        self._clear_button.clicked.connect(self._clear_display)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self._refresh_button)
        button_layout.addWidget(self._open_dir_button)
        button_layout.addWidget(self._export_button)
        button_layout.addWidget(self._copy_error_button)
        button_layout.addWidget(self._clear_button)
        button_layout.addStretch(1)

        self._log_view = QTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setLineWrapMode(QTextEdit.NoWrap)

        self._status_label = QLabel()
        self._status_label.setObjectName("pageNote")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(filter_group)
        layout.addLayout(button_layout)
        layout.addWidget(self._log_view, 1)
        layout.addWidget(self._status_label)

    def _reload_log_files(self) -> None:
        current_path = self._file_combo.currentData()
        self._file_combo.blockSignals(True)
        self._file_combo.clear()
        self._file_combo.addItem("app.log", str(self._log_service.app_log_path))
        self._file_combo.addItem("全部日志", "")

        seen = {str(self._log_service.app_log_path.resolve())}
        for path in self._log_service.list_log_files():
            resolved = str(path.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            self._file_combo.addItem(self._display_path(path), str(path))

        if current_path:
            index = self._file_combo.findData(current_path)
            if index >= 0:
                self._file_combo.setCurrentIndex(index)
        self._file_combo.blockSignals(False)

    def _build_query(self) -> LogQuery:
        selected_path = self._file_combo.currentData()
        files: tuple[Path, ...] = ()
        if selected_path:
            files = (Path(str(selected_path)),)

        level = self._level_combo.currentText().strip()
        if level == "全部":
            level = ""

        return LogQuery(
            module=self._module_edit.text().strip(),
            level=level,
            task_id=self._task_id_edit.text().strip(),
            keyword=self._keyword_edit.text().strip(),
            files=files,
            limit=self._limit_spin.value(),
        )

    def _refresh_all(self) -> None:
        self._reload_log_files()
        self.refresh()

    def _open_log_dir(self) -> None:
        self._log_service.logs_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._log_service.logs_dir)))

    def _export_current_logs(self) -> None:
        if not self._current_entries:
            QMessageBox.information(self, "导出当前日志", "当前没有可导出的日志。")
            return
        target = self._log_service.export_entries(self._current_entries)
        QMessageBox.information(self, "导出当前日志", f"已导出：{target}")

    def _copy_error_detail(self) -> None:
        detail = self._log_service.latest_error_detail(self._current_entries)
        if not detail:
            QMessageBox.information(self, "复制错误详情", "当前筛选结果中没有错误详情。")
            return
        QApplication.clipboard().setText(detail)
        QMessageBox.information(self, "复制错误详情", "已复制最新错误详情。")

    def _clear_display(self) -> None:
        self._last_display_text = ""
        self._log_view.clear()

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._log_service.logs_dir))
        except ValueError:
            return path.name
