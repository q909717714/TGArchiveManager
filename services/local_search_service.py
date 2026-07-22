"""Local SQLite search service for backed-up Telegram messages."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from database.models import MessageRecord
from database.repositories import MessageRepository


@dataclass(frozen=True)
class LocalSearchQuery:
    """Local message search filters."""

    keyword: str = ""
    tg_chat_id: Optional[int] = None
    date_from: str = ""
    date_to: str = ""
    message_type: str = ""
    media_filter: str = "all"
    limit: int = 500


class LocalSearchService:
    """Search backed-up messages stored in SQLite."""

    def __init__(self, message_repository: MessageRepository, logger: logging.Logger):
        self._message_repository = message_repository
        self._logger = logger

    def search(self, query: LocalSearchQuery) -> list[MessageRecord]:
        """Return local messages matching the given filters."""
        self._logger.info(
            "Local search started: keyword='%s' chat_id=%s type='%s' media='%s'",
            query.keyword,
            query.tg_chat_id,
            query.message_type,
            query.media_filter,
        )
        results = self._message_repository.search_messages(
            keyword=query.keyword,
            tg_chat_id=query.tg_chat_id,
            date_from=query.date_from,
            date_to=query.date_to,
            message_type=query.message_type,
            media_filter=query.media_filter,
            limit=query.limit,
        )
        self._logger.info("Local search completed: %s results", len(results))
        return results
