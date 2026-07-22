"""Group management service for stage-5 forwarding targets."""

from __future__ import annotations

import logging

from database.models import Chat
from database.repositories import ChatRepository, GroupRepository
from services.telegram_service import TelegramService, TelegramServiceError
from utils.error_codes import GP001


class GroupServiceError(RuntimeError):
    """Raised when a Telegram group cannot be created or registered."""

    error_code = GP001


class GroupService:
    """Create and register tool-managed Telegram groups."""

    def __init__(
        self,
        telegram_service: TelegramService,
        chat_repository: ChatRepository,
        group_repository: GroupRepository,
        logger: logging.Logger,
    ):
        self._telegram_service = telegram_service
        self._chat_repository = chat_repository
        self._group_repository = group_repository
        self._logger = logger

    def create_target_group(self, api_id: str, api_hash: str, title: str, category: str = "") -> Chat:
        """Create a Telegram supergroup and persist it as a selectable target."""
        clean_title = str(title).strip()
        if not clean_title:
            raise GroupServiceError("请输入目标群名称")

        self._logger.info("Creating target group '%s'", clean_title)
        try:
            chat = self._telegram_service.create_group(api_id, api_hash, clean_title)
            chat = self._chat_repository.upsert_chat(chat)
            self._group_repository.upsert_group(chat, category=category, name_rule="")
        except TelegramServiceError:
            raise
        except GroupServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Target group creation failed: %s", exc)
            raise GroupServiceError("目标群创建失败，请查看 error.log") from exc

        self._logger.info("Target group registered: title='%s' chat_id=%s", chat.title, chat.tg_chat_id)
        return chat
