"""Telegraph page parsing and network helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from database.models import TelegraphImage, TelegraphLink, TelegraphPage
from parsers.telegram_link_parser import TelegramLinkParser


class TelegraphServiceError(RuntimeError):
    """Raised when a Telegraph page cannot be parsed or fetched."""


@dataclass(frozen=True)
class ParsedTelegraphPage:
    """Parsed Telegraph page metadata and extracted links."""

    page: TelegraphPage
    images: list[TelegraphImage]
    telegram_links: list[TelegraphLink]


class TelegraphService:
    """Fetch and parse telegra.ph pages without depending on Telegram media APIs."""

    TELEGRAPH_HOSTS = {"telegra.ph", "www.telegra.ph"}
    TELEGRAM_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}

    def __init__(
        self,
        logger: logging.Logger,
        fetcher: Optional[Callable[[str, int], str]] = None,
        timeout_seconds: int = 20,
    ):
        self._logger = logger
        self._fetcher = fetcher or self._fetch_url
        self._timeout_seconds = max(1, int(timeout_seconds))

    def fetch_page(self, url: str) -> ParsedTelegraphPage:
        """Request a Telegraph page and return parsed page metadata."""
        normalized_url = self.normalize_url(url)
        if not self.is_telegraph_url(normalized_url):
            raise TelegraphServiceError(f"Not a Telegraph URL: {url}")

        self._logger.info("Fetching Telegraph page: %s", normalized_url)
        html = self._fetcher(normalized_url, self._timeout_seconds)
        return self.parse_html(normalized_url, html)

    def parse_html(self, url: str, html: str) -> ParsedTelegraphPage:
        """Parse already-fetched Telegraph HTML."""
        normalized_url = self.normalize_url(url)
        if not self.is_telegraph_url(normalized_url):
            raise TelegraphServiceError(f"Not a Telegraph URL: {url}")

        parser = _TelegraphHtmlParser()
        parser.feed(str(html or ""))
        parser.close()

        title = self._best_title(normalized_url, parser)
        published_at = parser.time_datetime or _compact_text(" ".join(parser.time_parts))
        author_name = parser.author_name or parser.meta_author
        author_url = urljoin(normalized_url, parser.author_url) if parser.author_url else ""

        image_urls = self._dedup_urls(
            urljoin(normalized_url, src)
            for src, in_article in parser.images
            if src and (in_article or not parser.has_article_images)
        )
        telegram_links = self._telegram_links(normalized_url, parser)

        page = TelegraphPage(
            id=None,
            search_result_id=None,
            message_db_id=None,
            url=normalized_url,
            normalized_url=normalized_url,
            title=title,
            published_at=published_at,
            author_name=author_name,
            author_url=author_url,
            image_count=len(image_urls),
            telegram_link_count=len(telegram_links),
        )
        images = [
            TelegraphImage(
                id=None,
                page_id=None,
                position=index + 1,
                url=image_url,
                normalized_url=self.normalize_url(image_url),
                download_status="pending",
            )
            for index, image_url in enumerate(image_urls)
        ]
        links = [
            TelegraphLink(
                id=None,
                page_id=None,
                position=index + 1,
                url=link.url,
                normalized_url=link.normalized_url,
                link_type=link.result_type,
                text=link.text,
            )
            for index, link in enumerate(telegram_links)
        ]
        return ParsedTelegraphPage(page=page, images=images, telegram_links=links)

    @classmethod
    def extract_telegraph_urls(cls, text: str) -> list[str]:
        """Return Telegraph links found in text-like content."""
        urls = []
        for url in TelegramLinkParser.extract_urls(text):
            normalized_url = cls.normalize_url(url)
            if cls.is_telegraph_url(normalized_url):
                urls.append(normalized_url)
        return cls._dedup_urls(urls)

    @classmethod
    def is_telegraph_url(cls, url: str) -> bool:
        """Return whether a URL targets a telegra.ph page."""
        parsed = urlparse(cls.normalize_url(url))
        return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in cls.TELEGRAPH_HOSTS

    @staticmethod
    def normalize_url(url: str) -> str:
        """Normalize URLs consistently with the public-search URL parser."""
        return TelegramLinkParser.normalize_url(url)

    @staticmethod
    def _fetch_url(url: str, timeout_seconds: int) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": "TGArchiveManager/1.0 (+https://telegra.ph parser)",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urlopen(request, timeout=timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")

    @classmethod
    def _telegram_links(cls, base_url: str, parser: "_TelegraphHtmlParser") -> list["_ParsedTelegramLink"]:
        candidates: list[tuple[str, str]] = []
        scoped_links = parser.article_links if parser.article_links else parser.links
        for href, text, _in_article, in_address in scoped_links:
            if in_address:
                continue
            absolute_url = urljoin(base_url, href)
            if cls._is_telegram_url(absolute_url):
                candidates.append((absolute_url, text))

        body_text = " ".join(parser.article_text if parser.article_text else parser.all_text)
        for url in TelegramLinkParser.extract_urls(body_text):
            absolute_url = urljoin(base_url, url)
            if cls._is_telegram_url(absolute_url):
                candidates.append((absolute_url, ""))

        links: list[_ParsedTelegramLink] = []
        seen: set[str] = set()
        for raw_url, text in candidates:
            info = TelegramLinkParser.parse(raw_url)
            if info.normalized_url in seen:
                continue
            seen.add(info.normalized_url)
            links.append(
                _ParsedTelegramLink(
                    url=info.url,
                    normalized_url=info.normalized_url,
                    result_type=info.result_type,
                    text=_compact_text(text),
                )
            )
        return links

    @classmethod
    def _is_telegram_url(cls, url: str) -> bool:
        parsed = urlparse(TelegramLinkParser.normalize_url(url))
        return parsed.netloc.lower() in cls.TELEGRAM_HOSTS

    @classmethod
    def _best_title(cls, url: str, parser: "_TelegraphHtmlParser") -> str:
        for candidate in (
            " ".join(parser.h1_parts),
            parser.meta_title,
            " ".join(parser.title_parts),
        ):
            title = _compact_text(candidate)
            if title:
                return title[:240]
        slug = urlparse(url).path.strip("/").split("/")[-1]
        return slug.replace("-", " ").strip()[:240] or "Telegraph page"

    @staticmethod
    def _dedup_urls(urls) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for url in urls:
            value = str(url or "").strip()
            if not value:
                continue
            normalized = TelegramLinkParser.normalize_url(value)
            if normalized in seen:
                continue
            seen.add(normalized)
            values.append(normalized)
        return values


@dataclass(frozen=True)
class _ParsedTelegramLink:
    url: str
    normalized_url: str
    result_type: str
    text: str = ""


class _TelegraphHtmlParser(HTMLParser):
    """Small, forgiving HTML collector for Telegraph's article structure."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.address_depth = 0
        self.title_depth = 0
        self.h1_depth = 0
        self.time_depth = 0
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.time_parts: list[str] = []
        self.article_text: list[str] = []
        self.all_text: list[str] = []
        self.images: list[tuple[str, bool]] = []
        self.links: list[tuple[str, str, bool, bool]] = []
        self.meta_title = ""
        self.meta_author = ""
        self.time_datetime = ""
        self.author_name = ""
        self.author_url = ""
        self._anchor_stack: list[dict[str, object]] = []

    @property
    def has_article_images(self) -> bool:
        return any(in_article for _src, in_article in self.images)

    @property
    def article_links(self) -> list[tuple[str, str, bool, bool]]:
        return [item for item in self.links if item[2]]

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        attrs_dict = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "article":
            self.article_depth += 1
        elif tag == "address":
            self.address_depth += 1
        elif tag == "title":
            self.title_depth += 1
        elif tag == "h1":
            self.h1_depth += 1
        elif tag == "time":
            self.time_depth += 1
            if not self.time_datetime:
                self.time_datetime = attrs_dict.get("datetime", "")
        elif tag == "meta":
            self._handle_meta(attrs_dict)
        elif tag == "img":
            src = attrs_dict.get("src", "").strip()
            if src:
                self.images.append((src, self.article_depth > 0))
        elif tag == "a":
            self._anchor_stack.append(
                {
                    "href": attrs_dict.get("href", "").strip(),
                    "text": [],
                    "in_article": self.article_depth > 0,
                    "in_address": self.address_depth > 0,
                }
            )

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._anchor_stack:
            anchor = self._anchor_stack.pop()
            href = str(anchor.get("href", "") or "").strip()
            text = _compact_text(" ".join(anchor.get("text", []) or []))
            in_article = bool(anchor.get("in_article", False))
            in_address = bool(anchor.get("in_address", False))
            if href:
                self.links.append((href, text, in_article, in_address))
            if in_address and not self.author_url and href:
                self.author_url = href
                self.author_name = text
        elif tag == "article" and self.article_depth > 0:
            self.article_depth -= 1
        elif tag == "address" and self.address_depth > 0:
            self.address_depth -= 1
        elif tag == "title" and self.title_depth > 0:
            self.title_depth -= 1
        elif tag == "h1" and self.h1_depth > 0:
            self.h1_depth -= 1
        elif tag == "time" and self.time_depth > 0:
            self.time_depth -= 1

    def handle_data(self, data: str) -> None:
        text = _compact_text(data)
        if not text:
            return
        self.all_text.append(text)
        if self.article_depth > 0:
            self.article_text.append(text)
        if self.title_depth > 0:
            self.title_parts.append(text)
        if self.h1_depth > 0:
            self.h1_parts.append(text)
        if self.time_depth > 0:
            self.time_parts.append(text)
        if self._anchor_stack:
            self._anchor_stack[-1]["text"].append(text)

    def _handle_meta(self, attrs: dict[str, str]) -> None:
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "").strip()
        if not content:
            return
        if name in {"og:title", "twitter:title", "title"} and not self.meta_title:
            self.meta_title = content
        elif name in {"author", "article:author"} and not self.meta_author:
            self.meta_author = content
        elif name in {"article:published_time", "pubdate"} and not self.time_datetime:
            self.time_datetime = content


def _compact_text(value: str) -> str:
    return " ".join(str(value or "").split())
