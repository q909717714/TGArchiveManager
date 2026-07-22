"""Centralized service construction for UI pages and workers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from database.db import DatabaseManager
from database.repositories import (
    AccountRepository,
    ChatRepository,
    DownloadRecordRepository,
    FileRepository,
    ForwardRepository,
    GroupRepository,
    MessageRepository,
    PublicSearchRepository,
    TaskRepository,
    TelegraphRepository,
)
from parsers.bot_result_parser import BotResultParser
from parsers.result_normalizer import ResultNormalizer
from providers.base_provider import BaseSearchProvider
from providers.jisou_provider import JisouProvider
from providers.telegram_native_provider import TelegramNativeSearchProvider
from services.backup_service import BackupService
from services.config_service import ConfigService
from services.download_service import DownloadService
from services.forward_service import ForwardService
from services.group_service import GroupService
from services.log_service import LogService
from services.public_search_service import PublicSearchService
from services.telegraph_service import TelegraphService
from services.telegram_service import TelegramService


@dataclass(frozen=True)
class ApplicationContext:
    """Read-only application dependencies shared by UI page service builders."""

    project_root: Path
    config_service: ConfigService
    log_service: LogService
    database: DatabaseManager


class ServiceFactory:
    """Build service-layer objects with consistent repositories, loggers, and config."""

    def __init__(self, context: ApplicationContext):
        self._context = context
        self._project_root = Path(context.project_root)
        self._config_service = context.config_service
        self._log_service = context.log_service
        self._database = context.database

    def telegram_service(self, chat_repository: Optional[ChatRepository] = None) -> TelegramService:
        """Build a TelegramService for the current project root and config."""
        return TelegramService(
            project_root=self._project_root,
            config=self._config_service.as_dict(),
            account_repository=AccountRepository(self._database),
            logger=self._log_service.get_logger("telegram"),
            chat_repository=chat_repository,
        )

    def telegraph_service(self, logger: Optional[logging.Logger] = None) -> TelegraphService:
        """Build a TelegraphService using the supplied task logger when available."""
        return TelegraphService(logger or self._log_service.get_logger("telegraph"))

    def download_service(
        self,
        telegram_service: Optional[TelegramService] = None,
        logger: Optional[logging.Logger] = None,
        message_repository: Optional[MessageRepository] = None,
        file_repository: Optional[FileRepository] = None,
        download_record_repository: Optional[DownloadRecordRepository] = None,
        telegraph_repository: Optional[TelegraphRepository] = None,
    ) -> DownloadService:
        """Build DownloadService with configured download root and policy options."""
        download_logger = logger or self._log_service.get_file_logger("download", "download.log")
        return DownloadService(
            telegram_service=telegram_service or self.telegram_service(),
            message_repository=message_repository or MessageRepository(self._database),
            file_repository=file_repository or FileRepository(self._database),
            download_record_repository=download_record_repository or DownloadRecordRepository(self._database),
            download_root=self._config_service.resolve_path("download.root_dir", "downloads"),
            logger=download_logger,
            telegraph_repository=telegraph_repository or TelegraphRepository(self._database),
            telegraph_service=self.telegraph_service(download_logger),
            download_options=self._config_service.get("download", {}) or {},
        )

    def group_service(
        self,
        telegram_service: Optional[TelegramService] = None,
        chat_repository: Optional[ChatRepository] = None,
        group_repository: Optional[GroupRepository] = None,
        logger: Optional[logging.Logger] = None,
    ) -> GroupService:
        """Build GroupService for target group creation and persistence."""
        return GroupService(
            telegram_service=telegram_service or self.telegram_service(chat_repository=chat_repository),
            chat_repository=chat_repository or ChatRepository(self._database),
            group_repository=group_repository or GroupRepository(self._database),
            logger=logger or self._log_service.get_logger("group_service"),
        )

    def forward_service(
        self,
        search_repository: Optional[PublicSearchRepository],
        telegram_service: Optional[TelegramService] = None,
        forward_repository: Optional[ForwardRepository] = None,
        task_repository: Optional[TaskRepository] = None,
        group_service: Optional[GroupService] = None,
        message_repository: Optional[MessageRepository] = None,
        logger: Optional[logging.Logger] = None,
        log_file: str = "",
        max_per_task: Optional[int] = None,
    ) -> ForwardService:
        """Build ForwardService for search-result cards or backed-up message records."""
        forward_logger = logger or self._log_service.get_file_logger("forward", "forward.log")
        resolved_telegram_service = telegram_service or self.telegram_service()
        return ForwardService(
            search_repository=search_repository,
            forward_repository=forward_repository or ForwardRepository(self._database),
            task_repository=task_repository or TaskRepository(self._database),
            telegram_service=resolved_telegram_service,
            logger=forward_logger,
            log_file=log_file or str(self._log_service.logs_dir / "forward.log"),
            group_service=group_service,
            message_repository=message_repository,
            task_logger_factory=self._log_service.get_task_logger,
            task_log_path_factory=self._log_service.task_log_path,
            max_per_task=self._forward_max_per_task(max_per_task),
        )

    def backup_service(
        self,
        telegram_service: Optional[TelegramService] = None,
        chat_repository: Optional[ChatRepository] = None,
        message_repository: Optional[MessageRepository] = None,
        file_repository: Optional[FileRepository] = None,
        task_repository: Optional[TaskRepository] = None,
        download_service: Optional[DownloadService] = None,
        logger: Optional[logging.Logger] = None,
        log_file: str = "",
    ) -> BackupService:
        """Build BackupService with a DownloadService sharing the same TelegramService."""
        resolved_telegram_service = telegram_service or self.telegram_service(chat_repository=chat_repository)
        backup_logger = logger or self._log_service.get_file_logger("download", "download.log")
        return BackupService(
            telegram_service=resolved_telegram_service,
            chat_repository=chat_repository or ChatRepository(self._database),
            message_repository=message_repository or MessageRepository(self._database),
            file_repository=file_repository or FileRepository(self._database),
            task_repository=task_repository or TaskRepository(self._database),
            download_service=download_service
            or self.download_service(telegram_service=resolved_telegram_service, logger=backup_logger),
            logger=backup_logger,
            log_file=log_file or str(self._log_service.logs_dir / "download.log"),
            task_logger_factory=self._log_service.get_task_logger,
            task_log_path_factory=self._log_service.task_log_path,
        )

    def public_search_service(
        self,
        providers: dict[str, BaseSearchProvider],
        repository: Optional[PublicSearchRepository] = None,
        logger: Optional[logging.Logger] = None,
        log_file: str = "",
    ) -> PublicSearchService:
        """Build PublicSearchService with configured duplicate-check behavior."""
        search_logger = logger or self._log_service.get_file_logger("public_search", "public_search.log")
        return PublicSearchService(
            repository=repository or PublicSearchRepository(self._database),
            providers=providers,
            logger=search_logger,
            log_file=log_file or str(self._log_service.logs_dir / "public_search.log"),
            task_logger_factory=self._log_service.get_task_logger,
            task_log_path_factory=self._log_service.task_log_path,
            telegraph_service=self.telegraph_service(search_logger),
            telegraph_repository=TelegraphRepository(self._database),
            duplicate_check=bool(self._config_service.get("public_search.duplicate_check", True)),
        )

    def bot_public_search_service(
        self,
        engine_config: dict[str, str],
        repository: Optional[PublicSearchRepository] = None,
        logger: Optional[logging.Logger] = None,
    ) -> PublicSearchService:
        """Build the Bot public-search service for one configured or custom bot."""
        search_logger = logger or self._log_service.get_file_logger("public_search", "public_search.log")
        engine_name = str(engine_config.get("engine_name", "custom_bot") or "custom_bot")
        provider = JisouProvider(
            telegram_service=self.telegram_service(),
            bot_username=self._normalize_bot_username(engine_config.get("username", "")),
            parser=BotResultParser(),
            normalizer=ResultNormalizer(),
            logger=search_logger,
            engine_name=engine_name,
            rate_limit_seconds=float(engine_config.get("rate_limit_seconds", 0) or 0),
        )
        return self.public_search_service(
            providers={engine_name: provider},
            repository=repository,
            logger=search_logger,
        )

    def telegram_native_public_search_service(
        self,
        target_chat_ids: Sequence[int],
        repository: Optional[PublicSearchRepository] = None,
        logger: Optional[logging.Logger] = None,
    ) -> PublicSearchService:
        """Build the TG-native search service scoped to selected joined chats."""
        search_logger = logger or self._log_service.get_file_logger("public_search", "public_search.log")
        provider = TelegramNativeSearchProvider(
            telegram_service=self.telegram_service(),
            logger=search_logger,
            target_chat_ids=target_chat_ids,
        )
        return self.public_search_service(
            providers={"telegram_native": provider},
            repository=repository,
            logger=search_logger,
        )

    def _forward_max_per_task(self, override: Optional[int]) -> int:
        if override is not None:
            return int(override or 0)
        return int(self._config_service.get("forward.max_per_task", 100) or 0)

    @staticmethod
    def _normalize_bot_username(value: object) -> str:
        username = str(value or "").strip()
        if not username:
            return ""
        return username if username.startswith("@") else f"@{username}"
