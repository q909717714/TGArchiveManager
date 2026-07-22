"""Logging setup for TGArchiveManager."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from utils.error_codes import describe_error_code


SENSITIVE_KEYS = ("api_hash", "password", "verification_code", "login_code")
LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
    r"(?P<level>[A-Z]+) \| (?P<logger>[^|]+) \| (?P<message>.*)$"
)
ERROR_CODE_PATTERN = re.compile(r"\b[A-Z]{2,3}\d{3}\b")
TASK_ID_PATTERNS = (
    re.compile(r"\btask_id\s*[=:]\s*([A-Za-z0-9_.:-]+)", re.IGNORECASE),
    re.compile(r"\btask\s+#?([A-Za-z0-9][A-Za-z0-9_.:-]*)\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class LogEntry:
    """One parsed log entry, including multiline traceback text if present."""

    raw: str
    timestamp: str
    level: str
    logger_name: str
    module: str
    message: str
    task_id: str
    error_code: str
    source_file: Path


@dataclass(frozen=True)
class LogQuery:
    """Filter options used by the log page and diagnostics tools."""

    module: str = ""
    level: str = ""
    task_id: str = ""
    keyword: str = ""
    files: tuple[Path, ...] = ()
    limit: int = 2000


class RedactingFilter(logging.Filter):
    """Redact sensitive tokens from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        lowered = message.lower()
        for key in SENSITIVE_KEYS:
            if key in lowered:
                record.msg = "[redacted sensitive log message]"
                record.args = ()
                break
        return True


class LogService:
    """Configure module loggers and application log files."""

    LOGGER_NAME = "tg_archive_manager"

    def __init__(self, project_root: Path, config: Dict[str, Any]):
        self._project_root = Path(project_root)
        self._config = config
        self._logs_dir = self._resolve_logs_dir()
        self._logger = logging.getLogger(self.LOGGER_NAME)

    @property
    def logs_dir(self) -> Path:
        return self._logs_dir

    @property
    def app_log_path(self) -> Path:
        return self._logs_dir / "app.log"

    @property
    def error_log_path(self) -> Path:
        return self._logs_dir / "error.log"

    def configure(self) -> logging.Logger:
        """Configure application and error log handlers."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        (self._logs_dir / "tasks").mkdir(parents=True, exist_ok=True)
        self._prune_old_logs()

        logger = self._logger
        logger.setLevel(self._configured_level())
        logger.propagate = False

        self._close_existing_handlers()

        formatter = self._formatter()
        redacting_filter = RedactingFilter()

        app_handler = logging.FileHandler(self.app_log_path, encoding="utf-8")
        app_handler.setLevel(logging.DEBUG)
        app_handler.setFormatter(formatter)
        app_handler.addFilter(redacting_filter)

        error_handler = logging.FileHandler(self.error_log_path, encoding="utf-8")
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        error_handler.addFilter(redacting_filter)

        logger.addHandler(app_handler)
        logger.addHandler(error_handler)

        return logger

    def get_logger(self, module_name: str) -> logging.Logger:
        """Return a child logger for a service, repository, or UI module."""
        if not module_name:
            return self._logger
        return logging.getLogger(f"{self.LOGGER_NAME}.{module_name}")

    def get_file_logger(self, module_name: str, filename: str) -> logging.Logger:
        """Return a child logger that also writes to a module-specific log file."""
        logger = self.get_logger(module_name)
        if not self._module_file_log_enabled(filename):
            return logger

        target_path = self._logs_dir / filename
        target_path.parent.mkdir(parents=True, exist_ok=True)

        normalized_target = str(target_path.resolve())
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler) and handler.baseFilename == normalized_target:
                return logger

        handler = logging.FileHandler(target_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(self._formatter())
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        return logger

    def task_log_path(self, task_id: str, task_type: str = "task") -> Path:
        """Return the independent log file path for one task."""
        safe_type = self._safe_token(task_type or "task")
        safe_task_id = self._safe_token(task_id or "unknown")
        return self._logs_dir / "tasks" / f"{safe_type}_{safe_task_id}.log"

    def get_task_logger(self, task_id: str, task_type: str = "task") -> logging.Logger:
        """Return a logger that writes to a task-specific log and propagates upward."""
        safe_type = self._safe_token(task_type or "task")
        safe_task_id = self._safe_token(task_id or "unknown")
        logger = logging.getLogger(f"{self.LOGGER_NAME}.{safe_type}.task.{safe_task_id}")
        logger.setLevel(logging.DEBUG)
        logger.propagate = True

        target_path = self.task_log_path(safe_task_id, safe_type)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        normalized_target = str(target_path.resolve())
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler) and handler.baseFilename == normalized_target:
                return logger

        handler = logging.FileHandler(target_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(self._formatter())
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
        return logger

    def list_log_files(self) -> List[Path]:
        """Return known log files in a stable order for the diagnostics page."""
        if not self._logs_dir.exists():
            return []

        preferred = [self.app_log_path, self.error_log_path]
        discovered = sorted(self._logs_dir.rglob("*.log"), key=lambda path: str(path.relative_to(self._logs_dir)).lower())
        result: list[Path] = []
        seen: set[str] = set()
        for path in preferred + discovered:
            resolved = str(path.resolve())
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            result.append(path)
        return result

    def read_entries(self, query: Optional[LogQuery] = None) -> List[LogEntry]:
        """Read, parse, and filter log entries from configured log files."""
        active_query = query or LogQuery()
        files = list(active_query.files) if active_query.files else self.list_log_files()
        entries: list[LogEntry] = []
        for path in files:
            log_path = Path(path)
            if not log_path.exists() or not log_path.is_file():
                continue
            entries.extend(self._parse_log_file(log_path))

        entries.sort(key=lambda entry: (entry.timestamp, str(entry.source_file), entry.raw))
        entries = [entry for entry in entries if self._matches_query(entry, active_query)]
        if active_query.limit > 0 and len(entries) > active_query.limit:
            entries = entries[-active_query.limit :]
        return entries

    def export_entries(self, entries: Iterable[LogEntry], target_path: Optional[Path] = None) -> Path:
        """Export currently filtered log entries into a standalone text file."""
        if target_path is None:
            export_dir = self._logs_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = export_dir / f"log_export_{timestamp}.log"
        else:
            target = Path(target_path)
            if not target.is_absolute():
                target = self._logs_dir / target
            target.parent.mkdir(parents=True, exist_ok=True)

        with target.open("w", encoding="utf-8", newline="\n") as stream:
            for entry in entries:
                stream.write(self.format_entry(entry, include_source=True))
                stream.write("\n")
        return target

    def latest_error_detail(self, entries: Iterable[LogEntry]) -> str:
        """Return a copyable detail block for the newest error-like entry."""
        entry_list = list(entries)
        for entry in reversed(entry_list):
            if entry.level in {"ERROR", "CRITICAL"} or entry.error_code or "Traceback" in entry.raw:
                lines = [
                    f"文件: {entry.source_file}",
                    f"时间: {entry.timestamp or '-'}",
                    f"等级: {entry.level or '-'}",
                    f"模块: {entry.logger_name or '-'}",
                ]
                if entry.task_id:
                    lines.append(f"任务 ID: {entry.task_id}")
                if entry.error_code:
                    lines.append(f"错误码: {entry.error_code}")
                    description = describe_error_code(entry.error_code)
                    if description:
                        lines.append(f"错误说明: {description}")
                lines.append("")
                lines.append(entry.raw)
                return "\n".join(lines)
        return ""

    def format_entry(self, entry: LogEntry, include_source: bool = False) -> str:
        """Format a parsed log entry for display or export."""
        if include_source:
            try:
                source = str(entry.source_file.relative_to(self._logs_dir))
            except ValueError:
                source = entry.source_file.name
            return f"[{source}] {entry.raw}"
        return entry.raw

    def _resolve_logs_dir(self) -> Path:
        value = self._config.get("logs", {}).get("root_dir", "logs")
        path = Path(str(value))
        if path.is_absolute():
            return path
        return self._project_root / path

    def _configured_level(self) -> int:
        value = str(self._config.get("logs", {}).get("level", "INFO")).upper()
        return getattr(logging, value, logging.INFO)

    def _module_file_log_enabled(self, filename: str) -> bool:
        flags = {
            "public_search.log": "save_public_search_log",
            "forward.log": "save_forward_log",
            "download.log": "save_download_log",
        }
        flag = flags.get(Path(str(filename)).name)
        if flag is None:
            return True
        return bool(self._config.get("logs", {}).get(flag, True))

    def _prune_old_logs(self) -> None:
        try:
            retention_days = int(self._config.get("logs", {}).get("retention_days", 0) or 0)
        except (TypeError, ValueError):
            retention_days = 0
        if retention_days <= 0 or not self._logs_dir.exists():
            return

        cutoff = datetime.now().timestamp() - retention_days * 24 * 60 * 60
        for path in self._logs_dir.rglob("*.log"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    def _formatter(self) -> logging.Formatter:
        return logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def _close_existing_handlers(self) -> None:
        for logger in self._all_project_loggers():
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def _all_project_loggers(self) -> list[logging.Logger]:
        loggers = [self._logger]
        prefix = f"{self.LOGGER_NAME}."
        for name, candidate in logging.Logger.manager.loggerDict.items():
            if name.startswith(prefix) and isinstance(candidate, logging.Logger):
                loggers.append(candidate)
        return loggers

    def _parse_log_file(self, path: Path) -> list[LogEntry]:
        entries: list[LogEntry] = []
        current: Optional[dict[str, Any]] = None

        def flush() -> None:
            if current is None:
                return
            raw = "\n".join(current["raw_lines"])
            message = "\n".join(current["message_lines"])
            logger_name = current["logger_name"]
            entries.append(
                LogEntry(
                    raw=raw,
                    timestamp=current["timestamp"],
                    level=current["level"],
                    logger_name=logger_name,
                    module=self._module_from_logger(logger_name),
                    message=message,
                    task_id=self._extract_task_id(raw),
                    error_code=self._extract_error_code(raw),
                    source_file=path,
                )
            )

        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for raw_line in stream:
                line = raw_line.rstrip("\n")
                match = LOG_LINE_PATTERN.match(line)
                if match:
                    flush()
                    current = {
                        "timestamp": match.group("timestamp"),
                        "level": match.group("level"),
                        "logger_name": match.group("logger").strip(),
                        "raw_lines": [line],
                        "message_lines": [match.group("message")],
                    }
                    continue

                if current is None:
                    current = {
                        "timestamp": "",
                        "level": "",
                        "logger_name": "",
                        "raw_lines": [line],
                        "message_lines": [line],
                    }
                else:
                    current["raw_lines"].append(line)
                    current["message_lines"].append(line)

        flush()
        return entries

    def _matches_query(self, entry: LogEntry, query: LogQuery) -> bool:
        module = query.module.strip().lower()
        if module and module not in entry.module.lower() and module not in entry.logger_name.lower():
            return False

        level = query.level.strip().upper()
        if level and entry.level.upper() != level:
            return False

        task_id = query.task_id.strip().lower()
        if task_id and task_id not in entry.task_id.lower() and task_id not in entry.raw.lower():
            return False

        keyword = query.keyword.strip().lower()
        if keyword:
            haystack = "\n".join(
                [
                    entry.raw,
                    entry.logger_name,
                    entry.module,
                    entry.source_file.name,
                    entry.error_code,
                    entry.task_id,
                ]
            ).lower()
            if keyword not in haystack:
                return False
        return True

    def _module_from_logger(self, logger_name: str) -> str:
        prefix = f"{self.LOGGER_NAME}."
        if logger_name == self.LOGGER_NAME:
            return "app"
        if logger_name.startswith(prefix):
            return logger_name[len(prefix) :]
        return logger_name

    @staticmethod
    def _extract_task_id(raw: str) -> str:
        for pattern in TASK_ID_PATTERNS:
            match = pattern.search(raw)
            if match:
                return match.group(1).strip(" ,;，。")
        return ""

    @staticmethod
    def _extract_error_code(raw: str) -> str:
        match = ERROR_CODE_PATTERN.search(raw)
        return match.group(0) if match else ""

    @staticmethod
    def _safe_token(value: str) -> str:
        token = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value).strip()).strip("._-")
        return token or "unknown"
