"""Forward management page for stage-5 search result card forwarding."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QDate, Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDateEdit,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.models import SearchResult
from database.repositories import (
    ChatRepository,
    ForwardRepository,
    GroupRepository,
    PublicSearchRepository,
    TaskRepository,
)
from services.config_service import ConfigService
from services.forward_service import ForwardService
from services.group_service import GroupService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from services.service_factory import ApplicationContext, ServiceFactory
from services.telegram_service import TelegramService
from ui.searchable_combo_box import SearchableComboBox as QComboBox
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.forward_worker import ForwardWorker


class ForwardPage(QWidget):
    """Forward saved public search results as text cards."""

    COLUMNS = ["勾选", "ID", "任务", "排名", "来源", "类型", "标题", "链接", "重复", "创建时间", "转发状态"]

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
        self._logger = log_service.get_logger("forward_page")
        self._forward_logger = log_service.get_file_logger("forward", "forward.log")
        self._search_repository = PublicSearchRepository(database)
        self._chat_repository = ChatRepository(database)
        self._forward_repository = ForwardRepository(database)
        self._task_repository = TaskRepository(database)
        self._group_repository = GroupRepository(database)
        self._thread: Optional[QThread] = None
        self._worker: Optional[ForwardWorker] = None
        self._preview_confirmed_ids: tuple[int, ...] = ()
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
        self._reload_task_combo()
        self._reload_category_combo()
        self._reload_target_chats()
        self._reload_results()

    def _build_ui(self) -> None:
        title = QLabel("转发管理")
        title.setObjectName("pageTitle")

        source_group = QGroupBox("搜索结果来源")
        self._task_combo = QComboBox()
        self._task_combo.setMaximumWidth(360)
        self._task_combo.currentIndexChanged.connect(self._on_task_filter_changed)
        self._keyword_filter_edit = QLineEdit()
        self._keyword_filter_edit.setPlaceholderText("关键词、标题、摘要或链接")
        self._category_combo = QComboBox()
        self._category_combo.setMaximumWidth(160)
        self._category_combo.currentIndexChanged.connect(self._reload_results)
        self._date_filter_checkbox = QCheckBox("启用时间范围")
        self._from_date_edit = QDateEdit()
        self._from_date_edit.setDisplayFormat("yyyy-MM-dd")
        self._from_date_edit.setCalendarPopup(True)
        self._from_date_edit.setDate(QDate.currentDate().addDays(-7))
        self._from_date_edit.setMaximumWidth(120)
        self._to_date_edit = QDateEdit()
        self._to_date_edit.setDisplayFormat("yyyy-MM-dd")
        self._to_date_edit.setCalendarPopup(True)
        self._to_date_edit.setDate(QDate.currentDate())
        self._to_date_edit.setMaximumWidth(120)
        self._apply_filter_button = QPushButton("应用筛选")
        self._apply_filter_button.clicked.connect(self._reload_results)
        self._reload_results_button = QPushButton("刷新结果")
        self._reload_results_button.clicked.connect(self._reload_all_sources)
        self._check_all_button = QPushButton("全选")
        self._check_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self._uncheck_all_button = QPushButton("全不选")
        self._uncheck_all_button.clicked.connect(lambda: self._set_all_checked(False))
        self._preview_button = QPushButton("预览选中卡片")
        self._preview_button.clicked.connect(self._preview_selected_cards)
        self._delete_results_button = QPushButton("删除选中结果")
        self._delete_results_button.clicked.connect(self._delete_selected_results)

        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(10, 8, 10, 8)
        source_layout.setSpacing(6)
        task_row = QHBoxLayout()
        task_row.setSpacing(6)
        task_row.addWidget(QLabel("任务"))
        task_row.addWidget(self._task_combo)
        task_row.addStretch(1)
        task_row.addWidget(self._reload_results_button)
        task_row.addWidget(self._check_all_button)
        task_row.addWidget(self._uncheck_all_button)
        source_layout.addLayout(task_row)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(QLabel("过滤"))
        filter_row.addWidget(self._keyword_filter_edit, 1)
        filter_row.addWidget(self._category_combo)
        filter_row.addWidget(self._date_filter_checkbox)
        filter_row.addWidget(self._from_date_edit)
        filter_row.addWidget(self._to_date_edit)
        filter_row.addWidget(self._apply_filter_button)
        filter_row.addWidget(self._preview_button)
        filter_row.addWidget(self._delete_results_button)
        source_layout.addLayout(filter_row)

        parameter_group = QGroupBox("转发参数")
        self._target_strategy_combo = QComboBox()
        self._target_strategy_combo.setMaximumWidth(180)
        self._target_strategy_combo.addItem("转发到已选目标", "existing")
        self._target_strategy_combo.addItem("按类型自动建群", "category")
        self._target_strategy_combo.addItem("按日期自动建群", "date")
        self._group_prefix_edit = QLineEdit()
        self._group_prefix_edit.setPlaceholderText("自动建群名称前缀")
        self._group_prefix_edit.setMaximumWidth(180)
        self._target_chat_combo = QComboBox()
        self._target_chat_combo.setMaximumWidth(360)
        self._reload_chats_button = QPushButton("刷新目标")
        self._reload_chats_button.clicked.connect(self._reload_target_chats)

        parameter_layout = QVBoxLayout(parameter_group)
        parameter_layout.setContentsMargins(10, 8, 10, 8)
        parameter_layout.setSpacing(6)
        target_row = QHBoxLayout()
        target_row.setSpacing(6)
        target_row.addWidget(QLabel("策略"))
        target_row.addWidget(self._target_strategy_combo)
        target_row.addWidget(QLabel("建群前缀"))
        target_row.addWidget(self._group_prefix_edit)
        target_row.addWidget(QLabel("目标"))
        target_row.addWidget(self._target_chat_combo)
        target_row.addWidget(self._reload_chats_button)
        target_row.addStretch(1)
        parameter_layout.addLayout(target_row)

        self._interval_spin = QSpinBox()
        self._interval_spin.setMaximumWidth(70)
        self._interval_spin.setRange(0, 60)
        self._interval_spin.setValue(3)
        self._skip_duplicate_checkbox = QCheckBox("跳过重复结果或已成功转发项")
        self._skip_duplicate_checkbox.setChecked(True)
        self._require_preview_checkbox = QCheckBox("发送前要求预览确认")
        self._require_preview_checkbox.setChecked(True)
        setting_row = QHBoxLayout()
        setting_row.setSpacing(8)
        setting_row.addWidget(QLabel("间隔秒数"))
        setting_row.addWidget(self._interval_spin)
        setting_row.addWidget(self._skip_duplicate_checkbox)
        setting_row.addWidget(self._require_preview_checkbox)
        setting_row.addStretch(1)
        parameter_layout.addLayout(setting_row)

        self._start_button = QPushButton("开始卡片转发")
        self._start_button.clicked.connect(self._start_forward)
        self._cancel_button = QPushButton("取消任务")
        self._cancel_button.clicked.connect(self._cancel_current_task)
        self._cancel_button.setEnabled(False)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        action_layout.addWidget(self._start_button)
        action_layout.addWidget(self._cancel_button)
        action_layout.addWidget(self._progress_bar, 1)

        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(320)
        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self._preview_text)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(320)

        table_page = QWidget()
        table_layout = QVBoxLayout(table_page)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self._table)

        self._content_tabs = QTabWidget()
        self._content_tabs.addTab(table_page, "结果表格")
        self._content_tabs.addTab(preview_page, "发送预览")

        self._status_label = QLabel("未转发")
        self._report_label = QLabel("任务报告：-")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(source_group)

        layout.addWidget(parameter_group)
        layout.addLayout(action_layout)
        layout.addWidget(self._content_tabs, 1)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report_label)

    def _load_initial_values(self) -> None:
        self._interval_spin.setValue(int(self._config_service.get("forward.default_interval_seconds", 3) or 3))
        self._skip_duplicate_checkbox.setChecked(bool(self._config_service.get("forward.skip_duplicates", True)))
        self._require_preview_checkbox.setChecked(
            bool(self._config_service.get("public_search.require_preview_before_forward", True))
        )
        self._group_prefix_edit.setText(self._group_prefix_from_rule(self._config_service.get("forward.default_group_name_rule", "")))
        if bool(self._config_service.get("forward.create_group_before_forward", False)):
            index = self._target_strategy_combo.findData("category")
            if index >= 0:
                self._target_strategy_combo.setCurrentIndex(index)

    def _reload_all_sources(self) -> None:
        self._reload_task_combo()
        self._reload_category_combo()
        self._reload_results()

    def _reload_task_combo(self) -> None:
        current_task_id = self._task_combo.currentData()
        self._task_combo.blockSignals(True)
        self._task_combo.clear()
        self._task_combo.addItem("最近 200 条结果", None)
        for task in self._search_repository.latest_tasks(limit=30):
            if task.id is None:
                continue
            self._task_combo.addItem(f"#{task.id} {task.keyword} ({task.total_saved} 条)", task.id)
        if current_task_id is not None:
            index = self._task_combo.findData(current_task_id)
            if index >= 0:
                self._task_combo.setCurrentIndex(index)
        self._task_combo.blockSignals(False)

    def _on_task_filter_changed(self) -> None:
        self._reload_category_combo()
        self._reload_results()

    def _reload_category_combo(self) -> None:
        if not hasattr(self, "_category_combo"):
            return
        current_category = self._category_combo.currentData()
        task_id = self._task_combo.currentData() if hasattr(self, "_task_combo") else None
        self._category_combo.blockSignals(True)
        self._category_combo.clear()
        self._category_combo.addItem("全部类型", "")
        for result_type in self._search_repository.distinct_result_types(task_id):
            self._category_combo.addItem(result_type, result_type)
        if current_category:
            index = self._category_combo.findData(current_category)
            if index >= 0:
                self._category_combo.setCurrentIndex(index)
        self._category_combo.blockSignals(False)

    def _reload_results(self) -> None:
        task_id = self._task_combo.currentData() if hasattr(self, "_task_combo") else None
        created_from = ""
        created_to = ""
        if hasattr(self, "_date_filter_checkbox") and self._date_filter_checkbox.isChecked():
            created_from = f"{self._from_date_edit.date().toString('yyyy-MM-dd')} 00:00:00"
            created_to = f"{self._to_date_edit.date().toString('yyyy-MM-dd')} 23:59:59"
        results = self._search_repository.list_filtered_results(
            task_id=int(task_id) if task_id is not None else None,
            keyword=self._keyword_filter_edit.text() if hasattr(self, "_keyword_filter_edit") else "",
            result_type=self._category_combo.currentData() if hasattr(self, "_category_combo") else "",
            created_from=created_from,
            created_to=created_to,
            limit=500,
        )
        self._populate_results(results)
        self._preview_confirmed_ids = ()
        self._preview_text.clear()
        self._content_tabs.setCurrentIndex(0)
        self._set_status(f"已加载搜索结果：{len(results)} 条")

    def _reload_target_chats(self) -> None:
        current_chat_id = self._target_chat_combo.currentData() if hasattr(self, "_target_chat_combo") else None
        chats = [
            chat
            for chat in self._chat_repository.list_chats()
            if chat.type in {"group", "channel", "unknown"} or chat.is_created_by_tool
        ]
        self._target_chat_combo.clear()
        for chat in chats:
            self._target_chat_combo.addItem(f"{chat.title} ({chat.type}, {chat.tg_chat_id})", chat.tg_chat_id)
        if current_chat_id is not None:
            index = self._target_chat_combo.findData(current_chat_id)
            if index >= 0:
                self._target_chat_combo.setCurrentIndex(index)
        self._set_status(f"已加载目标聊天：{len(chats)} 个")

    def _populate_results(self, results: List[SearchResult]) -> None:
        self._table.setRowCount(len(results))
        for row, result in enumerate(results):
            self._set_item(row, 0, "", result, checkable=True)
            self._set_item(row, 1, "" if result.id is None else str(result.id), result)
            self._set_item(row, 2, "" if result.task_id is None else str(result.task_id), result)
            self._set_item(row, 3, str(result.rank_no), result)
            self._set_item(row, 4, result.engine_name, result)
            self._set_item(row, 5, result.result_type, result)
            self._set_item(row, 6, result.title, result)
            self._set_item(row, 7, result.url, result)
            self._set_item(row, 8, "是" if result.is_duplicate else "否", result)
            self._set_item(row, 9, result.created_at, result)
            self._set_item(row, 10, result.forward_status, result)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)

    def _set_item(self, row: int, column: int, text: str, result: SearchResult, checkable: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(Qt.Checked)
        item.setFlags(flags)
        item.setData(Qt.UserRole, result.id)
        self._table.setItem(row, column, item)

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _selected_result_ids(self) -> List[int]:
        selected: List[int] = []
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
        preview = self._build_forward_service().preview_search_result_cards(result_ids, max_cards=10)
        if len(result_ids) > 10:
            preview = f"{preview}\n\n---\n\n仅预览前 10 条，实际将发送 {len(result_ids)} 条。"
        self._preview_text.setPlainText(preview)
        self._preview_confirmed_ids = tuple(result_ids)
        self._content_tabs.setCurrentIndex(1)
        self._set_status(f"已生成预览：{min(len(result_ids), 10)}/{len(result_ids)} 条")

    def _delete_selected_results(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "请等待当前转发任务结束后再删除。")
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

        deleted = self._search_repository.delete_results_by_ids(result_ids)
        self._reload_all_sources()
        self._preview_confirmed_ids = ()
        self._preview_text.clear()
        self._set_status(f"已删除搜索结果：{deleted} 条")

    def _start_forward(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前转发任务尚未完成。")
            return

        result_ids = self._selected_result_ids()
        if not result_ids:
            QMessageBox.information(self, "未选择结果", "请至少勾选一条搜索结果。")
            return
        if self._require_preview_checkbox.isChecked() and tuple(result_ids) != self._preview_confirmed_ids:
            self._preview_selected_cards()
            QMessageBox.information(self, "请确认预览", "已生成发送前预览，确认后请再次点击开始卡片转发。")
            return

        target_strategy = self._target_strategy_combo.currentData()
        target_chat_id = self._target_chat_combo.currentData()
        if target_strategy == "existing" and target_chat_id is None:
            QMessageBox.information(self, "未选择目标", "请先选择一个目标群。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials
        self._progress_bar.setValue(0)
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = ForwardWorker(
            service=self._build_forward_service(),
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "result_ids": result_ids,
                "target_strategy": target_strategy,
                "target_chat_id": int(target_chat_id) if target_chat_id is not None else 0,
                "group_title_prefix": self._group_prefix_edit.text(),
                "interval_seconds": self._interval_spin.value(),
                "skip_duplicates": self._skip_duplicate_checkbox.isChecked(),
            },
            logger=self._log_service.get_logger("forward_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.progress_changed.connect(self._on_forward_progress)
        self._worker.forward_completed.connect(self._on_forward_completed)
        self._worker.cancelled.connect(self._on_forward_cancelled)
        self._worker.failed.connect(self._on_forward_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_forward_worker_finished)
        self._thread.start()

    def _cancel_current_task(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        self._cancel_button.setEnabled(False)
        self._set_status("正在取消转发任务，当前步骤结束后会停止...")

    def _build_telegram_service(self) -> TelegramService:
        return self._service_factory.telegram_service(chat_repository=self._chat_repository)

    def _build_forward_service(self) -> ForwardService:
        telegram_service = self._build_telegram_service()
        return self._service_factory.forward_service(
            search_repository=self._search_repository,
            forward_repository=self._forward_repository,
            task_repository=self._task_repository,
            telegram_service=telegram_service,
            logger=self._forward_logger,
            log_file=str(self._log_service.logs_dir / "forward.log"),
            group_service=self._build_group_service(telegram_service),
            max_per_task=int(self._config_service.get("forward.max_per_task", 100) or 0),
        )

    def _build_group_service(self, telegram_service: Optional[TelegramService] = None) -> GroupService:
        return self._service_factory.group_service(
            telegram_service=telegram_service or self._build_telegram_service(),
            chat_repository=self._chat_repository,
            group_repository=self._group_repository,
            logger=self._log_service.get_logger("group_service"),
        )

    def _on_forward_progress(self, progress) -> None:
        if progress.total_count > 0:
            self._progress_bar.setValue(int(progress.done_count * 100 / progress.total_count))
        if progress.source_id is not None:
            self._set_result_status(progress.source_id, progress.status)
        self._report_label.setText(
            f"任务报告：task_id={progress.task_id}，已处理={progress.done_count}/{progress.total_count}，"
            f"成功={progress.success_count}，失败={progress.failed_count}，跳过={progress.skipped_count}"
        )

    def _on_forward_completed(self, report) -> None:
        self._progress_bar.setValue(100)
        group_text = ""
        if getattr(report, "target_group_titles", ()):
            group_text = f"，目标群={'; '.join(report.target_group_titles)}"
        self._report_label.setText(
            f"任务报告：task_id={report.task_id}，总数={report.total_count}，成功={report.success_count}，"
            f"失败={report.failed_count}，跳过={report.skipped_count}{group_text}，日志={report.log_file}"
        )
        self._reload_results()

    def _on_forward_cancelled(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        self._report_label.setText(f"任务报告：{message}")
        self._reload_results()

    def _on_forward_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "卡片转发失败", f"{error_code}: {message}")

    def _set_result_status(self, result_id: int, status: str) -> None:
        for row in range(self._table.rowCount()):
            id_item = self._table.item(row, 1)
            if id_item is None:
                continue
            try:
                if int(id_item.text()) != int(result_id):
                    continue
            except ValueError:
                continue
            status_item = self._table.item(row, 10)
            if status_item is not None:
                status_item.setText(status)
            return

    def _on_forward_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_buttons_enabled(True)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    @staticmethod
    def _group_prefix_from_rule(rule: object) -> str:
        text = str(rule or "").strip()
        if not text:
            return "TG整理"
        prefix = text.split("{", 1)[0].strip("_ -")
        return prefix or "TG整理"

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)
        self._reload_results_button.setEnabled(enabled)
        self._apply_filter_button.setEnabled(enabled)
        self._check_all_button.setEnabled(enabled)
        self._uncheck_all_button.setEnabled(enabled)
        self._preview_button.setEnabled(enabled)
        self._delete_results_button.setEnabled(enabled)
        self._reload_chats_button.setEnabled(enabled)
        self._cancel_button.setEnabled(not enabled)
