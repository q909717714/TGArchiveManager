"""Dedicated page for native Telegram channel and group message search."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.models import Chat, SearchResult
from database.repositories import (
    ChatRepository,
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
from services.public_search_service import PublicSearchService
from services.runtime_state import RuntimeState
from services.service_factory import ApplicationContext, ServiceFactory
from services.telegram_service import TelegramService
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.search_worker import PublicSearchWorker, SearchResultDownloadWorker


class TelegramNativeSearchPage(QWidget):
    """Search within selected joined Telegram channels and groups."""

    COLUMNS = ["勾选", "ID", "排名", "来源", "类型", "媒体信息", "标题", "摘要", "链接", "状态"]

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
        self._logger = log_service.get_logger("telegram_native_search_page")
        self._search_logger = log_service.get_file_logger("public_search", "public_search.log")
        self._repository = PublicSearchRepository(database)
        self._thread: Optional[QThread] = None
        self._worker: Optional[PublicSearchWorker] = None
        self._download_thread: Optional[QThread] = None
        self._download_worker: Optional[SearchResultDownloadWorker] = None
        self._search_scope_chats: list[Chat] = []
        self._selected_scope_chat_ids: set[int] = set()
        self._scope_initialized = False
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
        title = QLabel("TG 频道搜索")
        title.setObjectName("pageTitle")

        search_group = QGroupBox("搜索条件")
        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("输入要在选中频道/群聊中搜索的关键词")
        self._max_results_spin = QSpinBox()
        self._max_results_spin.setRange(1, 100)
        self._max_results_spin.setValue(100)
        self._telegraph_image_limit_spin = QSpinBox()
        self._telegraph_image_limit_spin.setRange(0, 9999)
        self._telegraph_image_limit_spin.setSpecialValueText("全部")
        self._telegraph_image_limit_spin.setValue(0)

        search_form = QFormLayout(search_group)
        search_form.addRow("关键词", self._keyword_edit)
        search_form.addRow("最大结果数", self._max_results_spin)
        search_form.addRow("Telegraph 图片下载数", self._telegraph_image_limit_spin)

        self._channel_group = QGroupBox("搜索范围")
        self._channel_filter_edit = QLineEdit()
        self._channel_filter_edit.setPlaceholderText("筛选频道/群聊名称、username、标签、官方分组或 ID")
        self._channel_filter_edit.textChanged.connect(lambda _text: self._populate_channel_scope_list())
        self._channel_tree = QTreeWidget()
        self._channel_tree.setMinimumHeight(220)
        self._channel_tree.setColumnCount(1)
        self._channel_tree.setHeaderLabel("官方分组 / 频道")
        self._channel_tree.setRootIsDecorated(True)
        self._channel_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._channel_tree.itemChanged.connect(self._on_scope_item_changed)
        self._channel_count_label = QLabel("已选择 0 个频道/群聊")
        self._select_all_channels_button = QPushButton("全选")
        self._select_all_channels_button.clicked.connect(self._select_all_scope_chats)
        self._clear_channels_button = QPushButton("清空")
        self._clear_channels_button.clicked.connect(self._clear_scope_chats)
        self._refresh_channels_button = QPushButton("刷新频道")
        self._refresh_channels_button.clicked.connect(self._refresh_channels_and_task_summary)

        channel_button_layout = QHBoxLayout()
        channel_button_layout.addWidget(self._select_all_channels_button)
        channel_button_layout.addWidget(self._clear_channels_button)
        channel_button_layout.addWidget(self._refresh_channels_button)

        channel_layout = QVBoxLayout(self._channel_group)
        channel_layout.addWidget(self._channel_filter_edit)
        channel_layout.addLayout(channel_button_layout)
        channel_layout.addWidget(self._channel_tree, 1)
        channel_layout.addWidget(self._channel_count_label)

        self._start_button = QPushButton("开始搜索")
        self._start_button.clicked.connect(self._start_search)
        self._preview_button = QPushButton("预览选中卡片")
        self._preview_button.clicked.connect(self._preview_selected_cards)
        self._download_button = QPushButton("下载选中媒体")
        self._download_button.clicked.connect(self._download_selected_media)
        self._delete_button = QPushButton("删除选中结果")
        self._delete_button.clicked.connect(self._delete_selected_results)
        self._cancel_button = QPushButton("取消任务")
        self._cancel_button.clicked.connect(self._cancel_current_task)
        self._cancel_button.setEnabled(False)

        action_layout = QHBoxLayout()
        action_layout.addWidget(self._start_button)
        action_layout.addWidget(self._preview_button)
        action_layout.addWidget(self._download_button)
        action_layout.addWidget(self._delete_button)
        action_layout.addWidget(self._cancel_button)
        action_layout.addStretch(1)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)

        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(140)

        self._status_label = QLabel("未搜索")
        self._report_label = QLabel("任务报告：-")

        content_layout = QHBoxLayout()
        content_layout.addWidget(self._channel_group, 0)

        result_layout = QVBoxLayout()
        result_layout.addLayout(action_layout)
        result_layout.addWidget(self._table, 1)
        result_layout.addWidget(self._preview_text)
        content_layout.addLayout(result_layout, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(search_group)
        layout.addLayout(content_layout, 1)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report_label)

    def _load_initial_values(self) -> None:
        self._max_results_spin.setValue(int(self._config_service.get("public_search.default_max_results", 100) or 100))
        self._reload_channel_scope()
        self._show_latest_task_summary()

    def _reload_channel_scope(self) -> None:
        previous_available_ids = {int(chat.tg_chat_id) for chat in self._search_scope_chats}
        had_all_selected = bool(previous_available_ids) and previous_available_ids.issubset(self._selected_scope_chat_ids)
        chats = [
            chat
            for chat in ChatRepository(self._database).list_chats()
            if self._is_search_scope_chat(chat)
        ]
        chats = self._sort_scope_chats(chats)
        available_ids = {int(chat.tg_chat_id) for chat in chats}
        self._search_scope_chats = chats
        if not self._scope_initialized or had_all_selected:
            self._selected_scope_chat_ids = set(available_ids)
        else:
            self._selected_scope_chat_ids.intersection_update(available_ids)
        self._scope_initialized = True
        self._populate_channel_scope_list()

    def _populate_channel_scope_list(self) -> None:
        filter_text = self._channel_filter_edit.text().strip().lower()
        self._channel_tree.blockSignals(True)
        self._channel_tree.clear()
        previous_group = None
        group_item: Optional[QTreeWidgetItem] = None
        for chat in self._search_scope_chats:
            if filter_text and not self._chat_matches_filter(chat, filter_text):
                continue
            group_name = self._chat_folder_group(chat)
            if group_name != previous_group:
                group_item = QTreeWidgetItem([f"{group_name}"])
                group_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                group_item.setCheckState(0, Qt.Unchecked)
                group_item.setExpanded(True)
                self._channel_tree.addTopLevelItem(group_item)
                previous_group = group_name
            if group_item is None:
                continue
            item = QTreeWidgetItem([self._chat_scope_label(chat)])
            item.setData(0, Qt.UserRole, int(chat.tg_chat_id))
            item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if int(chat.tg_chat_id) in self._selected_scope_chat_ids else Qt.Unchecked)
            group_item.addChild(item)
        self._refresh_scope_group_titles_and_states()
        self._channel_tree.blockSignals(False)
        self._update_channel_scope_summary()

    def _on_scope_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        if item.parent() is None:
            self._apply_group_check_state(item)
        else:
            chat_id = self._chat_id_for_scope_item(item)
            if chat_id is None:
                return
            if item.checkState(0) == Qt.Checked:
                self._selected_scope_chat_ids.add(chat_id)
            else:
                self._selected_scope_chat_ids.discard(chat_id)
            self._set_group_check_state(item.parent())
        self._update_channel_scope_summary()

    def _select_all_scope_chats(self) -> None:
        self._selected_scope_chat_ids = {int(chat.tg_chat_id) for chat in self._search_scope_chats}
        self._populate_channel_scope_list()

    def _clear_scope_chats(self) -> None:
        self._selected_scope_chat_ids.clear()
        self._populate_channel_scope_list()

    def _selected_native_chat_ids(self) -> list[int]:
        return [
            int(chat.tg_chat_id)
            for chat in self._search_scope_chats
            if int(chat.tg_chat_id) in self._selected_scope_chat_ids
        ]

    def _update_channel_scope_summary(self) -> None:
        available_ids = {int(chat.tg_chat_id) for chat in self._search_scope_chats}
        selected_count = len(self._selected_scope_chat_ids.intersection(available_ids))
        total_count = len(self._search_scope_chats)
        visible_count = self._visible_scope_chat_count()
        self._channel_count_label.setText(
            f"已选择 {selected_count}/{total_count} 个频道/群聊，当前显示 {visible_count} 个"
        )

    @staticmethod
    def _is_search_scope_chat(chat: Chat) -> bool:
        return str(chat.type or "").lower() in {"channel", "group"}

    @staticmethod
    def _chat_matches_filter(chat: Chat, filter_text: str) -> bool:
        haystack = " ".join(
            [
                str(chat.title or ""),
                str(chat.username or ""),
                str(chat.tag or ""),
                str(chat.telegram_folder_names or ""),
                str(chat.type or ""),
                str(chat.tg_chat_id),
            ]
        ).lower()
        return filter_text in haystack

    @staticmethod
    def _chat_scope_label(chat: Chat) -> str:
        username = f" @{chat.username}" if chat.username else ""
        tag = f" [{chat.tag}]" if chat.tag else ""
        return f"{chat.title}{username}{tag} ({chat.type}, {chat.tg_chat_id})"

    @staticmethod
    def _chat_folder_group(chat: Chat) -> str:
        return str(chat.telegram_folder_names or "未分组").strip() or "未分组"

    @classmethod
    def _sort_scope_chats(cls, chats: list[Chat]) -> list[Chat]:
        return sorted(
            chats,
            key=lambda chat: (
                cls._chat_folder_group(chat).lower(),
                str(chat.title or "").lower(),
                int(chat.tg_chat_id),
            ),
        )

    def _apply_group_check_state(self, group_item: QTreeWidgetItem) -> None:
        checked = group_item.checkState(0) == Qt.Checked
        self._channel_tree.blockSignals(True)
        for child_index in range(group_item.childCount()):
            child = group_item.child(child_index)
            chat_id = self._chat_id_for_scope_item(child)
            if chat_id is None:
                continue
            if checked:
                self._selected_scope_chat_ids.add(chat_id)
                child.setCheckState(0, Qt.Checked)
            else:
                self._selected_scope_chat_ids.discard(chat_id)
                child.setCheckState(0, Qt.Unchecked)
        self._set_group_check_state(group_item, block_signals=False)
        self._channel_tree.blockSignals(False)

    def _refresh_scope_group_titles_and_states(self) -> None:
        for index in range(self._channel_tree.topLevelItemCount()):
            group_item = self._channel_tree.topLevelItem(index)
            group_name = str(group_item.text(0)).split(" (", 1)[0]
            group_item.setText(0, f"{group_name} ({group_item.childCount()})")
            self._set_group_check_state(group_item, block_signals=False)
        self._channel_tree.expandAll()

    def _set_group_check_state(self, group_item: QTreeWidgetItem, block_signals: bool = True) -> None:
        total_count = group_item.childCount()
        checked_count = 0
        for child_index in range(total_count):
            child = group_item.child(child_index)
            chat_id = self._chat_id_for_scope_item(child)
            if chat_id is not None and chat_id in self._selected_scope_chat_ids:
                checked_count += 1

        if checked_count == 0:
            state = Qt.Unchecked
        elif checked_count == total_count:
            state = Qt.Checked
        else:
            state = Qt.PartiallyChecked

        if block_signals:
            self._channel_tree.blockSignals(True)
        group_item.setCheckState(0, state)
        if block_signals:
            self._channel_tree.blockSignals(False)

    def _visible_scope_chat_count(self) -> int:
        visible_count = 0
        for index in range(self._channel_tree.topLevelItemCount()):
            visible_count += self._channel_tree.topLevelItem(index).childCount()
        return visible_count

    @staticmethod
    def _chat_id_for_scope_item(item: QTreeWidgetItem) -> Optional[int]:
        try:
            return int(item.data(0, Qt.UserRole))
        except (TypeError, ValueError):
            return None

    def _start_search(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前 TG 频道搜索任务尚未完成。")
            return
        if self._download_thread is not None:
            QMessageBox.information(self, "下载任务运行中", "当前搜索结果媒体下载尚未完成。")
            return

        keyword = self._keyword_edit.text().strip()
        if not keyword:
            QMessageBox.information(self, "缺少关键词", "请输入搜索关键词。")
            return

        target_chat_ids = self._selected_native_chat_ids()
        if not target_chat_ids:
            QMessageBox.information(self, "缺少搜索范围", "请选择至少一个频道/群聊后再执行 TG 频道搜索。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials

        service = self._build_search_service(target_chat_ids)
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = PublicSearchWorker(
            service=service,
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "engine_name": "telegram_native",
                "keyword": keyword,
                "max_results": self._max_results_spin.value(),
            },
            logger=self._log_service.get_logger("telegram_native_search_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.search_completed.connect(self._on_search_completed)
        self._worker.cancelled.connect(self._on_search_cancelled)
        self._worker.failed.connect(self._on_search_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _build_search_service(self, target_chat_ids: list[int]) -> PublicSearchService:
        return self._service_factory.telegram_native_public_search_service(
            target_chat_ids=target_chat_ids,
            repository=self._repository,
            logger=self._search_logger,
        )

    def _build_telegram_service(self) -> TelegramService:
        return self._service_factory.telegram_service()

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

    def _on_search_completed(self, report) -> None:
        self._populate_table(list(report.results))
        self._show_search_report(report)
        self._set_status(f"搜索完成：保存 {report.total_saved} 条。")

    def _on_search_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "TG 频道搜索失败", f"{error_code}: {message}")

    def _on_search_cancelled(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        self._show_latest_task_summary()

    def _show_search_report(self, report) -> None:
        self._report_label.setText(
            f"任务报告：task_id={report.task_id}，总数={report.total_found}，保存={report.total_saved}，"
            f"重复={report.skipped_count}，日志={report.log_file}"
        )

    def _populate_table(self, results: list[SearchResult]) -> None:
        self._current_results = list(results)
        self._table.setRowCount(len(results))
        for row, result in enumerate(results):
            status = self._result_status_text(result)
            self._set_item(row, 0, "", checkable=True)
            self._set_item(row, 1, "" if result.id is None else str(result.id))
            self._set_item(row, 2, str(result.rank_no))
            self._set_item(row, 3, result.engine_name)
            self._set_item(row, 4, self._display_result_type(result))
            self._set_item(row, 5, ForwardService.result_media_info(result) or "-")
            self._set_item(row, 6, result.title)
            self._set_item(row, 7, result.summary)
            self._set_item(row, 8, result.url)
            self._set_item(row, 9, status)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)

    @staticmethod
    def _result_status_text(result: SearchResult) -> str:
        if result.is_duplicate:
            return "重复"
        return "已保存"

    @staticmethod
    def _display_result_type(result: SearchResult) -> str:
        if result.result_type == "telegraph_page":
            return "Telegraph 图片页面卡片"
        return result.result_type

    def _set_item(self, row: int, column: int, text: str, checkable: bool = False) -> None:
        item = QTableWidgetItem(text)
        flags = Qt.ItemIsSelectable | Qt.ItemIsEnabled
        if checkable:
            flags |= Qt.ItemIsUserCheckable
            item.setCheckState(Qt.Checked)
        item.setFlags(flags)
        self._table.setItem(row, column, item)

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
        results = self._repository.get_results_by_ids(result_ids[:10])
        preview = "\n\n---\n\n".join(ForwardService.format_card(result) for result in results)
        if len(result_ids) > 10:
            preview = f"{preview}\n\n---\n\n仅预览前 10 条，实际选中 {len(result_ids)} 条。"
        self._preview_text.setPlainText(preview)
        self._set_status(f"已生成预览：{min(len(result_ids), 10)}/{len(result_ids)} 条")

    def _download_selected_media(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "请等待当前 TG 频道搜索任务结束后再下载。")
            return
        if self._download_thread is not None:
            QMessageBox.information(self, "下载任务运行中", "当前搜索结果媒体下载尚未完成。")
            return

        result_ids = self._selected_result_ids()
        if not result_ids:
            QMessageBox.information(self, "未选择结果", "请至少勾选一条搜索结果。")
            return
        results = self._repository.get_results_by_ids(result_ids)
        if not results:
            QMessageBox.information(self, "结果不存在", "所选搜索结果不存在，请重新搜索或刷新任务。")
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

    def _delete_selected_results(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "请等待当前 TG 频道搜索任务结束后再删除。")
            return
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
        deleted_ids = set(result_ids)
        self._populate_table([result for result in self._current_results if result.id not in deleted_ids])
        self._preview_text.clear()
        self._show_latest_task_summary()
        self._set_status(f"已删除搜索结果：{deleted} 条")

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

    def _show_latest_task_summary(self) -> None:
        tasks = self._repository.latest_tasks(limit=5)
        if not tasks:
            self._report_label.setText("任务报告：暂无搜索任务")
            return
        summary = "；".join(
            f"#{task.id} {task.keyword} {task.status} 保存{task.total_saved}" for task in tasks if task.id is not None
        )
        self._report_label.setText(f"最近任务：{summary}")

    def _refresh_channels_and_task_summary(self) -> None:
        self._reload_channel_scope()
        self._show_latest_task_summary()

    def _cancel_current_task(self) -> None:
        requested = False
        for worker in (self._worker, self._download_worker):
            if worker is not None and hasattr(worker, "cancel"):
                worker.cancel()
                requested = True
        if requested:
            self._cancel_button.setEnabled(False)
            self._set_status("正在取消任务，当前步骤结束后会停止...")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_buttons_enabled(True)

    def _on_download_worker_finished(self) -> None:
        self._download_worker = None
        self._download_thread = None
        self._set_buttons_enabled(True)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)
        self._preview_button.setEnabled(enabled)
        self._download_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)
        self._select_all_channels_button.setEnabled(enabled)
        self._clear_channels_button.setEnabled(enabled)
        self._refresh_channels_button.setEnabled(enabled)
        self._channel_filter_edit.setEnabled(enabled)
        self._channel_tree.setEnabled(enabled)
        self._telegraph_image_limit_spin.setEnabled(enabled)
        self._cancel_button.setEnabled(not enabled)
