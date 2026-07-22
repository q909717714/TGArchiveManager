"""Telegram link parsing and URL normalization helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


URL_PATTERN = re.compile(
    r"https?://[^\s<>()\"']+|t\.me/[^\s<>()\"']+|(?:www\.)?telegra\.ph/[^\s<>()\"']+",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TelegramLinkInfo:
    """Structured metadata extracted from a Telegram URL."""

    url: str
    normalized_url: str
    result_type: str
    tg_username: str = ""
    tg_message_id: Optional[int] = None


class TelegramLinkParser:
    """Parse public Telegram links without resolving or joining them."""

    @staticmethod
    def extract_urls(text: str) -> list[str]:
        """Return URL-like tokens found in text."""
        if not text:
            return []
        return [match.group(0).rstrip(".,;，。；)") for match in URL_PATTERN.finditer(text)]

    @classmethod
    def parse(cls, url: str) -> TelegramLinkInfo:
        """Parse a URL into normalized Telegram metadata."""
        normalized_url = cls.normalize_url(url)
        parsed = urlparse(normalized_url)
        host = parsed.netloc.lower()
        path_parts = [part for part in parsed.path.split("/") if part]

        if host in {"telegra.ph", "www.telegra.ph"}:
            return TelegramLinkInfo(url=url, normalized_url=normalized_url, result_type="telegraph_page")

        if host not in {"t.me", "telegram.me", "www.t.me"}:
            return TelegramLinkInfo(url=url, normalized_url=normalized_url, result_type="link")

        if not path_parts:
            return TelegramLinkInfo(url=url, normalized_url=normalized_url, result_type="link")

        first = path_parts[0]
        if first in {"joinchat", "+"} or first.startswith("+"):
            return TelegramLinkInfo(url=url, normalized_url=normalized_url, result_type="invite")

        username = first
        message_id = cls._message_id_from_path(path_parts)
        if message_id is not None:
            return TelegramLinkInfo(
                url=url,
                normalized_url=normalized_url,
                result_type="message",
                tg_username=username,
                tg_message_id=message_id,
            )

        if username.lower().endswith("bot"):
            result_type = "bot"
        else:
            result_type = "channel"

        return TelegramLinkInfo(
            url=url,
            normalized_url=normalized_url,
            result_type=result_type,
            tg_username=username,
        )

    @staticmethod
    def normalize_url(url: str) -> str:
        """Normalize URL text for deduplication."""
        value = str(url).strip()
        if not value:
            return ""
        lower_value = value.lower()
        if lower_value.startswith("t.me/") or lower_value.startswith("telegra.ph/") or lower_value.startswith("www.telegra.ph/"):
            value = f"https://{value}"

        parsed = urlparse(value)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        path = re.sub(r"/+", "/", parsed.path).rstrip("/")
        query_pairs = [
            (key, val)
            for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
        ]
        query = urlencode(query_pairs, doseq=True)
        return urlunparse((scheme, netloc, path, "", query, ""))

    @staticmethod
    def _message_id_from_path(path_parts: list[str]) -> Optional[int]:
        for part in reversed(path_parts[1:]):
            if part.isdigit():
                return int(part)
        return None
