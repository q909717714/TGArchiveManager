"""Parse Telegram bot search responses into raw result items."""

from __future__ import annotations

import re
from dataclasses import dataclass

from parsers.telegram_link_parser import TelegramLinkParser


@dataclass(frozen=True)
class ParsedBotResult:
    """A raw search result parsed from bot text or buttons."""

    title: str
    summary: str
    url: str


class BotResultParser:
    """Extract result candidates from Telegram search bot responses."""

    RANK_MARKER_PATTERN = re.compile(r"(?=(?:🥇|🥈|🥉|🎖|🏅|🔹|▪️|▫️|\d+[.、]))")
    LEADING_MARKER_PATTERN = re.compile(r"^(?:🥇|🥈|🥉|🎖|🏅|🔹|▪️|▫️|\d+[.、])\s*")

    def parse_messages(self, responses: list[object]) -> list[ParsedBotResult]:
        """Parse response objects exposing `text` and `button_urls` fields."""
        results: list[ParsedBotResult] = []
        for response in responses:
            text = str(getattr(response, "text", "") or "")
            text_links = list(getattr(response, "text_links", []) or [])
            button_urls = list(getattr(response, "button_urls", []) or [])
            if text_links:
                results.extend(self._parse_entity_link_results(text, text_links))

            entity_urls = {self._link_url(link) for link in text_links}
            urls = [
                url
                for url in TelegramLinkParser.extract_urls(text) + button_urls
                if url and url not in entity_urls
            ]
            for url in urls:
                title = self._title_near_url(text, url)
                summary = self._summary(text)
                results.append(ParsedBotResult(title=title, summary=summary, url=url))
            if not text_links and not urls:
                results.extend(self._parse_text_only_results(text))
        return results

    @classmethod
    def _parse_text_only_results(cls, text: str) -> list[ParsedBotResult]:
        """Parse ranked text-only bot results when no URL is present."""
        return [
            ParsedBotResult(title=chunk[:120], summary=chunk[:500], url="")
            for chunk in cls._ranked_chunks(text)
        ]

    @classmethod
    def _parse_entity_link_results(cls, text: str, text_links: list[object]) -> list[ParsedBotResult]:
        ranked_chunks = cls._ranked_chunks(text)
        results: list[ParsedBotResult] = []
        for index, link in enumerate(text_links):
            url = cls._link_url(link)
            if not url:
                continue
            display_text = cls._link_text(link)
            ranked_chunk = ranked_chunks[index] if index < len(ranked_chunks) else ""
            title = cls._best_entity_title(display_text, ranked_chunk, url)
            summary = ranked_chunk or display_text or title
            results.append(ParsedBotResult(title=title[:120], summary=summary[:500], url=url))
        return results

    @classmethod
    def _ranked_chunks(cls, text: str) -> list[str]:
        compact = " ".join(line.strip() for line in text.splitlines() if line.strip())
        if not compact:
            return []

        chunks = [chunk.strip() for chunk in cls.RANK_MARKER_PATTERN.split(compact) if chunk.strip()]
        ranked_chunks = [
            cls.LEADING_MARKER_PATTERN.sub("", chunk).strip()
            for chunk in chunks
            if cls.LEADING_MARKER_PATTERN.match(chunk)
        ]
        ranked_chunks = [chunk for chunk in ranked_chunks if chunk]

        if len(ranked_chunks) < 2:
            return []
        return ranked_chunks

    @staticmethod
    def _link_url(link: object) -> str:
        if isinstance(link, tuple) and len(link) >= 2:
            return str(link[1] or "").strip()
        return str(getattr(link, "url", "") or "").strip()

    @staticmethod
    def _link_text(link: object) -> str:
        if isinstance(link, tuple) and len(link) >= 1:
            return str(link[0] or "").strip()
        return str(getattr(link, "text", "") or "").strip()

    @staticmethod
    def _best_entity_title(display_text: str, ranked_chunk: str, url: str) -> str:
        generic_text = {"", "查看", "点击", "详情", "进入", "查看详情", "🔗"}
        clean_display = display_text.strip()
        if clean_display not in generic_text and len(clean_display) >= 4:
            return clean_display
        return ranked_chunk.strip() or url

    @staticmethod
    def _title_near_url(text: str, url: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return url

        for index, line in enumerate(lines):
            if url in line or url.replace("https://", "") in line:
                for candidate in reversed(lines[max(0, index - 3) : index]):
                    if not TelegramLinkParser.extract_urls(candidate):
                        return candidate[:120]
                return line.replace(url, "").strip()[:120] or url

        for line in lines:
            if not TelegramLinkParser.extract_urls(line):
                return line[:120]
        return url

    @staticmethod
    def _summary(text: str) -> str:
        compact = " ".join(part.strip() for part in text.splitlines() if part.strip())
        return compact[:500]
