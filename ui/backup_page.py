"""Message backup and media download page."""

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
from database.models import MessageRecord
from database.repositories import (
    ChatRepository,
    DownloadRecordRepository,
    FileRepository,
    ForwardRepository,
    GroupRepository,
    MessageRepository,
    TaskRepository,
    TelegraphRepository,
)
from services.backup_service import BackupService
from services.config_service import ConfigService
from services.download_service import DownloadService, format_download_progress_metrics
from services.forward_service import ForwardService
from services.group_service import GroupService
from services.log_service import LogService
from services.runtime_state import RuntimeState
from services.service_factory import ApplicationContext, ServiceFactory
from services.telegraph_service import TelegraphService
from services.telegram_service import TelegramService
from ui.searchable_combo_box import SearchableComboBox as QComboBox
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.backup_worker import BackupWorker, MessageMediaDownloadWorker
from workers.forward_worker import ForwardWorker


class BackupPage(QWidget):
    """Back up Telegram messages and optionally download media."""

    COLUMNS = ["勾选", "chat_id", "消息ID", "日期", "发送者", "类型", "大小/图片数", "已下载", "已转发", "预览", "本地路径"]

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
        self._logger = log_service.get_logger("backup_page")
        self._download_logger = log_service.get_file_logger("download", "download.log")
        self._forward_logger = log_service.get_file_logger("forward", "forward.log")
        self._chat_repository = ChatRepository(database)
        self._message_repository = MessageRepository(database)
        self._file_repository = FileRepository(database)
        self._download_record_repository = DownloadRecordRepository(database)
        self._forward_repository = ForwardRepository(database)
        self._group_repository = GroupRepository(database)
        self._task_repository = TaskRepository(database)
        self._telegraph_repository = TelegraphRepository(database)
        self._thread: Optional[QThread] = None
        self._worker: Optional[object] = None
        self._forward_preview_keys: tuple[tuple[int, int, int], ...] = ()
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
        self._reload_chats()
        self._reload_target_chats()
        self._reload_messages()

    def _build_ui(self) -> None:
        title = QLabel("聊天预览与下载")
        title.setObjectName("pageTitle")

        parameter_group = QGroupBox("预览参数")
        self._chat_combo = QComboBox()
        self._chat_combo.setMaximumWidth(420)
        self._chat_combo.currentIndexChanged.connect(self._reload_messages)
        self._reload_chats_button = QPushButton("刷新聊天")
        self._reload_chats_button.clicked.connect(self._reload_chats)
        self._limit_spin = QSpinBox()
        self._limit_spin.setMaximumWidth(90)
        self._limit_spin.setRange(1, 5000)
        self._limit_spin.setValue(100)
        self._incremental_checkbox = QCheckBox("增量备份")
        self._incremental_checkbox.setChecked(True)
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
        self._retry_spin = QSpinBox()
        self._retry_spin.setMaximumWidth(70)
        self._retry_spin.setRange(1, 10)
        self._retry_spin.setValue(3)
        self._telegraph_image_limit_spin = QSpinBox()
        self._telegraph_image_limit_spin.setMaximumWidth(90)
        self._telegraph_image_limit_spin.setRange(0, 9999)
        self._telegraph_image_limit_spin.setSpecialValueText("全部")
        self._telegraph_image_limit_spin.setValue(0)

        parameter_layout = QVBoxLayout(parameter_group)
        parameter_layout.setContentsMargins(10, 8, 10, 8)
        parameter_layout.setSpacing(6)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(6)
        chat_row.addWidget(QLabel("聊天"))
        chat_row.addWidget(self._chat_combo)
        chat_row.addWidget(self._reload_chats_button)
        chat_row.addWidget(QLabel("最近 N 条"))
        chat_row.addWidget(self._limit_spin)
        chat_row.addWidget(self._incremental_checkbox)
        chat_row.addStretch(1)
        parameter_layout.addLayout(chat_row)

        option_row = QHBoxLayout()
        option_row.setSpacing(8)
        option_row.addWidget(self._date_filter_checkbox)
        option_row.addWidget(self._from_date_edit)
        option_row.addWidget(self._to_date_edit)
        option_row.addWidget(QLabel("失败重试"))
        option_row.addWidget(self._retry_spin)
        option_row.addWidget(QLabel("Telegraph \u56fe\u7247\u4e0b\u8f7d\u6570"))
        option_row.addWidget(self._telegraph_image_limit_spin)
        option_row.addStretch(1)
        parameter_layout.addLayout(option_row)

        forward_group = QGroupBox("聊天记录转发")
        self._target_strategy_combo = QComboBox()
        self._target_strategy_combo.setMaximumWidth(180)
        self._target_strategy_combo.addItem("转发到已选目标", "existing")
        self._target_strategy_combo.addItem("自动新建一个群组", "auto_group")
        self._target_chat_combo = QComboBox()
        self._target_chat_combo.setMaximumWidth(420)
        self._reload_targets_button = QPushButton("刷新目标")
        self._reload_targets_button.clicked.connect(self._reload_target_chats)
        self._forward_group_title_edit = QLineEdit()
        self._forward_group_title_edit.setPlaceholderText("自动新建群名称，留空则按聊天和日期生成")
        self._forward_group_title_edit.setMaximumWidth(320)
        self._forward_interval_spin = QSpinBox()
        self._forward_interval_spin.setMaximumWidth(70)
        self._forward_interval_spin.setRange(0, 60)
        self._forward_interval_spin.setValue(3)

        forward_layout = QVBoxLayout(forward_group)
        forward_layout.setContentsMargins(10, 8, 10, 8)
        forward_layout.setSpacing(6)
        forward_target_row = QHBoxLayout()
        forward_target_row.setSpacing(6)
        forward_target_row.addWidget(QLabel("策略"))
        forward_target_row.addWidget(self._target_strategy_combo)
        forward_target_row.addWidget(QLabel("目标"))
        forward_target_row.addWidget(self._target_chat_combo)
        forward_target_row.addWidget(self._reload_targets_button)
        forward_target_row.addStretch(1)
        forward_layout.addLayout(forward_target_row)
        forward_option_row = QHBoxLayout()
        forward_option_row.setSpacing(6)
        forward_option_row.addWidget(QLabel("新群名"))
        forward_option_row.addWidget(self._forward_group_title_edit)
        forward_option_row.addWidget(QLabel("发送间隔秒数"))
        forward_option_row.addWidget(self._forward_interval_spin)
        forward_option_row.addStretch(1)
        forward_layout.addLayout(forward_option_row)

        self._start_button = QPushButton("预览最近消息")
        self._start_button.clicked.connect(self._start_backup)
        self._download_selected_button = QPushButton("下载勾选媒体")
        self._download_selected_button.clicked.connect(self._start_selected_download)
        self._forward_selected_button = QPushButton("转发勾选记录")
        self._forward_selected_button.clicked.connect(self._start_selected_forward)
        self._cancel_button = QPushButton("取消任务")
        self._cancel_button.clicked.connect(self._cancel_current_task)
        self._cancel_button.setEnabled(False)
        self._refresh_messages_button = QPushButton("刷新本地消息")
        self._refresh_messages_button.clicked.connect(self._reload_messages)
        self._delete_selected_button = QPushButton("删除勾选记录")
        self._delete_selected_button.clicked.connect(self._delete_selected_messages)
        self._check_all_button = QPushButton("全选")
        self._check_all_button.clicked.connect(lambda: self._set_all_checked(True))
        self._uncheck_all_button = QPushButton("全不选")
        self._uncheck_all_button.clicked.connect(lambda: self._set_all_checked(False))
        self._preview_button = QPushButton("预览勾选")
        self._preview_button.clicked.connect(self._preview_selected_messages)
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)

        action_layout = QHBoxLayout()
        action_layout.setSpacing(8)
        action_layout.addWidget(self._start_button)
        action_layout.addWidget(self._download_selected_button)
        action_layout.addWidget(self._forward_selected_button)
        action_layout.addWidget(self._cancel_button)
        action_layout.addWidget(self._refresh_messages_button)
        action_layout.addWidget(self._delete_selected_button)
        action_layout.addWidget(self._check_all_button)
        action_layout.addWidget(self._uncheck_all_button)
        action_layout.addWidget(self._preview_button)
        action_layout.addWidget(self._progress_bar, 1)

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

        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(320)
        preview_page = QWidget()
        preview_layout = QVBoxLayout(preview_page)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self._preview_text)

        self._content_tabs = QTabWidget()
        self._content_tabs.addTab(table_page, "消息列表")
        self._content_tabs.addTab(preview_page, "勾选预览")

        self._status_label = QLabel("未预览")
        self._report_label = QLabel("任务报告：-")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(parameter_group)
        layout.addWidget(forward_group)
        layout.addLayout(action_layout)
        layout.addWidget(self._content_tabs, 1)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report_label)

    def _load_initial_values(self) -> None:
        self._limit_spin.setValue(int(self._config_service.get("backup.default_limit", 1000) or 1000))
        self._incremental_checkbox.setChecked(bool(self._config_service.get("backup.enable_incremental", True)))
        self._retry_spin.setValue(int(self._config_service.get("download.retry_count", 3) or 3))
        self._forward_interval_spin.setValue(int(self._config_service.get("forward.default_interval_seconds", 3) or 3))

    def _reload_chats(self) -> None:
        current_chat_id = self._chat_combo.currentData() if hasattr(self, "_chat_combo") else None
        self._chat_combo.blockSignals(True)
        chats = self._chat_repository.list_chats()
        self._chat_combo.clear()
        for chat in chats:
            self._chat_combo.addItem(f"{chat.title} ({chat.type}, {chat.tg_chat_id})", chat.tg_chat_id)
        if current_chat_id is not None:
            index = self._chat_combo.findData(current_chat_id)
            if index >= 0:
                self._chat_combo.setCurrentIndex(index)
        self._chat_combo.blockSignals(False)
        self._set_status(f"已加载聊天：{len(chats)} 个")

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
        self._set_status(f"已加载转发目标：{len(chats)} 个")

    def _reload_messages(self, _index: int = -1) -> None:
        chat_id = self._chat_combo.currentData() if hasattr(self, "_chat_combo") else None
        limit = self._limit_spin.value() if hasattr(self, "_limit_spin") else 200
        messages = self._message_repository.list_messages(int(chat_id), limit=limit) if chat_id is not None else self._message_repository.list_messages(limit=limit)
        self._populate_messages(messages)
        self._forward_preview_keys = ()
        self._preview_text.clear()
        self._content_tabs.setCurrentIndex(0)
        self._set_status(f"当前列表：{len(messages)} 条")

    def _populate_messages(self, messages: List[MessageRecord]) -> None:
        self._table.setRowCount(len(messages))
        for row, message in enumerate(messages):
            self._set_item(row, 0, "", checkable=True)
            self._set_item(row, 1, str(message.tg_chat_id))
            self._set_item(row, 2, str(message.message_id))
            self._set_item(row, 3, message.date)
            self._set_item(row, 4, message.sender_name)
            self._set_item(row, 5, self._display_message_type(message))
            self._set_item(row, 6, self._display_media_size_for_row(message))
            self._set_item(row, 7, "是" if message.is_downloaded else "否")
            self._set_item(row, 8, "是" if message.is_forwarded else "否")
            self._set_item(row, 9, message.text_preview)
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

    def _preview_selected_messages(self) -> None:
        selected_messages = self._selected_message_records()
        if not selected_messages:
            QMessageBox.information(self, "未勾选消息", "请至少勾选一条本地消息。")
            return

        preview = self._build_forward_service().preview_message_records(selected_messages, max_messages=50)
        if len(selected_messages) > 50:
            preview = f"{preview}\n\n---\n\n仅预览前 50 条，实际将发送 {len(selected_messages)} 条。"
        self._preview_text.setPlainText(preview)
        self._forward_preview_keys = self._message_record_keys(selected_messages)
        self._content_tabs.setCurrentIndex(1)
        self._set_status(f"已生成勾选预览：{len(selected_messages)} 条")

    def _start_backup(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前预览、下载或转发任务尚未完成。")
            return
        chat_id = self._chat_combo.currentData()
        if chat_id is None:
            QMessageBox.information(self, "未选择聊天", "请先在“聊天列表”同步并选择一个聊天。")
            return

        date_from = ""
        date_to = ""
        if self._date_filter_checkbox.isChecked():
            date_from = self._from_date_edit.date().toString("yyyy-MM-dd")
            date_to = self._to_date_edit.date().toString("yyyy-MM-dd")

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials
        self._progress_bar.setValue(0)
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = BackupWorker(
            service=self._build_backup_service(),
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "tg_chat_id": int(chat_id),
                "limit": self._limit_spin.value(),
                "date_from": date_from,
                "date_to": date_to,
                "incremental": self._incremental_checkbox.isChecked(),
                "download_media": False,
                "retry_count": self._retry_spin.value(),
                "selected_message_ids": [],
            },
            logger=self._log_service.get_logger("backup_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.progress_changed.connect(self._on_backup_progress)
        self._worker.backup_completed.connect(self._on_backup_completed)
        self._worker.cancelled.connect(self._on_task_cancelled)
        self._worker.failed.connect(self._on_backup_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _start_selected_download(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前预览、下载或转发任务尚未完成。")
            return

        selected_messages = self._selected_message_records()
        if not selected_messages:
            QMessageBox.information(self, "未勾选消息", "请至少勾选一条需要下载媒体的消息。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials

        self._progress_bar.setValue(0)
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = MessageMediaDownloadWorker(
            service=self._build_download_service(),
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "messages": selected_messages,
                "retry_count": self._retry_spin.value(),
                "telegraph_image_limit": self._telegraph_image_limit_spin.value(),
            },
            logger=self._log_service.get_logger("backup_download_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.progress_changed.connect(self._on_download_progress)
        self._worker.download_completed.connect(self._on_download_completed)
        self._worker.cancelled.connect(self._on_task_cancelled)
        self._worker.failed.connect(self._on_download_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _start_selected_forward(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前预览、下载或转发任务尚未完成。")
            return

        selected_messages = self._selected_message_records()
        if not selected_messages:
            QMessageBox.information(self, "未勾选消息", "请至少勾选一条需要转发的聊天记录。")
            return

        selected_keys = self._message_record_keys(selected_messages)
        if selected_keys != self._forward_preview_keys:
            self._preview_selected_messages()
            QMessageBox.information(self, "请确认预览", "已生成发送前预览，确认后请再次点击“转发勾选记录”。")
            return

        target_strategy = self._target_strategy_combo.currentData()
        target_chat_id = self._target_chat_combo.currentData()
        if target_strategy == "existing" and target_chat_id is None:
            QMessageBox.information(self, "未选择目标", "请先选择一个目标群，或选择自动新建群组。")
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
                "source_type": "message_record",
                "api_id": api_id,
                "api_hash": api_hash,
                "messages": selected_messages,
                "target_strategy": str(target_strategy or "existing"),
                "target_chat_id": int(target_chat_id) if target_chat_id is not None else 0,
                "group_title": self._forward_group_title_edit.text(),
                "interval_seconds": self._forward_interval_spin.value(),
            },
            logger=self._log_service.get_logger("message_forward_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.progress_changed.connect(self._on_forward_progress)
        self._worker.forward_completed.connect(self._on_forward_completed)
        self._worker.cancelled.connect(self._on_task_cancelled)
        self._worker.failed.connect(self._on_forward_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _cancel_current_task(self) -> None:
        if self._worker is None or not hasattr(self._worker, "cancel"):
            return
        self._worker.cancel()
        self._cancel_button.setEnabled(False)
        self._set_status("正在取消任务，当前步骤结束后会停止...")

    def _delete_selected_messages(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前预览、下载或转发任务尚未完成。")
            return

        selected_messages = self._selected_message_records()
        if not selected_messages:
            QMessageBox.information(self, "未勾选消息", "请至少勾选一条需要删除的本地消息记录。")
            return

        reply = QMessageBox.question(
            self,
            "确认删除本地消息记录",
            f"将删除 {len(selected_messages)} 条本地消息记录及其文件/下载/转发元数据，不会删除已下载到磁盘的媒体文件，也不会删除 Telegram 远端消息。是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        deleted = self._message_repository.delete_messages_by_keys(
            [(message.tg_chat_id, message.message_id) for message in selected_messages]
        )
        self._forward_preview_keys = ()
        self._preview_text.clear()
        self._reload_messages()
        self._set_status(f"已删除本地消息记录：{deleted} 条")

    def _selected_message_records(self) -> List[MessageRecord]:
        messages: List[MessageRecord] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, 0)
            chat_item = self._table.item(row, 1)
            id_item = self._table.item(row, 2)
            if (
                check_item is None
                or chat_item is None
                or id_item is None
                or check_item.checkState() != Qt.Checked
            ):
                continue
            try:
                tg_chat_id = int(chat_item.text())
                message_id = int(id_item.text())
            except ValueError:
                continue
            message = self._message_repository.get_by_chat_and_message_id(tg_chat_id, message_id)
            if message is not None:
                messages.append(message)
        return messages

    @staticmethod
    def _message_record_keys(messages: List[MessageRecord]) -> tuple[tuple[int, int, int], ...]:
        return tuple(
            (int(message.id or 0), int(message.tg_chat_id), int(message.message_id))
            for message in messages
        )

    def _build_telegram_service(self) -> TelegramService:
        return self._service_factory.telegram_service(chat_repository=self._chat_repository)

    def _build_backup_service(self) -> BackupService:
        telegram_service = self._build_telegram_service()
        return self._service_factory.backup_service(
            telegram_service=telegram_service,
            chat_repository=self._chat_repository,
            message_repository=self._message_repository,
            file_repository=self._file_repository,
            task_repository=self._task_repository,
            download_service=self._build_download_service(telegram_service),
            logger=self._download_logger,
            log_file=str(self._log_service.logs_dir / "download.log"),
        )

    def _build_download_service(self, telegram_service: Optional[TelegramService] = None) -> DownloadService:
        if telegram_service is None:
            telegram_service = self._build_telegram_service()
        return self._service_factory.download_service(
            telegram_service=telegram_service,
            message_repository=self._message_repository,
            file_repository=self._file_repository,
            download_record_repository=self._download_record_repository,
            logger=self._download_logger,
            telegraph_repository=self._telegraph_repository,
        )

    def _build_forward_service(self) -> ForwardService:
        telegram_service = self._build_telegram_service()
        return self._service_factory.forward_service(
            search_repository=None,
            forward_repository=self._forward_repository,
            task_repository=self._task_repository,
            telegram_service=telegram_service,
            logger=self._forward_logger,
            log_file=str(self._log_service.logs_dir / "forward.log"),
            group_service=self._build_group_service(telegram_service),
            message_repository=self._message_repository,
            max_per_task=int(self._config_service.get("forward.max_per_task", 100) or 0),
        )

    def _build_group_service(self, telegram_service: Optional[TelegramService] = None) -> GroupService:
        return self._service_factory.group_service(
            telegram_service=telegram_service or self._build_telegram_service(),
            chat_repository=self._chat_repository,
            group_repository=self._group_repository,
            logger=self._log_service.get_logger("group_service"),
        )

    def _on_backup_progress(self, progress) -> None:
        if progress.total_count > 0:
            self._progress_bar.setValue(int(progress.done_count * 100 / progress.total_count))
        else:
            self._progress_bar.setValue(100)
        self._report_label.setText(
            f"任务报告：task_id={progress.task_id}，已处理={progress.done_count}/{progress.total_count}，"
            f"保存={progress.saved_count}，下载={progress.downloaded_count}，失败={progress.failed_count}，跳过={progress.skipped_count}"
        )

    def _on_backup_completed(self, report) -> None:
        self._progress_bar.setValue(100)
        self._report_label.setText(
            f"任务报告：task_id={report.task_id}，总数={report.total_count}，保存={report.saved_count}，"
            f"下载={report.downloaded_count}，失败={report.failed_count}，跳过={report.skipped_count}，日志={report.log_file}"
        )
        self._reload_chats()
        self._reload_messages()

    def _on_download_progress(self, progress) -> None:
        progress_percent = int(getattr(progress, "progress_percent", 0) or 0)
        if progress_percent > 0:
            self._progress_bar.setValue(progress_percent)
        elif progress.total_count > 0:
            self._progress_bar.setValue(int(progress.done_count * 100 / progress.total_count))
        else:
            self._progress_bar.setValue(100)
        metrics = format_download_progress_metrics(progress)
        self._report_label.setText(
            f"下载报告：task_id={progress.task_id}，{metrics}，已处理={progress.done_count}/{progress.total_count}，"
            f"成功={progress.success_count}，失败={progress.failed_count}，跳过={progress.skipped_count}"
        )

    def _on_download_completed(self, report) -> None:
        self._progress_bar.setValue(100)
        self._report_label.setText(
            f"下载报告：task_id={report.task_id}，总数={report.total_count}，"
            f"成功={report.success_count}，失败={report.failed_count}，跳过={report.skipped_count}"
        )
        self._reload_messages()

    def _on_forward_progress(self, progress) -> None:
        if progress.total_count > 0:
            self._progress_bar.setValue(int(progress.done_count * 100 / progress.total_count))
        else:
            self._progress_bar.setValue(100)
        self._report_label.setText(
            f"转发报告：task_id={progress.task_id}，已处理={progress.done_count}/{progress.total_count}，"
            f"成功={progress.success_count}，失败={progress.failed_count}，跳过={progress.skipped_count}"
        )

    def _on_forward_completed(self, report) -> None:
        self._progress_bar.setValue(100)
        group_text = ""
        if getattr(report, "target_group_titles", ()):
            group_text = f"，目标群={'; '.join(report.target_group_titles)}"
        self._report_label.setText(
            f"转发报告：task_id={report.task_id}，总数={report.total_count}，"
            f"成功={report.success_count}，失败={report.failed_count}，跳过={report.skipped_count}"
            f"{group_text}，日志={report.log_file}"
        )
        self._forward_preview_keys = ()
        self._reload_target_chats()
        self._reload_messages()

    def _on_task_cancelled(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        self._report_label.setText(f"任务报告：{message}")
        self._reload_chats()
        self._reload_messages()

    def _on_backup_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "消息预览失败", f"{error_code}: {message}")

    def _on_download_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "勾选媒体下载失败", f"{error_code}: {message}")

    def _on_forward_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "聊天记录转发失败", f"{error_code}: {message}")

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_buttons_enabled(True)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)
        self._download_selected_button.setEnabled(enabled)
        self._forward_selected_button.setEnabled(enabled)
        self._refresh_messages_button.setEnabled(enabled)
        self._delete_selected_button.setEnabled(enabled)
        self._reload_chats_button.setEnabled(enabled)
        self._reload_targets_button.setEnabled(enabled)
        self._telegraph_image_limit_spin.setEnabled(enabled)
        self._check_all_button.setEnabled(enabled)
        self._uncheck_all_button.setEnabled(enabled)
        self._preview_button.setEnabled(enabled)
        self._cancel_button.setEnabled(not enabled)

    @classmethod
    def _display_message_type(cls, message: MessageRecord) -> str:
        media_type = str(message.media_type or message.message_type or "").lower()
        if media_type == "telegraph_page" or cls._message_has_telegraph_link(message):
            return "Telegraph 图片页面卡片"
        if media_type == "photo":
            return "图片"
        if media_type == "video":
            return "视频"
        if cls._message_text_has_link(message):
            return "链接"
        if not message.has_media:
            return "文字"
        if media_type == "audio":
            return "音频"
        if media_type in {"document", "file"}:
            return "文件"
        return "媒体"

    @classmethod
    def _display_media_size(cls, message: MessageRecord) -> str:
        display_type = cls._display_message_type(message)
        if display_type == "Telegraph 图片页面卡片":
            return "图片数未知"
        if display_type not in {"图片", "视频", "文件", "音频", "媒体"}:
            return "-"
        if message.file_size is None:
            return "未知"
        return cls._format_size(message.file_size)

    def _display_media_size_for_row(self, message: MessageRecord) -> str:
        if self._display_message_type(message) == "Telegraph 图片页面卡片":
            image_count = self._telegraph_image_count_for_message(message)
            if image_count is None:
                return "图片数未知"
            return f"图片 {image_count} 张"
        return self._display_media_size(message)

    def _telegraph_image_count_for_message(self, message: MessageRecord) -> Optional[int]:
        if message.id is None:
            return None
        for url in self._telegraph_urls_from_message(message):
            page = self._telegraph_repository.get_page_by_message_id(
                int(message.id),
                TelegraphService.normalize_url(url),
            )
            if page is not None:
                return int(page.image_count)
        return None

    @staticmethod
    def _message_text_has_link(message: MessageRecord) -> bool:
        value = f"{message.text or ''} {message.text_preview or ''} {message.external_urls or ''}".lower()
        return any(marker in value for marker in ("http://", "https://", "www.", "t.me/", "telegram.me/", "tg://"))

    @staticmethod
    def _message_has_telegraph_link(message: MessageRecord) -> bool:
        return bool(BackupPage._telegraph_urls_from_message(message))

    @staticmethod
    def _telegraph_urls_from_message(message: MessageRecord) -> list[str]:
        value = "\n".join(
            [
                message.text or "",
                message.text_preview or "",
                message.source_link or "",
                message.external_urls or "",
            ]
        )
        return TelegraphService.extract_telegraph_urls(value)

    @staticmethod
    def _format_size(size: int) -> str:
        try:
            value = max(0, int(size))
        except (TypeError, ValueError):
            return "未知"
        units = ["B", "KB", "MB", "GB"]
        amount = float(value)
        unit_index = 0
        while amount >= 1024 and unit_index < len(units) - 1:
            amount /= 1024
            unit_index += 1
        if unit_index == 0:
            return f"{int(amount)} {units[unit_index]}"
        return f"{amount:.1f} {units[unit_index]}"
