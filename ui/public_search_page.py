"""Public search page for Telegram bot providers."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
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
    QVBoxLayout,
    QWidget,
)

from database.db import DatabaseManager
from database.models import SearchResult
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
from ui.searchable_combo_box import SearchableComboBox as QComboBox
from ui.telegram_credentials import telegram_credentials_or_warn
from workers.search_worker import PublicSearchWorker, SearchResultDownloadWorker, VerificationClickWorker


class PublicSearchPage(QWidget):
    """Run Telegram search providers and display saved results."""

    COLUMNS = ["勾选", "ID", "排名", "来源", "类型", "标题", "摘要", "链接", "状态"]

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
        self._logger = log_service.get_logger("public_search_page")
        self._search_logger = log_service.get_file_logger("public_search", "public_search.log")
        self._repository = PublicSearchRepository(database)
        self._thread: Optional[QThread] = None
        self._worker: Optional[PublicSearchWorker] = None
        self._verification_thread: Optional[QThread] = None
        self._verification_worker: Optional[VerificationClickWorker] = None
        self._download_thread: Optional[QThread] = None
        self._download_worker: Optional[SearchResultDownloadWorker] = None
        self._pending_verification: Optional[dict] = None
        self._current_verification_media_path = ""
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
        title = QLabel("Bot 公开搜索")
        title.setObjectName("pageTitle")

        search_group = QGroupBox("搜索条件")
        self._keyword_edit = QLineEdit()
        self._keyword_edit.setPlaceholderText("输入 Bot 搜索关键词")
        self._max_results_spin = QSpinBox()
        self._max_results_spin.setRange(1, 100)
        self._max_results_spin.setValue(100)
        self._telegraph_image_limit_spin = QSpinBox()
        self._telegraph_image_limit_spin.setRange(0, 9999)
        self._telegraph_image_limit_spin.setSpecialValueText("全部")
        self._telegraph_image_limit_spin.setValue(0)
        self._engine_combo = QComboBox()
        self._engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        self._bot_username_edit = QLineEdit()
        self._bot_username_edit.setPlaceholderText("@jisou")

        search_form = QFormLayout(search_group)
        search_form.addRow("关键词", self._keyword_edit)
        search_form.addRow("搜索工具", self._engine_combo)
        search_form.addRow("Bot 用户名", self._bot_username_edit)
        search_form.addRow("最大结果数", self._max_results_spin)
        search_form.addRow("Telegraph 图片下载数", self._telegraph_image_limit_spin)

        self._start_button = QPushButton("开始搜索")
        self._start_button.clicked.connect(self._start_search)
        self._reload_tasks_button = QPushButton("刷新工具/任务")
        self._reload_tasks_button.clicked.connect(self._refresh_engines_and_task_summary)
        self._preview_button = QPushButton("预览选中卡片")
        self._preview_button.clicked.connect(self._preview_selected_cards)
        self._download_button = QPushButton("下载选中媒体")
        self._download_button.clicked.connect(self._download_selected_media)
        self._delete_button = QPushButton("删除选中结果")
        self._delete_button.clicked.connect(self._delete_selected_results)
        self._cancel_button = QPushButton("取消任务")
        self._cancel_button.clicked.connect(self._cancel_current_task)
        self._cancel_button.setEnabled(False)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self._start_button)
        button_layout.addWidget(self._reload_tasks_button)
        button_layout.addWidget(self._preview_button)
        button_layout.addWidget(self._download_button)
        button_layout.addWidget(self._delete_button)
        button_layout.addWidget(self._cancel_button)
        button_layout.addStretch(1)

        self._verification_group = QGroupBox("Bot 人机验证")
        self._verification_group.setVisible(False)
        self._verification_prompt_label = QLabel()
        self._verification_prompt_label.setWordWrap(True)
        self._verification_media_label = QLabel()
        self._verification_media_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._verification_media_label.setVisible(False)
        self._open_verification_media_button = QPushButton("打开验证附件")
        self._open_verification_media_button.setVisible(False)
        self._open_verification_media_button.clicked.connect(self._open_verification_media)
        self._verification_option_combo = QComboBox()
        self._submit_verification_button = QPushButton("提交验证")
        self._submit_verification_button.clicked.connect(self._submit_verification)
        self._hide_verification_button = QPushButton("隐藏")
        self._hide_verification_button.clicked.connect(self._hide_verification_panel)

        verification_button_layout = QHBoxLayout()
        verification_button_layout.addWidget(self._verification_option_combo)
        verification_button_layout.addWidget(self._submit_verification_button)
        verification_button_layout.addWidget(self._hide_verification_button)

        verification_layout = QVBoxLayout(self._verification_group)
        verification_layout.addWidget(self._verification_prompt_label)
        verification_layout.addWidget(self._verification_media_label)
        verification_layout.addWidget(self._open_verification_media_button)
        verification_layout.addLayout(verification_button_layout)

        self._table = QTableWidget(0, len(self.COLUMNS))
        self._table.setHorizontalHeaderLabels(self.COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)

        self._preview_text = QTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setMinimumHeight(150)

        self._status_label = QLabel("未搜索")
        self._report_label = QLabel("任务报告：-")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(search_group)
        layout.addLayout(button_layout)
        layout.addWidget(self._verification_group)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._preview_text)
        layout.addWidget(self._status_label)
        layout.addWidget(self._report_label)

    def _load_initial_values(self) -> None:
        self._max_results_spin.setValue(int(self._config_service.get("public_search.default_max_results", 100) or 100))
        self._reload_engine_options()

    def _reload_engine_options(self) -> None:
        current_engine = ""
        current_data = self._engine_combo.currentData()
        if isinstance(current_data, dict):
            current_engine = str(current_data.get("engine_name", ""))

        self._engine_combo.blockSignals(True)
        self._engine_combo.clear()
        for option in self._search_engine_options():
            self._engine_combo.addItem(option["label"], option)

        if current_engine:
            for index in range(self._engine_combo.count()):
                data = self._engine_combo.itemData(index)
                if isinstance(data, dict) and data.get("engine_name") == current_engine:
                    self._engine_combo.setCurrentIndex(index)
                    break
        self._engine_combo.blockSignals(False)
        self._on_engine_changed()

    def _search_engine_options(self) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        seen_keys: set[str] = set()

        engines = self._config_service.get("search_engines", {}) or {}
        if isinstance(engines, dict):
            for engine_name, values in engines.items():
                if not isinstance(values, dict) or not bool(values.get("enabled", False)):
                    continue
                engine_type = str(values.get("type", "") or "").strip()
                if engine_type == "telegram_bot":
                    username = self._normalize_bot_username(values.get("username", ""))
                    if username:
                        self._append_engine_option(
                            options,
                            seen_keys,
                            label=f"{engine_name} ({username})",
                            engine_name=str(engine_name),
                            engine_type="telegram_bot",
                            username=username,
                        )

        for chat in ChatRepository(self._database).list_chats():
            username = self._normalize_bot_username(chat.username)
            if not username or not username.lower().endswith("bot"):
                continue
            label = f"{chat.title or username} ({username})"
            self._append_engine_option(
                options,
                seen_keys,
                label=label,
                engine_name=self._engine_name_from_bot_username(username),
                engine_type="telegram_bot",
                username=username,
            )

        self._append_engine_option(
            options,
            seen_keys,
            label="自定义 Bot",
            engine_name="custom_bot",
            engine_type="telegram_bot",
            username="",
        )
        return options

    @staticmethod
    def _append_engine_option(
        options: list[dict[str, str]],
        seen_keys: set[str],
        label: str,
        engine_name: str,
        engine_type: str,
        username: str,
    ) -> None:
        key = f"{engine_type}:{username.lower() if username else engine_name}"
        if key in seen_keys:
            return
        seen_keys.add(key)
        options.append(
            {
                "label": str(label),
                "engine_name": str(engine_name),
                "type": str(engine_type),
                "username": str(username),
            }
        )

    def _on_engine_changed(self) -> None:
        data = self._selected_engine_data()
        username = str(data.get("username", ""))
        self._bot_username_edit.setEnabled(True)
        self._bot_username_edit.setText(username)
        self._bot_username_edit.setPlaceholderText("@bot_username")

    def _selected_engine_config(self) -> Optional[dict[str, str]]:
        data = self._selected_engine_data()
        engine_type = "telegram_bot"
        engine_name = data.get("engine_name", "")
        username = self._normalize_bot_username(self._bot_username_edit.text() or data.get("username", ""))
        if not username:
            QMessageBox.information(self, "缺少 Bot 用户名", "请输入 Bot 用户名，例如 @jisou。")
            return None
        if engine_name == "custom_bot":
            engine_name = self._engine_name_from_bot_username(username)
        return {
            "engine_name": engine_name or self._engine_name_from_bot_username(username),
            "type": engine_type,
            "username": username,
        }

    def _selected_engine_data(self) -> dict[str, str]:
        data = self._engine_combo.currentData()
        if isinstance(data, dict):
            return {str(key): str(value) for key, value in data.items()}
        return {"engine_name": "custom_bot", "type": "telegram_bot", "username": ""}

    @staticmethod
    def _normalize_bot_username(value: object) -> str:
        username = str(value or "").strip()
        if not username:
            return ""
        return username if username.startswith("@") else f"@{username}"

    @staticmethod
    def _engine_name_from_bot_username(username: str) -> str:
        raw = str(username or "").strip().lstrip("@").lower()
        safe = "".join(char if char.isalnum() else "_" for char in raw).strip("_")
        return f"bot_{safe or 'custom'}"

    def _start_search(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "当前公开搜索任务尚未完成。")
            return
        if self._verification_thread is not None:
            QMessageBox.information(self, "验证提交中", "当前 Bot 验证提交尚未完成。")
            return
        if self._download_thread is not None:
            QMessageBox.information(self, "下载任务运行中", "当前搜索结果媒体下载尚未完成。")
            return

        keyword = self._keyword_edit.text().strip()
        if not keyword:
            QMessageBox.information(self, "缺少关键词", "请输入搜索关键词。")
            return
        engine_config = self._selected_engine_config()
        if engine_config is None:
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials
        self._hide_verification_panel()
        service = self._build_search_service(engine_config)
        self._set_buttons_enabled(False)
        self._thread = QThread(self)
        self._worker = PublicSearchWorker(
            service=service,
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "engine_name": engine_config["engine_name"],
                "keyword": keyword,
                "max_results": self._max_results_spin.value(),
            },
            logger=self._log_service.get_logger("public_search_worker"),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.status_changed.connect(self._set_status)
        self._worker.search_completed.connect(self._on_search_completed)
        self._worker.verification_required.connect(self._on_verification_required)
        self._worker.cancelled.connect(self._on_search_cancelled)
        self._worker.failed.connect(self._on_search_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)
        self._thread.start()

    def _build_search_service(self, engine_config: Optional[dict[str, str]] = None) -> PublicSearchService:
        config = engine_config or self._selected_engine_config() or {
            "engine_name": "custom_bot",
            "type": "telegram_bot",
            "username": "",
        }
        engine_name = str(config.get("engine_name", "custom_bot") or "custom_bot")
        return self._service_factory.bot_public_search_service(
            {
                "engine_name": engine_name,
                "type": str(config.get("type", "telegram_bot") or "telegram_bot"),
                "username": self._normalize_bot_username(config.get("username", "")),
                "rate_limit_seconds": str(config.get("rate_limit_seconds", 0) or 0),
            },
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
        self._hide_verification_panel()
        self._populate_table(list(report.results))
        self._show_search_report(report)
        self._set_status(f"搜索完成：保存 {report.total_saved} 条。")

    def _show_search_report(self, report) -> None:
        self._report_label.setText(
            f"任务报告：task_id={report.task_id}，总数={report.total_found}，保存={report.total_saved}，"
            f"重复={report.skipped_count}，日志={report.log_file}"
        )

    def _on_verification_required(self, payload: object) -> None:
        data = dict(payload or {})
        options = [str(item).strip() for item in list(data.get("options", []) or []) if str(item).strip()]
        self._pending_verification = data
        self._verification_prompt_label.setText(str(data.get("prompt") or data.get("message") or "Bot 要求人机验证。"))
        self._set_verification_media(str(data.get("media_path", "") or ""))
        self._verification_option_combo.clear()
        self._verification_option_combo.addItems(options)
        self._verification_group.setVisible(True)
        self._submit_verification_button.setEnabled(bool(options) and self._thread is None and self._verification_thread is None)
        task_id = int(data.get("task_id", 0) or 0)
        task_text = f"任务 #{task_id} " if task_id > 0 else ""
        self._set_status(f"{data.get('error_code', 'SE005')}: {task_text}Bot 要求人机验证，请选择结果并提交。")
        if not options:
            QMessageBox.warning(self, "缺少验证选项", "Bot 要求人机验证，但当前响应没有可提交的按钮选项。请重新搜索获取最新验证消息。")

    def _on_search_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "公开搜索失败", f"{error_code}: {message}")

    def _on_search_cancelled(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        self._show_latest_task_summary()

    def _set_verification_media(self, media_path: str) -> None:
        clean_path = str(media_path).strip()
        self._current_verification_media_path = clean_path
        self._verification_media_label.clear()
        self._verification_media_label.setVisible(False)
        self._open_verification_media_button.setVisible(False)
        if not clean_path:
            return

        path = Path(clean_path)
        if not path.exists():
            self._verification_media_label.setText(f"验证图片不存在：{clean_path}")
            self._verification_media_label.setVisible(True)
            return

        self._open_verification_media_button.setVisible(True)
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._verification_media_label.setText(f"验证附件：{clean_path}")
            self._verification_media_label.setVisible(True)
            return

        self._verification_media_label.setPixmap(
            pixmap.scaled(520, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        self._verification_media_label.setVisible(True)

    def _open_verification_media(self) -> None:
        path = Path(self._current_verification_media_path)
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _submit_verification(self) -> None:
        if self._verification_thread is not None:
            QMessageBox.information(self, "验证提交中", "当前 Bot 验证提交尚未完成。")
            return
        if self._thread is not None:
            QMessageBox.information(self, "任务运行中", "请等待当前公开搜索任务结束后再提交验证。")
            return
        if not self._pending_verification:
            QMessageBox.information(self, "缺少验证信息", "请先执行搜索并获取 Bot 验证提示。")
            return

        button_text = self._verification_option_combo.currentText().strip()
        if not button_text:
            QMessageBox.information(self, "未选择验证结果", "请选择一个 Bot 验证选项。")
            return

        credentials = telegram_credentials_or_warn(self, self._runtime_state)
        if credentials is None:
            return
        api_id, api_hash = credentials
        self._verification_thread = QThread(self)
        self._verification_worker = VerificationClickWorker(
            service=self._build_search_service(
                {
                    "engine_name": str(self._pending_verification.get("engine_name", "jisou") or "jisou"),
                    "type": "telegram_bot",
                    "username": str(self._pending_verification.get("bot_username", "") or ""),
                }
            ),
            payload={
                "api_id": api_id,
                "api_hash": api_hash,
                "engine_name": self._pending_verification.get("engine_name", "jisou"),
                "keyword": self._pending_verification.get("keyword", self._keyword_edit.text().strip()),
                "max_results": self._pending_verification.get("max_results", self._max_results_spin.value()),
                "task_id": self._pending_verification.get("task_id", 0),
                "bot_username": self._pending_verification.get("bot_username", ""),
                "message_id": self._pending_verification.get("message_id", 0),
                "button_text": button_text,
            },
            logger=self._log_service.get_logger("verification_worker"),
        )
        self._set_buttons_enabled(False)
        self._submit_verification_button.setEnabled(False)
        self._verification_worker.moveToThread(self._verification_thread)
        self._verification_thread.started.connect(self._verification_worker.run)
        self._verification_worker.status_changed.connect(self._set_status)
        self._verification_worker.verification_completed.connect(self._on_verification_completed)
        self._verification_worker.verification_required.connect(self._on_verification_required)
        self._verification_worker.cancelled.connect(self._on_verification_cancelled)
        self._verification_worker.failed.connect(self._on_verification_failed)
        self._verification_worker.finished.connect(self._verification_thread.quit)
        self._verification_worker.finished.connect(self._verification_worker.deleteLater)
        self._verification_thread.finished.connect(self._verification_thread.deleteLater)
        self._verification_thread.finished.connect(self._on_verification_worker_finished)
        self._verification_thread.start()

    def _on_verification_completed(self, report) -> None:
        self._hide_verification_panel()
        self._populate_table(list(report.results))
        self._show_search_report(report)
        self._set_status(f"Bot 人机验证通过，搜索完成：保存 {report.total_saved} 条。")

    def _on_verification_failed(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        QMessageBox.warning(self, "验证提交失败", f"{error_code}: {message}")

    def _on_verification_cancelled(self, error_code: str, message: str) -> None:
        self._set_status(f"{error_code}: {message}")
        self._show_latest_task_summary()

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
            self._set_item(row, 5, result.title)
            self._set_item(row, 6, result.summary)
            self._set_item(row, 7, result.url)
            self._set_item(row, 8, status)
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
            QMessageBox.information(self, "任务运行中", "请等待当前公开搜索任务结束后再下载。")
            return
        if self._verification_thread is not None:
            QMessageBox.information(self, "验证提交中", "请等待当前 Bot 验证提交结束后再下载。")
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
            QMessageBox.information(self, "任务运行中", "请等待当前公开搜索任务结束后再删除。")
            return
        if self._verification_thread is not None:
            QMessageBox.information(self, "验证提交中", "请等待当前 Bot 验证提交结束后再删除。")
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
        remaining = [result for result in self._current_results if result.id not in set(result_ids)]
        self._populate_table(remaining)
        self._preview_text.clear()
        self._show_latest_task_summary()
        self._set_status(f"已删除搜索结果：{deleted} 条")

    def _cancel_current_task(self) -> None:
        requested = False
        for worker in (self._worker, self._verification_worker, self._download_worker):
            if worker is not None and hasattr(worker, "cancel"):
                worker.cancel()
                requested = True
        if requested:
            self._cancel_button.setEnabled(False)
            self._set_status("正在取消任务，当前步骤结束后会停止...")

    def _show_latest_task_summary(self) -> None:
        tasks = self._repository.latest_tasks(limit=5)
        if not tasks:
            self._report_label.setText("任务报告：暂无公开搜索任务")
            return
        summary = "；".join(
            f"#{task.id} {task.keyword} {task.status} 保存{task.total_saved}" for task in tasks if task.id is not None
        )
        self._report_label.setText(f"最近任务：{summary}")

    def _refresh_engines_and_task_summary(self) -> None:
        self._reload_engine_options()
        self._show_latest_task_summary()

    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self._set_buttons_enabled(True)

    def _on_verification_worker_finished(self) -> None:
        self._verification_worker = None
        self._verification_thread = None
        self._set_buttons_enabled(True)

    def _on_download_worker_finished(self) -> None:
        self._download_worker = None
        self._download_thread = None
        self._set_buttons_enabled(True)

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        self._start_button.setEnabled(enabled)
        self._reload_tasks_button.setEnabled(enabled)
        self._preview_button.setEnabled(enabled)
        self._download_button.setEnabled(enabled)
        self._delete_button.setEnabled(enabled)
        self._engine_combo.setEnabled(enabled)
        self._bot_username_edit.setEnabled(enabled)
        self._telegraph_image_limit_spin.setEnabled(enabled)
        self._cancel_button.setEnabled(not enabled)
        if hasattr(self, "_submit_verification_button"):
            can_submit = enabled and self._verification_group.isVisible() and self._verification_option_combo.count() > 0
            self._submit_verification_button.setEnabled(can_submit)

    def _hide_verification_panel(self) -> None:
        self._pending_verification = None
        self._current_verification_media_path = ""
        self._verification_prompt_label.clear()
        self._verification_media_label.clear()
        self._verification_media_label.setVisible(False)
        self._open_verification_media_button.setVisible(False)
        self._verification_option_combo.clear()
        self._verification_group.setVisible(False)
