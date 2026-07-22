"""Public search orchestration service."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from database.models import SearchResult
from database.repositories import PublicSearchRepository, TelegraphRepository
from providers.base_provider import BaseSearchProvider, SearchProviderVerificationRequired
from services.cancellation import CancellationToken, OperationCancelled, check_cancelled
from services.telegraph_service import ParsedTelegraphPage, TelegraphService
from utils.error_codes import SE003


class PublicSearchError(RuntimeError):
    """Raised when a public search task fails."""

    error_code = "SE000"


@dataclass(frozen=True)
class PublicSearchReport:
    """Public search task result summary."""

    task_id: int
    keyword: str
    engine_name: str
    total_found: int
    total_saved: int
    skipped_count: int
    log_file: str
    results: list[SearchResult]


class PublicSearchService:
    """Run public search providers and persist task/results."""

    def __init__(
        self,
        repository: PublicSearchRepository,
        providers: dict[str, BaseSearchProvider],
        logger: logging.Logger,
        log_file: str,
        task_logger_factory: Optional[Callable[[str, str], logging.Logger]] = None,
        task_log_path_factory: Optional[Callable[[str, str], Path]] = None,
        telegraph_service: Optional[TelegraphService] = None,
        telegraph_repository: Optional[TelegraphRepository] = None,
        duplicate_check: bool = True,
    ):
        self._repository = repository
        self._providers = providers
        self._logger = logger
        self._log_file = log_file
        self._task_logger_factory = task_logger_factory
        self._task_log_path_factory = task_log_path_factory
        self._telegraph_service = telegraph_service
        self._telegraph_repository = telegraph_repository
        self._duplicate_check = bool(duplicate_check)

    def search(
        self,
        api_id: str,
        api_hash: str,
        engine_name: str,
        keyword: str,
        max_results: int,
        cancel_token: Optional[CancellationToken] = None,
    ) -> PublicSearchReport:
        """Execute one provider search and save normalized results."""
        check_cancelled(cancel_token)
        clean_keyword = str(keyword).strip()
        if not clean_keyword:
            raise PublicSearchError("请输入搜索关键词")
        capped_max_results = max(1, min(int(max_results), 100))

        provider = self._providers.get(engine_name)
        if provider is None:
            raise PublicSearchError(f"未启用搜索 Provider：{engine_name}")

        task_id = self._repository.create_task(clean_keyword, engine_name, capped_max_results, self._log_file)
        task_logger = self._task_logger(str(task_id))
        task_log_file = self._task_log_file(str(task_id))
        self._repository.update_task_log_file(task_id, task_log_file)
        task_logger.info("Public search task %s started: engine=%s keyword='%s'", task_id, engine_name, clean_keyword)

        try:
            results = self._call_provider_search(
                provider,
                api_id,
                api_hash,
                clean_keyword,
                capped_max_results,
                cancel_token,
            )
            return self._save_task_results(
                task_id,
                clean_keyword,
                engine_name,
                results,
                task_logger,
                task_log_file,
                cancel_token=cancel_token,
            )
        except SearchProviderVerificationRequired as exc:
            self._repository.complete_task(
                task_id=task_id,
                status="verification_required",
                total_found=0,
                total_saved=0,
                success_count=0,
                failed_count=0,
                skipped_count=0,
            )
            exc.attach_task_context(task_id, clean_keyword, engine_name, capped_max_results, task_log_file)
            task_logger.info("Public search task %s is waiting for human verification", task_id)
            raise
        except OperationCancelled:
            self._repository.complete_task(
                task_id=task_id,
                status="cancelled",
                total_found=0,
                total_saved=0,
                success_count=0,
                failed_count=0,
                skipped_count=0,
            )
            task_logger.info("Public search task %s cancelled by user", task_id)
            raise
        except Exception:
            self._repository.complete_task(
                task_id=task_id,
                status="failed",
                total_found=0,
                total_saved=0,
                success_count=0,
                failed_count=1,
                skipped_count=0,
            )
            task_logger.exception("Public search task %s failed", task_id)
            raise

    def submit_verification(
        self,
        api_id: str,
        api_hash: str,
        engine_name: str,
        keyword: str,
        max_results: int,
        task_id: int,
        message_id: int,
        button_text: str,
        cancel_token: Optional[CancellationToken] = None,
    ) -> PublicSearchReport:
        """Submit human verification for a pending search task and save returned results."""
        check_cancelled(cancel_token)
        clean_keyword = str(keyword).strip()
        if not clean_keyword:
            raise PublicSearchError("缺少待验证搜索关键词，请重新搜索")
        parsed_task_id = int(task_id)
        if parsed_task_id <= 0:
            raise PublicSearchError("缺少待验证搜索任务，请重新搜索")
        capped_max_results = max(1, min(int(max_results), 100))

        provider = self._providers.get(engine_name)
        if provider is None:
            raise PublicSearchError(f"未启用搜索 Provider：{engine_name}")

        task_logger = self._task_logger(str(parsed_task_id))
        task_log_file = self._task_log_file(str(parsed_task_id))
        self._repository.update_task_log_file(parsed_task_id, task_log_file)
        task_logger.info(
            "Submitting verification for public search task %s: engine=%s keyword='%s'",
            parsed_task_id,
            engine_name,
            clean_keyword,
        )
        try:
            results = self._call_provider_verification(
                provider,
                api_id=api_id,
                api_hash=api_hash,
                keyword=clean_keyword,
                max_results=capped_max_results,
                message_id=int(message_id),
                button_text=button_text,
                cancel_token=cancel_token,
            )
            return self._save_task_results(
                parsed_task_id,
                clean_keyword,
                engine_name,
                results,
                task_logger,
                task_log_file,
                cancel_token=cancel_token,
            )
        except SearchProviderVerificationRequired as exc:
            self._repository.complete_task(
                task_id=parsed_task_id,
                status="verification_required",
                total_found=0,
                total_saved=0,
                success_count=0,
                failed_count=0,
                skipped_count=0,
            )
            exc.attach_task_context(parsed_task_id, clean_keyword, engine_name, capped_max_results, task_log_file)
            task_logger.info("Public search task %s still requires human verification", parsed_task_id)
            raise
        except OperationCancelled:
            self._repository.complete_task(
                task_id=parsed_task_id,
                status="cancelled",
                total_found=0,
                total_saved=0,
                success_count=0,
                failed_count=0,
                skipped_count=0,
            )
            task_logger.info("Public search verification task %s cancelled by user", parsed_task_id)
            raise
        except Exception:
            self._repository.complete_task(
                task_id=parsed_task_id,
                status="failed",
                total_found=0,
                total_saved=0,
                success_count=0,
                failed_count=1,
                skipped_count=0,
            )
            task_logger.exception("Public search verification task %s failed", parsed_task_id)
            raise

    def _save_task_results(
        self,
        task_id: int,
        keyword: str,
        engine_name: str,
        results: list[SearchResult],
        task_logger: logging.Logger,
        task_log_file: str,
        cancel_token: Optional[CancellationToken] = None,
    ) -> PublicSearchReport:
        """Persist provider results and finalize a public search task."""
        check_cancelled(cancel_token)
        if not results:
            error = PublicSearchError("搜索结果为空")
            error.error_code = SE003
            raise error

        results, telegraph_pages = self._enrich_telegraph_results(results, task_logger, cancel_token=cancel_token)
        check_cancelled(cancel_token)
        saved_results = self._repository.save_results(task_id, results, duplicate_check=self._duplicate_check)
        self._save_telegraph_pages(saved_results, telegraph_pages, task_logger, cancel_token=cancel_token)
        duplicate_count = sum(1 for result in saved_results if result.is_duplicate)
        self._repository.complete_task(
            task_id=task_id,
            status="completed",
            total_found=len(results),
            total_saved=len(saved_results),
            success_count=len(saved_results) - duplicate_count,
            failed_count=0,
            skipped_count=duplicate_count,
        )
        task_logger.info(
            "Public search task %s completed: found=%s saved=%s duplicates=%s",
            task_id,
            len(results),
            len(saved_results),
            duplicate_count,
        )
        return PublicSearchReport(
            task_id=task_id,
            keyword=keyword,
            engine_name=engine_name,
            total_found=len(results),
            total_saved=len(saved_results),
            skipped_count=duplicate_count,
            log_file=task_log_file,
            results=saved_results,
        )

    def _enrich_telegraph_results(
        self,
        results: list[SearchResult],
        task_logger: logging.Logger,
        cancel_token: Optional[CancellationToken] = None,
    ) -> tuple[list[SearchResult], dict[str, ParsedTelegraphPage]]:
        """Parse Telegraph pages before saving their card metadata."""
        enriched_results: list[SearchResult] = []
        parsed_pages: dict[str, ParsedTelegraphPage] = {}

        for result in results:
            check_cancelled(cancel_token)
            if not self._is_telegraph_result(result):
                enriched_results.append(result)
                continue

            normalized_url = TelegraphService.normalize_url(result.url or result.normalized_url)
            telegraph_result = replace(
                result,
                result_type="telegraph_page",
                url=result.url or normalized_url,
                normalized_url=normalized_url,
                can_forward=True if result.can_forward is None else result.can_forward,
                forward_status=result.forward_status or "pending",
            )

            if self._telegraph_service is None:
                enriched_results.append(telegraph_result)
                continue

            try:
                parsed_page = self._telegraph_service.fetch_page(normalized_url)
            except Exception as exc:
                task_logger.warning("Telegraph page parse failed: url=%s error=%s", normalized_url, exc)
                self._logger.warning("Telegraph page parse failed: url=%s error=%s", normalized_url, exc)
                enriched_results.append(replace(telegraph_result, is_accessible=False))
                continue

            page = parsed_page.page
            parsed_pages[normalized_url] = parsed_page
            enriched_results.append(
                replace(
                    telegraph_result,
                    title=page.title or telegraph_result.title,
                    summary=self._telegraph_summary(page, telegraph_result.summary),
                    is_accessible=True,
                )
            )

        return enriched_results, parsed_pages

    def _save_telegraph_pages(
        self,
        saved_results: list[SearchResult],
        parsed_pages: dict[str, ParsedTelegraphPage],
        task_logger: logging.Logger,
        cancel_token: Optional[CancellationToken] = None,
    ) -> None:
        if self._telegraph_repository is None:
            return

        for result in saved_results:
            check_cancelled(cancel_token)
            if result.id is None or result.result_type != "telegraph_page":
                continue
            parsed_page = parsed_pages.get(result.normalized_url)
            if parsed_page is None:
                continue
            try:
                self._telegraph_repository.upsert_page_for_search_result(
                    int(result.id),
                    replace(parsed_page.page, search_result_id=int(result.id)),
                    parsed_page.images,
                    parsed_page.telegram_links,
                )
            except Exception as exc:
                task_logger.warning(
                    "Telegraph page persistence failed: result_id=%s url=%s error=%s",
                    result.id,
                    result.normalized_url,
                    exc,
                )
                self._logger.warning(
                    "Telegraph page persistence failed: result_id=%s url=%s error=%s",
                    result.id,
                    result.normalized_url,
                    exc,
                )

    @staticmethod
    def _is_telegraph_result(result: SearchResult) -> bool:
        return result.result_type == "telegraph_page" or TelegraphService.is_telegraph_url(
            result.url or result.normalized_url
        )

    @staticmethod
    def _telegraph_summary(page, fallback_summary: str) -> str:
        parts = []
        if page.published_at:
            parts.append(f"发布时间：{page.published_at}")
        if page.author_name or page.author_url:
            source = page.author_name or page.author_url
            if page.author_url and page.author_name:
                source = f"{page.author_name} ({page.author_url})"
            parts.append(f"作者/来源：{source}")
        parts.append(f"图片数量：{int(page.image_count)}")
        parts.append(f"Telegram 跳转链接：{int(page.telegram_link_count)}")
        if fallback_summary:
            parts.append(str(fallback_summary))
        return "；".join(parts)[:1000]

    def _task_logger(self, task_id: str) -> logging.Logger:
        if self._task_logger_factory is None:
            return self._logger
        return self._task_logger_factory(task_id, "public_search")

    def _task_log_file(self, task_id: str) -> str:
        if self._task_log_path_factory is None:
            return self._log_file
        return str(self._task_log_path_factory(task_id, "public_search"))

    @classmethod
    def _call_provider_search(
        cls,
        provider: BaseSearchProvider,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        cancel_token: Optional[CancellationToken],
    ) -> list[SearchResult]:
        check_cancelled(cancel_token)
        if cls._callable_accepts_cancel_token(provider.search):
            return provider.search(api_id, api_hash, keyword, max_results, cancel_token=cancel_token)
        return provider.search(api_id, api_hash, keyword, max_results)

    @classmethod
    def _call_provider_verification(
        cls,
        provider: BaseSearchProvider,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        message_id: int,
        button_text: str,
        cancel_token: Optional[CancellationToken],
    ) -> list[SearchResult]:
        check_cancelled(cancel_token)
        if cls._callable_accepts_cancel_token(provider.submit_verification):
            return provider.submit_verification(
                api_id=api_id,
                api_hash=api_hash,
                keyword=keyword,
                max_results=max_results,
                message_id=message_id,
                button_text=button_text,
                cancel_token=cancel_token,
            )
        return provider.submit_verification(
            api_id=api_id,
            api_hash=api_hash,
            keyword=keyword,
            max_results=max_results,
            message_id=message_id,
            button_text=button_text,
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
