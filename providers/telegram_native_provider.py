"""Telegram native joined-chat search provider."""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Optional, Sequence

from database.models import SearchResult
from providers.base_provider import BaseSearchProvider
from services.cancellation import CancellationToken, check_cancelled
from services.telegraph_service import TelegraphService
from services.telegram_service import TelegramArchivedMessage, TelegramService


class TelegramNativeSearchProvider(BaseSearchProvider):
    """Search messages in joined Telegram channels and groups without using a bot."""

    @property
    def engine_name(self) -> str:
        return "telegram_native"

    def __init__(
        self,
        telegram_service: TelegramService,
        logger: logging.Logger,
        target_chat_ids: Optional[Sequence[int]] = None,
    ):
        self._telegram_service = telegram_service
        self._logger = logger
        self._target_chat_ids = [int(chat_id) for chat_id in list(target_chat_ids or [])]

    def search(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[SearchResult]:
        """Return normalized results from Telegram's native joined-chat search."""
        check_cancelled(cancel_token)
        self._logger.info("Telegram native provider search started for keyword '%s'", keyword)
        messages = self._search_joined_messages(
            api_id=api_id,
            api_hash=api_hash,
            keyword=keyword,
            max_results=max_results,
            target_chat_ids=self._target_chat_ids if self._target_chat_ids else None,
            cancel_token=cancel_token,
        )
        results: list[SearchResult] = []
        for message in messages:
            check_cancelled(cancel_token)
            for result in self._results_from_message(keyword=keyword, message=message):
                check_cancelled(cancel_token)
                results.append(result)
                if len(results) >= max(1, int(max_results)):
                    break
            if len(results) >= max(1, int(max_results)):
                break
        results = [
            self._with_rank_no(result, index + 1)
            for index, result in enumerate(results)
        ]
        self._logger.info("Telegram native provider search parsed %s results", len(results))
        return results

    def _search_joined_messages(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        target_chat_ids: Optional[Sequence[int]],
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramArchivedMessage]:
        check_cancelled(cancel_token)
        method = self._telegram_service.search_joined_messages
        if self._callable_accepts_cancel_token(method):
            return method(
                api_id=api_id,
                api_hash=api_hash,
                keyword=keyword,
                max_results=max_results,
                target_chat_ids=target_chat_ids,
                cancel_token=cancel_token,
            )
        return method(
            api_id=api_id,
            api_hash=api_hash,
            keyword=keyword,
            max_results=max_results,
            target_chat_ids=target_chat_ids,
        )

    @staticmethod
    def _callable_accepts_cancel_token(callable_object: Callable) -> bool:
        try:
            parameters = inspect.signature(callable_object).parameters
        except (TypeError, ValueError):
            return False
        if "cancel_token" in parameters:
            return True
        return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())

    def _results_from_message(self, keyword: str, message: TelegramArchivedMessage) -> list[SearchResult]:
        telegraph_urls = TelegraphService.extract_telegraph_urls(
            "\n".join(
                [
                    message.text,
                    message.text_preview,
                    getattr(message, "webpage_url", ""),
                ]
            )
        )
        if telegraph_urls:
            return [
                self._telegraph_result_from_message(keyword=keyword, message=message, url=url)
                for url in telegraph_urls
            ]
        return [self._result_from_message(keyword=keyword, rank_no=0, message=message)]

    def _result_from_message(self, keyword: str, rank_no: int, message: TelegramArchivedMessage) -> SearchResult:
        chat_title = message.chat_title or str(message.tg_chat_id)
        title_parts = [chat_title, f"#{message.message_id}"]
        if message.sender_name:
            title_parts.append(message.sender_name)
        title = " ".join(title_parts)
        summary = self._summary_from_message(message)
        normalized_url = f"telegram-native:{message.tg_chat_id}:{message.message_id}"
        return SearchResult(
            id=None,
            task_id=None,
            engine_name=self.engine_name,
            rank_no=int(rank_no),
            keyword=str(keyword),
            result_type=message.message_type or "text",
            title=title[:240],
            summary=summary[:1000],
            url=message.source_link,
            normalized_url=normalized_url,
            tg_username="",
            tg_message_id=message.message_id,
            tg_chat_id=message.tg_chat_id,
            is_duplicate=False,
            is_accessible=True,
            is_protected=False,
            can_forward=True,
            forward_status="pending",
        )

    def _telegraph_result_from_message(self, keyword: str, message: TelegramArchivedMessage, url: str) -> SearchResult:
        chat_title = message.chat_title or str(message.tg_chat_id)
        normalized_url = TelegraphService.normalize_url(url)
        title = f"{chat_title} #{message.message_id} Telegraph"
        summary = self._summary_from_message(message)
        return SearchResult(
            id=None,
            task_id=None,
            engine_name=self.engine_name,
            rank_no=0,
            keyword=str(keyword),
            result_type="telegraph_page",
            title=title[:240],
            summary=summary[:1000],
            url=normalized_url,
            normalized_url=normalized_url,
            tg_username="",
            tg_message_id=message.message_id,
            tg_chat_id=message.tg_chat_id,
            is_duplicate=False,
            is_accessible=True,
            is_protected=False,
            can_forward=True,
            forward_status="pending",
        )

    @staticmethod
    def _with_rank_no(result: SearchResult, rank_no: int) -> SearchResult:
        return SearchResult(
            id=result.id,
            task_id=result.task_id,
            engine_name=result.engine_name,
            rank_no=int(rank_no),
            keyword=result.keyword,
            result_type=result.result_type,
            title=result.title,
            summary=result.summary,
            url=result.url,
            normalized_url=result.normalized_url,
            tg_username=result.tg_username,
            tg_message_id=result.tg_message_id,
            tg_chat_id=result.tg_chat_id,
            is_duplicate=result.is_duplicate,
            is_accessible=result.is_accessible,
            is_protected=result.is_protected,
            can_forward=result.can_forward,
            forward_status=result.forward_status,
            created_at=result.created_at,
        )

    @staticmethod
    def _summary_from_message(message: TelegramArchivedMessage) -> str:
        parts = []
        if message.date:
            parts.append(f"日期：{message.date}")
        if message.sender_name:
            parts.append(f"发送者：{message.sender_name}")
        preview = message.text_preview or message.text
        if preview:
            parts.append(f"内容：{preview}")
        if message.has_media:
            media_text = message.media_type or message.message_type or "media"
            parts.append(f"媒体：{media_text}")
            if message.file_size is not None:
                parts.append(f"大小：{TelegramNativeSearchProvider._format_size(message.file_size)}")
        return "；".join(parts)

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
