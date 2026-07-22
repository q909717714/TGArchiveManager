"""Jisou-like Telegram bot public search provider."""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Optional

from database.models import SearchResult
from parsers.bot_result_parser import BotResultParser
from parsers.result_normalizer import ResultNormalizer
from providers.base_provider import BaseSearchProvider, SearchProviderVerificationRequired
from services.cancellation import CancellationToken, check_cancelled, sleep_with_cancel
from services.telegram_service import TelegramService


class JisouProvider(BaseSearchProvider):
    """Search a configured Jisou-like Telegram bot through TelegramService."""

    def __init__(
        self,
        telegram_service: TelegramService,
        bot_username: str,
        parser: BotResultParser,
        normalizer: ResultNormalizer,
        logger: logging.Logger,
        response_limit: int = 10,
        timeout_seconds: int = 30,
        rate_limit_seconds: float = 0,
        engine_name: str = "jisou",
    ):
        self._telegram_service = telegram_service
        self._bot_username = bot_username
        self._parser = parser
        self._normalizer = normalizer
        self._logger = logger
        self._response_limit = response_limit
        self._timeout_seconds = timeout_seconds
        self._rate_limit_seconds = max(0.0, float(rate_limit_seconds or 0))
        self._engine_name = str(engine_name or "jisou").strip() or "jisou"

    @property
    def engine_name(self) -> str:
        return self._engine_name

    def search(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[SearchResult]:
        """Send keyword to the configured bot and parse returned links."""
        check_cancelled(cancel_token)
        self._logger.info("Jisou provider search started for keyword '%s'", keyword)
        responses = self._query_search_bot(
            api_id=api_id,
            api_hash=api_hash,
            bot_username=self._bot_username,
            keyword=keyword,
            response_limit=self._response_limit,
            timeout_seconds=self._timeout_seconds,
            cancel_token=cancel_token,
        )
        responses = self._collect_paginated_responses(
            api_id=api_id,
            api_hash=api_hash,
            keyword=keyword,
            max_results=max_results,
            responses=responses,
            cancel_token=cancel_token,
        )
        check_cancelled(cancel_token)
        return self._results_from_responses(keyword, max_results, responses, verification_message="搜索 Bot 要求人机验证，请在下方选择验证结果并提交。")

    def submit_verification(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        message_id: int,
        button_text: str,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[SearchResult]:
        """Submit a manual verification answer and parse the bot's follow-up results."""
        check_cancelled(cancel_token)
        self._logger.info(
            "Jisou provider submitting verification for keyword '%s' message_id=%s",
            keyword,
            message_id,
        )
        responses = self._click_bot_button_and_collect_responses(
            api_id=api_id,
            api_hash=api_hash,
            bot_username=self._bot_username,
            message_id=int(message_id),
            button_text=button_text,
            response_limit=self._response_limit,
            timeout_seconds=self._timeout_seconds,
            cancel_token=cancel_token,
        )
        responses = self._collect_paginated_responses(
            api_id=api_id,
            api_hash=api_hash,
            keyword=keyword,
            max_results=max_results,
            responses=responses,
            cancel_token=cancel_token,
        )
        check_cancelled(cancel_token)
        return self._results_from_responses(keyword, max_results, responses, verification_message="Bot 仍要求人机验证，请重新选择验证结果并提交。")

    def _collect_paginated_responses(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        responses: list[object],
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[object]:
        """Follow result pagination buttons until enough results have been collected."""
        check_cancelled(cancel_token)
        all_responses = list(responses)
        seen_page_signatures = {self._response_signature(response) for response in all_responses}
        previous_count = self._result_count(keyword, max_results, all_responses)
        if self._human_verification_details(all_responses):
            return all_responses

        for page_index in range(self._max_page_clicks(max_results)):
            check_cancelled(cancel_token)
            if previous_count >= max_results:
                break

            next_page = self._next_page_action(all_responses)
            if next_page is None:
                self._logger.info("Jisou pagination stopped: no next-page button after %s results", previous_count)
                break

            message_id, button_text = next_page
            self._logger.info(
                "Jisou pagination clicking page button '%s' on message_id=%s, current_results=%s",
                button_text,
                message_id,
                previous_count,
            )
            if self._rate_limit_seconds > 0:
                sleep_with_cancel(cancel_token, self._rate_limit_seconds)
            check_cancelled(cancel_token)
            page_responses = self._click_bot_button_and_collect_responses(
                api_id=api_id,
                api_hash=api_hash,
                bot_username=self._bot_username,
                message_id=message_id,
                button_text=button_text,
                response_limit=self._response_limit,
                timeout_seconds=self._timeout_seconds,
                cancel_token=cancel_token,
            )
            if not page_responses:
                self._logger.info("Jisou pagination stopped: no response after next-page click")
                break

            new_signature_count = 0
            for response in page_responses:
                signature = self._response_signature(response)
                if signature in seen_page_signatures:
                    continue
                seen_page_signatures.add(signature)
                all_responses.append(response)
                new_signature_count += 1

            if self._human_verification_details(page_responses):
                break

            current_count = self._result_count(keyword, max_results, all_responses)
            if current_count <= previous_count or new_signature_count <= 0:
                self._logger.info(
                    "Jisou pagination stopped: page click produced no new results, page_index=%s results=%s",
                    page_index + 1,
                    current_count,
                )
                break
            previous_count = current_count

        return all_responses

    def _query_search_bot(
        self,
        api_id: str,
        api_hash: str,
        bot_username: str,
        keyword: str,
        response_limit: int,
        timeout_seconds: int,
        cancel_token: Optional[CancellationToken],
    ) -> list[object]:
        check_cancelled(cancel_token)
        method = self._telegram_service.query_search_bot
        if self._callable_accepts_cancel_token(method):
            return method(
                api_id=api_id,
                api_hash=api_hash,
                bot_username=bot_username,
                keyword=keyword,
                response_limit=response_limit,
                timeout_seconds=timeout_seconds,
                cancel_token=cancel_token,
            )
        return method(
            api_id=api_id,
            api_hash=api_hash,
            bot_username=bot_username,
            keyword=keyword,
            response_limit=response_limit,
            timeout_seconds=timeout_seconds,
        )

    def _click_bot_button_and_collect_responses(
        self,
        api_id: str,
        api_hash: str,
        bot_username: str,
        message_id: int,
        button_text: str,
        response_limit: int,
        timeout_seconds: int,
        cancel_token: Optional[CancellationToken],
    ) -> list[object]:
        check_cancelled(cancel_token)
        method = self._telegram_service.click_bot_button_and_collect_responses
        if self._callable_accepts_cancel_token(method):
            return method(
                api_id=api_id,
                api_hash=api_hash,
                bot_username=bot_username,
                message_id=message_id,
                button_text=button_text,
                response_limit=response_limit,
                timeout_seconds=timeout_seconds,
                cancel_token=cancel_token,
            )
        return method(
            api_id=api_id,
            api_hash=api_hash,
            bot_username=bot_username,
            message_id=message_id,
            button_text=button_text,
            response_limit=response_limit,
            timeout_seconds=timeout_seconds,
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

    def _result_count(self, keyword: str, max_results: int, responses: list[object]) -> int:
        parsed_results = self._parser.parse_messages(responses)
        return len(self._normalizer.normalize(keyword, self.engine_name, parsed_results, max_results))

    def _results_from_responses(
        self,
        keyword: str,
        max_results: int,
        responses: list[object],
        verification_message: str,
    ) -> list[SearchResult]:
        """Validate bot responses and return normalized search results."""
        self._log_response_summary(responses)
        verification = self._human_verification_details(responses)
        if verification:
            self._logger.warning("Jisou provider requires human verification before search can continue")
            raise SearchProviderVerificationRequired(
                message=verification_message,
                bot_username=self._bot_username,
                message_id=verification["message_id"],
                prompt=verification["prompt"],
                options=verification["options"],
                media_path=verification["media_path"],
            )

        parsed_results = self._parser.parse_messages(responses)
        results = self._normalizer.normalize(keyword, self.engine_name, parsed_results, max_results)
        if not results:
            self._logger.warning("Jisou provider did not find parseable links in bot responses")
        self._logger.info("Jisou provider search parsed %s results", len(results))
        return results

    @classmethod
    def _next_page_action(cls, responses: list[object]) -> tuple[int, str] | None:
        for response in reversed(responses):
            message_id = int(getattr(response, "message_id", 0) or 0)
            if message_id <= 0:
                continue
            for button_text in list(getattr(response, "button_texts", []) or []):
                clean_text = str(button_text).strip()
                if cls._is_next_page_button(clean_text):
                    return message_id, clean_text
        return None

    @staticmethod
    def _is_next_page_button(button_text: str) -> bool:
        text = str(button_text).strip()
        if not text:
            return False

        compact = "".join(text.lower().split())
        previous_indicators = ("上一页", "上页", "上一頁", "首頁", "首页", "prev", "previous", "back")
        if any(indicator in compact for indicator in previous_indicators):
            return False

        next_indicators = (
            "下一页",
            "下页",
            "下一頁",
            "下頁",
            "下一個",
            "下一个",
            "更多",
            "查看更多",
            "加载更多",
            "載入更多",
            "next",
            "more",
        )
        if any(indicator in compact for indicator in next_indicators):
            return True

        return compact in {">", ">>", "›", "»", "▶", "▶️", "➡", "➡️", "⏭", "⏭️"}

    @staticmethod
    def _response_signature(response: object) -> str:
        text = str(getattr(response, "text", "") or "")
        buttons = "|".join(str(item).strip() for item in list(getattr(response, "button_texts", []) or []))
        urls = "|".join(str(item).strip() for item in list(getattr(response, "button_urls", []) or []))
        links = "|".join(str(getattr(item, "url", "") or "").strip() for item in list(getattr(response, "text_links", []) or []))
        return "\n".join([text, buttons, urls, links])

    @staticmethod
    def _max_page_clicks(max_results: int) -> int:
        return max(1, min(50, max(1, int(max_results))))

    def _log_response_summary(self, responses: list[object]) -> None:
        for response in responses:
            text = str(getattr(response, "text", "") or "")
            button_urls = list(getattr(response, "button_urls", []) or [])
            button_texts = list(getattr(response, "button_texts", []) or [])
            text_links = list(getattr(response, "text_links", []) or [])
            media_path = str(getattr(response, "media_path", "") or "")
            preview = " ".join(text.split())[:300]
            self._logger.info(
                "Jisou response message_id=%s text_len=%s button_url_count=%s button_text_count=%s text_link_count=%s media_path='%s' preview='%s'",
                getattr(response, "message_id", ""),
                len(text),
                len(button_urls),
                len(button_texts),
                len(text_links),
                media_path,
                preview,
            )
            if button_texts:
                self._logger.info(
                    "Jisou response message_id=%s button_texts='%s'",
                    getattr(response, "message_id", ""),
                    " | ".join(button_texts[:12]),
                )

    @staticmethod
    def _human_verification_details(responses: list[object]) -> dict[str, object]:
        indicators = ("人机验证", "请选择计算结果", "验证码", "human verification", "captcha")
        for response in responses:
            raw_text = str(getattr(response, "text", "") or "")
            text = raw_text.lower()
            if not any(indicator.lower() in text for indicator in indicators):
                continue

            prompt = " ".join(raw_text.split())[:1000]
            button_texts = [str(item).strip() for item in list(getattr(response, "button_texts", []) or []) if str(item).strip()]
            return {
                "message_id": int(getattr(response, "message_id", 0) or 0),
                "prompt": prompt,
                "options": button_texts[:12],
                "media_path": str(getattr(response, "media_path", "") or ""),
            }
        return {}
