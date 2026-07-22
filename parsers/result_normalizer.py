"""Normalize parsed public search results into database models."""

from __future__ import annotations

import hashlib

from database.models import SearchResult
from parsers.bot_result_parser import ParsedBotResult
from parsers.telegram_link_parser import TelegramLinkParser


class ResultNormalizer:
    """Normalize, rank, and deduplicate parsed search results."""

    def normalize(
        self,
        keyword: str,
        engine_name: str,
        parsed_results: list[ParsedBotResult],
        max_results: int,
    ) -> list[SearchResult]:
        """Return unique SearchResult models capped by max_results."""
        unique: list[SearchResult] = []
        seen_keys: set[str] = set()

        for parsed in parsed_results:
            link_info = TelegramLinkParser.parse(parsed.url) if parsed.url else None
            normalized_url = link_info.normalized_url if link_info is not None else self._text_result_key(
                engine_name,
                keyword,
                parsed,
            )
            if not normalized_url or normalized_url in seen_keys:
                continue

            seen_keys.add(normalized_url)
            result_type = link_info.result_type if link_info is not None else "unknown"
            unique.append(
                SearchResult(
                    id=None,
                    task_id=None,
                    engine_name=engine_name,
                    rank_no=len(unique) + 1,
                    keyword=keyword,
                    result_type=result_type,
                    title=parsed.title or parsed.summary or normalized_url,
                    summary=parsed.summary,
                    url=parsed.url,
                    normalized_url=normalized_url,
                    tg_username=link_info.tg_username if link_info is not None else "",
                    tg_message_id=link_info.tg_message_id if link_info is not None else None,
                    tg_chat_id=None,
                    is_duplicate=False,
                    is_accessible=None,
                    is_protected=False,
                    can_forward=None,
                    forward_status="pending" if parsed.url else "card_only",
                )
            )
            if len(unique) >= max_results:
                break

        return unique

    @staticmethod
    def _text_result_key(engine_name: str, keyword: str, parsed: ParsedBotResult) -> str:
        raw = "\n".join([engine_name, keyword, parsed.title, parsed.summary])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"text:{engine_name}:{digest[:24]}"
