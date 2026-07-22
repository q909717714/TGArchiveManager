"""Application settings page."""

from __future__ import annotations

from typing import Any, Dict

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from services.config_service import ConfigError, ConfigService


class SettingsPage(QWidget):
    """Edit stable application configuration values."""

    BOT_ENGINE_NAMES = ("jisou", "soso", "kuai")

    def __init__(self, config_service: ConfigService, parent=None):
        super().__init__(parent)
        self._config_service = config_service
        self._bot_engines: Dict[str, Dict[str, object]] = {}

        self._build_ui()
        self._load_from_config()

    def _build_ui(self) -> None:
        title = QLabel("设置")
        title.setObjectName("pageTitle")

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        content_layout.addWidget(self._build_telegram_group())
        content_layout.addWidget(self._build_search_group())
        content_layout.addWidget(self._build_forward_group())
        content_layout.addWidget(self._build_backup_download_group())
        content_layout.addWidget(self._build_export_log_group())
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        self._save_button = QPushButton("保存配置")
        self._save_button.clicked.connect(self._save_config)
        self._reload_button = QPushButton("重新加载")
        self._reload_button.clicked.connect(self._reload_config)
        self._status_label = QLabel("未保存")

        action_layout = QHBoxLayout()
        action_layout.addWidget(self._save_button)
        action_layout.addWidget(self._reload_button)
        action_layout.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(scroll, 1)
        layout.addLayout(action_layout)
        layout.addWidget(self._status_label)

    def _build_telegram_group(self) -> QGroupBox:
        group = QGroupBox("Telegram")
        layout = QFormLayout(group)
        self._api_id_edit = QLineEdit()
        self._api_hash_edit = QLineEdit()
        self._session_name_edit = QLineEdit()
        self._session_dir_edit = QLineEdit()
        layout.addRow("API ID", self._api_id_edit)
        layout.addRow("API Hash", self._api_hash_edit)
        layout.addRow("Session 名称", self._session_name_edit)
        layout.addRow("Session 目录", self._session_dir_edit)
        return group

    def _build_search_group(self) -> QGroupBox:
        group = QGroupBox("搜索")
        layout = QFormLayout(group)
        self._public_search_enabled_check = QCheckBox("启用公开搜索")
        self._default_max_results_spin = self._spin_box(1, 5000)
        self._duplicate_check = QCheckBox("保存时标记重复结果")
        self._require_preview_check = QCheckBox("转发前要求预览")
        self._telegram_native_enabled_check = QCheckBox("启用 TG 频道搜索")

        layout.addRow("", self._public_search_enabled_check)
        layout.addRow("默认结果数", self._default_max_results_spin)
        layout.addRow("", self._duplicate_check)
        layout.addRow("", self._require_preview_check)
        layout.addRow("", self._telegram_native_enabled_check)

        for engine_name in self.BOT_ENGINE_NAMES:
            enabled = QCheckBox(f"启用 {engine_name}")
            username = QLineEdit()
            rate_limit = self._spin_box(0, 3600)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(enabled)
            row_layout.addWidget(QLabel("Bot"))
            row_layout.addWidget(username)
            row_layout.addWidget(QLabel("分页间隔秒"))
            row_layout.addWidget(rate_limit)
            self._bot_engines[engine_name] = {
                "enabled": enabled,
                "username": username,
                "rate_limit": rate_limit,
            }
            layout.addRow(engine_name, row)
        return group

    def _build_forward_group(self) -> QGroupBox:
        group = QGroupBox("转发")
        layout = QFormLayout(group)
        self._forward_interval_spin = self._spin_box(0, 3600)
        self._forward_max_spin = self._spin_box(0, 100000)
        self._skip_duplicates_check = QCheckBox("跳过重复结果或已成功转发项")
        self._create_group_check = QCheckBox("默认使用自动建群")
        self._group_name_rule_edit = QLineEdit()
        layout.addRow("默认间隔秒数", self._forward_interval_spin)
        layout.addRow("单任务最大条数", self._forward_max_spin)
        layout.addRow("", self._skip_duplicates_check)
        layout.addRow("", self._create_group_check)
        layout.addRow("默认建群规则", self._group_name_rule_edit)
        return group

    def _build_backup_download_group(self) -> QGroupBox:
        group = QGroupBox("备份和下载")
        layout = QFormLayout(group)
        self._backup_limit_spin = self._spin_box(1, 100000)
        self._incremental_check = QCheckBox("默认增量备份")
        self._download_root_edit = QLineEdit()
        self._download_images_check = QCheckBox("下载图片")
        self._download_videos_check = QCheckBox("下载视频")
        self._download_documents_check = QCheckBox("下载文件")
        self._download_audio_check = QCheckBox("下载音频")
        self._max_file_size_spin = self._spin_box(0, 102400)
        self._retry_count_spin = self._spin_box(1, 20)
        self._skip_existing_check = QCheckBox("跳过已存在文件")
        layout.addRow("默认备份条数", self._backup_limit_spin)
        layout.addRow("", self._incremental_check)
        layout.addRow("下载目录", self._download_root_edit)
        layout.addRow("", self._download_images_check)
        layout.addRow("", self._download_videos_check)
        layout.addRow("", self._download_documents_check)
        layout.addRow("", self._download_audio_check)
        layout.addRow("最大文件 MB", self._max_file_size_spin)
        layout.addRow("失败重试次数", self._retry_count_spin)
        layout.addRow("", self._skip_existing_check)
        return group

    def _build_export_log_group(self) -> QGroupBox:
        group = QGroupBox("导出和日志")
        layout = QFormLayout(group)
        self._export_root_edit = QLineEdit()
        self._enable_csv_check = QCheckBox("CSV")
        self._enable_excel_check = QCheckBox("Excel")
        self._enable_json_check = QCheckBox("JSON")
        self._enable_html_check = QCheckBox("HTML")
        self._logs_root_edit = QLineEdit()
        self._log_level_combo = QComboBox()
        self._log_level_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self._retention_spin = self._spin_box(0, 3650)
        self._save_public_search_log_check = QCheckBox("保存公开搜索独立日志")
        self._save_forward_log_check = QCheckBox("保存转发独立日志")
        self._save_download_log_check = QCheckBox("保存下载独立日志")
        layout.addRow("导出目录", self._export_root_edit)
        layout.addRow("启用 CSV", self._enable_csv_check)
        layout.addRow("启用 Excel", self._enable_excel_check)
        layout.addRow("启用 JSON", self._enable_json_check)
        layout.addRow("启用 HTML", self._enable_html_check)
        layout.addRow("日志目录", self._logs_root_edit)
        layout.addRow("日志等级", self._log_level_combo)
        layout.addRow("日志保留天数", self._retention_spin)
        layout.addRow("", self._save_public_search_log_check)
        layout.addRow("", self._save_forward_log_check)
        layout.addRow("", self._save_download_log_check)
        return group

    def _load_from_config(self) -> None:
        self._api_id_edit.setText(str(self._config_service.get("telegram.api_id", "") or ""))
        self._api_hash_edit.setText(str(self._config_service.get("telegram.api_hash", "") or ""))
        self._session_name_edit.setText(str(self._config_service.get("telegram.session_name", "user") or "user"))
        self._session_dir_edit.setText(str(self._config_service.get("telegram.session_dir", "sessions") or "sessions"))

        self._public_search_enabled_check.setChecked(bool(self._config_service.get("public_search.enabled", True)))
        self._default_max_results_spin.setValue(self._int_value("public_search.default_max_results", 100))
        self._duplicate_check.setChecked(bool(self._config_service.get("public_search.duplicate_check", True)))
        self._require_preview_check.setChecked(
            bool(self._config_service.get("public_search.require_preview_before_forward", True))
        )
        self._telegram_native_enabled_check.setChecked(
            bool(self._config_service.get("search_engines.telegram_native.enabled", True))
        )

        for engine_name, controls in self._bot_engines.items():
            enabled = controls["enabled"]
            username = controls["username"]
            rate_limit = controls["rate_limit"]
            if isinstance(enabled, QCheckBox):
                enabled.setChecked(bool(self._config_service.get(f"search_engines.{engine_name}.enabled", False)))
            if isinstance(username, QLineEdit):
                username.setText(str(self._config_service.get(f"search_engines.{engine_name}.username", "") or ""))
            if isinstance(rate_limit, QSpinBox):
                rate_limit.setValue(self._int_value(f"search_engines.{engine_name}.rate_limit_seconds", 0))

        self._forward_interval_spin.setValue(self._int_value("forward.default_interval_seconds", 3))
        self._forward_max_spin.setValue(self._int_value("forward.max_per_task", 100))
        self._skip_duplicates_check.setChecked(bool(self._config_service.get("forward.skip_duplicates", True)))
        self._create_group_check.setChecked(bool(self._config_service.get("forward.create_group_before_forward", True)))
        self._group_name_rule_edit.setText(
            str(self._config_service.get("forward.default_group_name_rule", "搜索_{keyword}_{date}") or "")
        )

        self._backup_limit_spin.setValue(self._int_value("backup.default_limit", 1000))
        self._incremental_check.setChecked(bool(self._config_service.get("backup.enable_incremental", True)))
        self._download_root_edit.setText(str(self._config_service.get("download.root_dir", "downloads") or "downloads"))
        self._download_images_check.setChecked(bool(self._config_service.get("download.download_images", True)))
        self._download_videos_check.setChecked(bool(self._config_service.get("download.download_videos", True)))
        self._download_documents_check.setChecked(bool(self._config_service.get("download.download_documents", True)))
        self._download_audio_check.setChecked(bool(self._config_service.get("download.download_audio", True)))
        self._max_file_size_spin.setValue(self._int_value("download.max_file_size_mb", 500))
        self._retry_count_spin.setValue(self._int_value("download.retry_count", 3))
        self._skip_existing_check.setChecked(bool(self._config_service.get("download.skip_existing", True)))

        self._export_root_edit.setText(str(self._config_service.get("export.root_dir", "exports") or "exports"))
        self._enable_csv_check.setChecked(bool(self._config_service.get("export.enable_csv", True)))
        self._enable_excel_check.setChecked(bool(self._config_service.get("export.enable_excel", True)))
        self._enable_json_check.setChecked(bool(self._config_service.get("export.enable_json", True)))
        self._enable_html_check.setChecked(bool(self._config_service.get("export.enable_html", False)))
        self._logs_root_edit.setText(str(self._config_service.get("logs.root_dir", "logs") or "logs"))
        self._set_combo_text(self._log_level_combo, str(self._config_service.get("logs.level", "INFO") or "INFO"))
        self._retention_spin.setValue(self._int_value("logs.retention_days", 30))
        self._save_public_search_log_check.setChecked(
            bool(self._config_service.get("logs.save_public_search_log", True))
        )
        self._save_forward_log_check.setChecked(bool(self._config_service.get("logs.save_forward_log", True)))
        self._save_download_log_check.setChecked(bool(self._config_service.get("logs.save_download_log", True)))

    def _save_config(self) -> None:
        config = self._config_service.as_dict()
        self._write_telegram_config(config)
        self._write_search_config(config)
        self._write_forward_config(config)
        self._write_backup_download_config(config)
        self._write_export_log_config(config)

        try:
            self._config_service.save_config(config)
        except (ConfigError, OSError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            self._status_label.setText("保存失败")
            return

        self._status_label.setText("配置已保存")
        QMessageBox.information(self, "保存完成", "配置已保存到 config/config.yaml。")

    def _reload_config(self) -> None:
        try:
            self._config_service.load()
        except ConfigError as exc:
            QMessageBox.warning(self, "重新加载失败", str(exc))
            self._status_label.setText("重新加载失败")
            return
        self._load_from_config()
        self._status_label.setText("配置已重新加载")

    def _write_telegram_config(self, config: Dict[str, Any]) -> None:
        telegram = self._section(config, "telegram")
        telegram["api_id"] = self._api_id_edit.text().strip()
        telegram["api_hash"] = self._api_hash_edit.text().strip()
        telegram["session_name"] = self._session_name_edit.text().strip() or "user"
        telegram["session_dir"] = self._session_dir_edit.text().strip() or "sessions"

    def _write_search_config(self, config: Dict[str, Any]) -> None:
        public_search = self._section(config, "public_search")
        public_search["enabled"] = self._public_search_enabled_check.isChecked()
        public_search["default_max_results"] = self._default_max_results_spin.value()
        public_search["duplicate_check"] = self._duplicate_check.isChecked()
        public_search["require_preview_before_forward"] = self._require_preview_check.isChecked()
        public_search.setdefault("default_forward_mode", "card")

        engines = self._section(config, "search_engines")
        native = engines.setdefault("telegram_native", {})
        if isinstance(native, dict):
            native["enabled"] = self._telegram_native_enabled_check.isChecked()
            native["type"] = "telegram_native"
        for engine_name, controls in self._bot_engines.items():
            engine = engines.setdefault(engine_name, {})
            if not isinstance(engine, dict):
                engine = {}
                engines[engine_name] = engine
            engine["enabled"] = self._checked(controls["enabled"])
            engine["type"] = "telegram_bot"
            engine["username"] = self._text(controls["username"])
            engine["rate_limit_seconds"] = self._spin_value(controls["rate_limit"])

    def _write_forward_config(self, config: Dict[str, Any]) -> None:
        forward = self._section(config, "forward")
        forward["default_interval_seconds"] = self._forward_interval_spin.value()
        forward["max_per_task"] = self._forward_max_spin.value()
        forward["skip_duplicates"] = self._skip_duplicates_check.isChecked()
        forward["create_group_before_forward"] = self._create_group_check.isChecked()
        forward["default_group_name_rule"] = self._group_name_rule_edit.text().strip() or "搜索_{keyword}_{date}"

    def _write_backup_download_config(self, config: Dict[str, Any]) -> None:
        backup = self._section(config, "backup")
        backup["default_limit"] = self._backup_limit_spin.value()
        backup["enable_incremental"] = self._incremental_check.isChecked()

        download = self._section(config, "download")
        download["root_dir"] = self._download_root_edit.text().strip() or "downloads"
        download["download_images"] = self._download_images_check.isChecked()
        download["download_videos"] = self._download_videos_check.isChecked()
        download["download_documents"] = self._download_documents_check.isChecked()
        download["download_audio"] = self._download_audio_check.isChecked()
        download["max_file_size_mb"] = self._max_file_size_spin.value()
        download["retry_count"] = self._retry_count_spin.value()
        download["skip_existing"] = self._skip_existing_check.isChecked()

    def _write_export_log_config(self, config: Dict[str, Any]) -> None:
        export = self._section(config, "export")
        export["root_dir"] = self._export_root_edit.text().strip() or "exports"
        export["enable_csv"] = self._enable_csv_check.isChecked()
        export["enable_excel"] = self._enable_excel_check.isChecked()
        export["enable_json"] = self._enable_json_check.isChecked()
        export["enable_html"] = self._enable_html_check.isChecked()

        logs = self._section(config, "logs")
        logs["root_dir"] = self._logs_root_edit.text().strip() or "logs"
        logs["level"] = self._log_level_combo.currentText() or "INFO"
        logs["retention_days"] = self._retention_spin.value()
        logs["save_public_search_log"] = self._save_public_search_log_check.isChecked()
        logs["save_forward_log"] = self._save_forward_log_check.isChecked()
        logs["save_download_log"] = self._save_download_log_check.isChecked()

    @staticmethod
    def _spin_box(minimum: int, maximum: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(int(minimum), int(maximum))
        spin.setMaximumWidth(100)
        return spin

    def _int_value(self, dotted_key: str, default: int) -> int:
        try:
            return int(self._config_service.get(dotted_key, default))
        except (TypeError, ValueError):
            return int(default)

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: str) -> None:
        index = combo.findText(str(value).strip().upper())
        combo.setCurrentIndex(index if index >= 0 else combo.findText("INFO"))

    @staticmethod
    def _section(config: Dict[str, Any], key: str) -> Dict[str, Any]:
        section = config.setdefault(key, {})
        if isinstance(section, dict):
            return section
        replacement: Dict[str, Any] = {}
        config[key] = replacement
        return replacement

    @staticmethod
    def _checked(control: object) -> bool:
        return bool(control.isChecked()) if isinstance(control, QCheckBox) else False

    @staticmethod
    def _text(control: object) -> str:
        return control.text().strip() if isinstance(control, QLineEdit) else ""

    @staticmethod
    def _spin_value(control: object) -> int:
        return int(control.value()) if isinstance(control, QSpinBox) else 0
