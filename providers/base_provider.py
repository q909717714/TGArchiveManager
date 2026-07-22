"""Public search provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from database.models import SearchResult
from services.cancellation import CancellationToken
from utils.error_codes import SE005


class SearchProviderError(RuntimeError):
    """Raised when a public search provider cannot return usable results."""

    error_code = "SE000"


class SearchProviderVerificationRequired(SearchProviderError):
    """Raised when a provider bot asks the user to complete manual verification."""

    error_code = SE005

    def __init__(
        self,
        message: str,
        bot_username: str,
        message_id: int,
        prompt: str,
        options: list[str],
        media_path: str = "",
    ):
        super().__init__(message)
        self.bot_username = bot_username
        self.message_id = int(message_id)
        self.prompt = prompt
        self.options = list(options)
        self.media_path = str(media_path or "")
        self.task_id = 0
        self.keyword = ""
        self.engine_name = ""
        self.max_results = 0
        self.log_file = ""

    def attach_task_context(self, task_id: int, keyword: str, engine_name: str, max_results: int, log_file: str) -> None:
        """Attach the recoverable public-search task context for the UI."""
        self.task_id = int(task_id)
        self.keyword = str(keyword)
        self.engine_name = str(engine_name)
        self.max_results = int(max_results)
        self.log_file = str(log_file)


class BaseSearchProvider(ABC):
    """Base interface for all public search providers."""

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Return a stable provider engine name."""

    @abstractmethod
    def search(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[SearchResult]:
        """Search public results and return normalized SearchResult objects."""

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
        """Submit a provider verification answer and return search results when available."""
        raise SearchProviderError("当前搜索 Provider 不支持软件内提交人机验证")
