"""Saved public-search result management page."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDate, Qt, QThread
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.models import SearchResult
from database.repositories import (
    DownloadRecordRepository,
    FileRepository,
    MessageRepository,
    PublicSearchRepository,
    TelegraphRepository,
)
from services.config_service import ConfigService
from services.download_service import DownloadService, format_download_progress_metrics
from services.forward_service import ForwardService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from services.service_factory import ApplicationContext, ServiceFactory
from services.telegram_service import TelegramService
from ui.searchable_combo_box import SearchableComboBox as QComboBox
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.search_worker import SearchResultDownloadWorker


class SearchResultPage(QWidget):
    """Display, filter, preview, download, and delete saved search results."""

    COLUMNS = [
        "勾选",
        "ID",
        "任务",
        "排名",
        "来源",
        "类型",
        "媒体信息",
        "标题",
        "摘要",
        "链接",
        "重复",
        "创建时间",
        "转发状态",
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
        self._repository = PublicSearchRepository(database)
        self._download_thread: Optional[QThread] = None
        self._download_worker: Optional[SearchResultDownloadWorker] = None
        self._current_results: list[SearchResult] = []
        self._service_factory = ServiceFactory(
            ApplicationContext(
                project_root=self._project_root,
                config_service=self._config_service,
                log_service=self._log_service,
                database=self._database,
            )
        )

        self._build_ui()
        self._load_initial_values()

    def _build_ui(self) -> None:
        title = QLabel("搜索结果")
        title.setObjectName("pageTitle")

        filter_group = QGroupBox("筛选条件")
        self._task_combo = QComboBox()
        self._task_combo.currentIndexChanged.connect(self._on_task_filter_changed)
        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("关键词、标题、摘要或链接")
        self._type_combo = QComboBox()
        self._date_filter_checkbox = QCheckBox("启用时间范围")
        self._from_date_edit = QDateEdit()
        self._from_date_edit.setDisplayFormat("yyyy-MM-dd")
        self._from_date_edit.setCalendarPopup(True)
        self._from_date_edit.setDate(QDate.currentDate().addDays(-7))
        self._to_date_edit = QDateEdit()
        self._to_date_edit.setDisplayFormat("yyyy-MM-dd")
        self._to_date_edit.setCalendarPopup(True)
        self._to_date_edit.setDate(QDate.currentDate())
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(1, 1000)
        self._limit_spin.setValue(500)
        self._telegraph_image_limit_spin = QSpinBox()
        self._telegraph_image_limit_spin.setRange(0, 9999)
        self._telegraph_image_limit_spin.setSpecialValueText("全部")
        self._telegraph_image_limit_spin.setValue(0)

        date_row = QHBoxLayout()
        date_row.addWidget(self._date_filter_checkbox)
        date_row.addWidget(self._from_date_edit)
        date_row.addWidget(self._to_date_edit)
        date_row.addStretch(1)

        filter_form = QFormLayout(filter_group)
        filter_form.addRow("任务", self._task_combo)
        filter_form.addRow("关键词", self._keyword_edit)
        filter_form.addRow("类型", self._type_combo)
        filter_form.addRow("时间", date_row)
        filter_form.addRow("最大结果", self._limit_spin)
        filter_form.addRow("Telegraph 图片下载数", self._telegraph_image_limit_spin)

        self._refresh_button = QPushButton("刷新结果")
        self._refresh_button.clicked.connect(self._reload_all_sources)
        self._apply_filter_button = QPushButton("应用筛选")
        self._apply_filter_button.clicked.connect(self._reload_results)
        self._check_all_button = QPushButton("全选")
        self._check_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self._uncheck_all_button = QPushButton("全不选")
        self._uncheck_all_button.clicked.connect(lambda: self._set_all_checked(False))
        self._preview_button = QPushButton("预览选中卡片")
        self._preview_button.clicked.connect(self._preview_selected_cards)
        self._download_button = QPushButton("下载选中媒体")
        self._download_button.clicked.connect(self._download_selected_media)
        self._cancel_button = QPushButton("取消下载")
        self._cancel_button.clicked.connect(self._cancel_download)
        self._cancel_button.setEnabled(False)
        self._delete_results_button = QPushButton("删除选中结果")
        self._delete_results_button.clicked.connect(self._delete_selected_results)
        self._delete_task_button = QPushButton("删除当前任务")
        self._delete_task_button.clicked.connect(self._delete_current_task)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self._refresh_button)
        action_layout.addWidget(self._apply_filter_button)
        action_layout.addWidget(self._check_all_button)
        action_layout.addWidget(self._uncheck_all_button)
        action_layout.addWidget(self._preview_button)
        action_layout.addWidget(self._download_button)
        action_layout.addWidget(self._cancel_button)
        action_layout.addWidget(self._delete_results_button)
        action_layout.addWidget(self._delete_task_button)
        action_layout.addStretch(1)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(320)

        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(180)

        self._status_label = QLabel("未加载")
        self._report_label = QLabel("任务报告：-")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(filter_group)
        layout.addLayout(action_layout)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._preview_text)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report_label)

    def _load_initial_values(self) -> None:
        self._reload_all_sources()

    def _reload_all_sources(self) -> None:
        self._reload_task_combo()
        self._reload_type_combo()
        self._reload_results()

    def _reload_task_combo(self) -> None:
        current_task_id = self._task_combo.currentData() if hasattr(self, "_task_combo") else None
        self._task_combo.blockSignals(True)
        self._task_combo.clear()
        self._task_combo.addItem("最近结果", None)
        for task in self._repository.latest_tasks(limit=50):
            if task.id is None:
                continue
            self._task_combo.addItem(
                f"#{task.id} {task.keyword} ({task.status}, 保存 {task.total_saved})",
                task.id,
            )
        if current_task_id is not None:
            index = self._task_combo.findData(current_task_id)
            if index >= 0:
                self._task_combo.setCurrentIndex(index)
        self._task_combo.blockSignals(False)

    def _on_task_filter_changed(self) -> None:
        self._reload_type_combo()
        self._reload_results()

    def _reload_type_combo(self) -> None:
        current_type = self._type_combo.currentData() if hasattr(self, "_type_combo") else ""
        task_id = self._task_combo.currentData() if hasattr(self, "_task_combo") else None
        self._type_combo.blockSignals(True)
        self._type_combo.clear()
        self._type_combo.addItem("全部类型", "")
        for result_type in self._repository.distinct_result_types(int(task_id) if task_id is not None else None):
            self._type_combo.addItem(result_type, result_type)
        if current_type:
            index = self._type_combo.findData(current_type)
            if index >= 0:
                self._type_combo.setCurrentIndex(index)
        self._type_combo.blockSignals(False)

    def _reload_results(self) -> None:
        created_from = ""
        created_to = ""
        if self._date_filter_checkbox.isChecked():
            created_from = f"{self._from_date_edit.date().toString('yyyy-MM-dd')} 00:00:00"
            created_to = f"{self._to_date_edit.date().toString('yyyy-MM-dd')} 23:59:59"

        task_id = self._task_combo.currentData()
        results = self._repository.list_filtered_results(
            task_id=int(task_id) if task_id is not None else None,
            keyword=self._keyword_edit.text(),
            result_type=self._type_combo.currentData() or "",
            created_from=created_from,
            created_to=created_to,
            limit=self._limit_spin.value(),
        )
        self._populate_table(results)
        self._preview_text.clear()
        self._set_status(f"已加载搜索结果：{len(results)} 条")
        self._show_task_summary()

    def _populate_table(self, results: list[SearchResult]) -> None:
        self._current_results = list(results)
        self._table.setRowCount(len(results))
        for row, result in enumerate(results):
            self._set_item(row, 0, "", checkable=True)
            self._set_item(row, 1, "" if result.id is None else str(result.id))
            self._set_item(row, 2, "" if result.task_id is None else str(result.task_id))
            self._set_item(row, 3, str(result.rank_no))
            self._set_item(row, 4, result.engine_name)
            self._set_item(row, 5, self._display_result_type(result))
            self._set_item(row, 6, ForwardService.result_media_info(result) or "-")
            self._set_item(row, 7, result.title)
            self._set_item(row, 8, result.summary)
            self._set_item(row, 9, result.url)
            self._set_item(row, 10, "是" if result.is_duplicate else "否")
            self._set_item(row, 11, result.created_at)
            self._set_item(row, 12, result.forward_status)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)

    def _set_item(self, row: int, column: int, text: str, checkable: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(Qt.Checked)
        item.setFlags(flags)
        self._table.setItem(row, column, item)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _selected_result_ids(self) -> list[int]:
        selected: list[int] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, 0)
            id_item = self._table.item(row, 1)
            if check_item is None or id_item is None or check_item.checkState() != Qt.Checked:
                continue
            try:
                selected.append(int(id_item.text()))
            except ValueError:
                continue
        return selected

    def _preview_selected_cards(self) -> None:
        result_ids = self._selected_result_ids()
        if not result_ids:
            QMessageBox.information(self, "未选择结果", "请至少勾选一条搜索结果。")
            return

        results = self._repository.get_results_by_ids(result_ids[:20])
        preview = "\n\n---\n\n".join(ForwardService.format_card(result) for result in results)
        if len(result_ids) > 20:
            preview = f"{preview}\n\n---\n\n仅预览前 20 条，实际选中 {len(result_ids)} 条。"
        self._preview_text.setPlainText(preview)
        self._set_status(f"已生成预览：{min(len(result_ids), 20)}/{len(result_ids)} 条")

    def _download_selected_media(self) -> None:
        if self._download_thread is not None:
            QMessageBox.information(self, "下载任务运行中", "当前搜索结果媒体下载尚未完成。")
            return

        result_ids = self._selected_result_ids()
        if not result_ids:
            QMessageBox.information(self, "未选择结果", "请至少勾选一条搜索结果。")
            return
        results = self._repository.get_results_by_ids(result_ids)
        if not results:
            QMessageBox.information(self, "结果不存在", "所选搜索结果不存在，请刷新后重试。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials

        self._set_buttons_enabled(False)
        self._download_thread = QThread(self)
        self._download_worker = SearchResultDownloadWorker(
            service=self._build_download_service(),
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "results": results,
                "retry_count": int(self._config_service.get("download.retry_count", 3) or 3),
                "telegraph_image_limit": self._telegraph_image_limit_spin.value(),
            },
            logger=self._log_service.get_logger("search_result_download_worker"),
        )
        self._download_worker.moveToThread(self._download_thread)
        self._download_thread.started.connect(self._download_worker.run)
        self._download_worker.status_changed.connect(self._set_status)
        self._download_worker.progress_changed.connect(self._on_download_progress)
        self._download_worker.download_completed.connect(self._on_download_completed)
        self._download_worker.cancelled.connect(self._on_download_cancelled)
        self._download_worker.failed.connect(self._on_download_failed)
        self._download_worker.finished.connect(self._download_thread.quit)
        self._download_worker.finished.connect(self._download_worker.deleteLater)
        self._download_thread.finished.connect(self._download_thread.deleteLater)
        self._download_thread.finished.connect(self._on_download_worker_finished)
        self._download_thread.start()

    def _cancel_download(self) -> None:
        if self._download_worker is None:
            return
        self._download_worker.cancel()
        self._cancel_button.setEnabled(False)
        self._set_status("正在取消下载任务，当前步骤结束后会停止...")

    def _delete_selected_results(self) -> None:
        if self._download_thread is not None:
            QMessageBox.information(self, "下载任务运行中", "请等待当前搜索结果媒体下载结束后再删除。")
            return

        result_ids = self._selected_result_ids()
        if not result_ids:
            QMessageBox.information(self, "未选择结果", "请至少勾选一条搜索结果。")
            return

        reply = QMessageBox.question(
            self,
            "确认删除搜索结果",
            f"将删除 {len(result_ids)} 条本地搜索结果及其 Telegraph 明细/转发元数据，不会删除已下载文件。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = self._repository.delete_results_by_ids(result_ids)
        self._reload_all_sources()
        self._set_status(f"已删除搜索结果：{deleted} 条")

    def _delete_current_task(self) -> None:
        if self._download_thread is not None:
            QMessageBox.information(self, "下载任务运行中", "请等待当前搜索结果媒体下载结束后再删除。")
            return

        task_id = self._task_combo.currentData()
        if task_id is None:
            QMessageBox.information(self, "未选择任务", "请选择一个具体搜索任务后再删除。")
            return

        reply = QMessageBox.question(
            self,
            "确认删除搜索任务",
            "将删除当前搜索任务及其全部本地搜索结果、Telegraph 明细和转发元数据，不会删除已下载文件。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = self._repository.delete_tasks_by_ids([int(task_id)])
        self._reload_all_sources()
        self._set_status(f"已删除搜索任务：{deleted} 个")

    def _build_download_service(self) -> DownloadService:
        download_logger = self._log_service.get_file_logger("download", "download.log")
        return self._service_factory.download_service(
            telegram_service=self._build_telegram_service(),
            message_repository=MessageRepository(self._database),
            file_repository=FileRepository(self._database),
            download_record_repository=DownloadRecordRepository(self._database),
            logger=download_logger,
            telegraph_repository=TelegraphRepository(self._database),
        )

    def _build_telegram_service(self) -> TelegramService:
        return self._service_factory.telegram_service()

    def _on_download_progress(self, progress) -> None:
        metrics = format_download_progress_metrics(progress)
        self._report_label.setText(
            f"下载报告：task_id={progress.task_id}，{metrics}，已处理={progress.done_count}/{progress.total_count}，"
            f"成功={progress.success_count}，失败={progress.failed_count}，跳过={progress.skipped_count}"
        )

    def _on_download_completed(self, report) -> None:
        self._report_label.setText(
            f"下载报告：task_id={report.task_id}，总数={report.total_count}，成功={report.success_count}，"
            f"失败={report.failed_count}，跳过={report.skipped_count}"
        )

    def _on_download_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "搜索结果媒体下载失败", f"{error_code}: {message}")

    def _on_download_cancelled(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        self._report_label.setText(f"下载报告：{message}")

    def _on_download_worker_finished(self) -> None:
        self._download_worker = None
        self._download_thread = None
        self._set_buttons_enabled(True)

    def _show_task_summary(self) -> None:
        tasks = self._repository.latest_tasks(limit=5)
        if not tasks:
            self._report_label.setText("任务报告：暂无搜索任务")
            return
        summary = "；".join(
            f"#{task.id} {task.keyword} {task.status} 保存{task.total_saved}" for task in tasks if task.id is not None
        )
        self._report_label.setText(f"最近任务：{summary}")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._refresh_button.setEnabled(enabled)
        self._apply_filter_button.setEnabled(enabled)
        self._check_all_button.setEnabled(enabled)
        self._uncheck_all_button.setEnabled(enabled)
        self._preview_button.setEnabled(enabled)
        self._download_button.setEnabled(enabled)
        self._delete_results_button.setEnabled(enabled)
        self._delete_task_button.setEnabled(enabled)
        self._cancel_button.setEnabled(not enabled)
        self._task_combo.setEnabled(enabled)
        self._keyword_edit.setEnabled(enabled)
        self._type_combo.setEnabled(enabled)
        self._date_filter_checkbox.setEnabled(enabled)
        self._from_date_edit.setEnabled(enabled)
        self._to_date_edit.setEnabled(enabled)
        self._limit_spin.setEnabled(enabled)
        self._telegraph_image_limit_spin.setEnabled(enabled)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    @staticmethod
    def _display_result_type(result: SearchResult) -> str:
        if result.result_type == "telegraph_page":
            return "Telegraph 图片页面卡片"
        return result.result_type
