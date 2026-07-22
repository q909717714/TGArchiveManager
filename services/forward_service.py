"""Forwarding service for public search results and backed-up chat records."""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from database.models import ForwardRecord, MessageRecord, SearchResult
from database.repositories import ForwardRepository, MessageRepository, PublicSearchRepository, TaskRepository
from services.cancellation import CancellationToken, OperationCancelled, check_cancelled
from services.group_service import GroupService
from services.telegram_service import TelegramOutgoingMessage, TelegramSendResult, TelegramService, TelegramServiceError
from utils.error_codes import FW001, FW002, FW003


class ForwardServiceError(RuntimeError):
    """Raised when a forwarding task cannot be completed."""

    error_code = FW003


@dataclass(frozen=True)
class ForwardProgress:
    """Progress update emitted after one selected source item is processed."""

    task_id: str
    source_id: Optional[int]
    done_count: int
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    status: str
    message: str


@dataclass(frozen=True)
class ForwardReport:
    """Final forwarding task report."""

    task_id: str
    target_chat_id: int
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    log_file: str
    target_group_titles: tuple[str, ...] = ()


class ForwardService:
    """Format selected records as text and send them to a target chat."""

    def __init__(
        self,
        search_repository: Optional[PublicSearchRepository],
        forward_repository: ForwardRepository,
        task_repository: TaskRepository,
        telegram_service: TelegramService,
        logger: logging.Logger,
        log_file: str,
        group_service: Optional[GroupService] = None,
        message_repository: Optional[MessageRepository] = None,
        task_logger_factory: Optional[Callable[[str, str], logging.Logger]] = None,
        task_log_path_factory: Optional[Callable[[str, str], Path]] = None,
        max_per_task: int = 0,
    ):
        self._search_repository = search_repository
        self._forward_repository = forward_repository
        self._task_repository = task_repository
        self._telegram_service = telegram_service
        self._logger = logger
        self._log_file = log_file
        self._group_service = group_service
        self._message_repository = message_repository
        self._task_logger_factory = task_logger_factory
        self._task_log_path_factory = task_log_path_factory
        self._max_per_task = max(0, int(max_per_task or 0))

    def preview_search_result_cards(self, result_ids: Sequence[int], max_cards: int = 10) -> str:
        """Return a plain text preview for selected search-result cards."""
        selected_ids = [int(result_id) for result_id in result_ids if int(result_id) > 0]
        if not selected_ids or self._search_repository is None:
            return ""
        results = self._search_repository.get_results_by_ids(selected_ids[: max(1, int(max_cards))])
        return "\n\n---\n\n".join(self.format_card(result) for result in results)

    def preview_message_records(self, messages: Sequence[MessageRecord], max_messages: int = 20) -> str:
        """Return a plain text preview for selected backed-up chat messages."""
        selected_messages = self._ordered_message_records(messages)
        if not selected_messages:
            return ""
        preview_messages = selected_messages[: max(1, int(max_messages))]
        return "\n\n---\n\n".join(self.format_message_record(message) for message in preview_messages)

    def forward_search_result_cards(
        self,
        api_id: str,
        api_hash: str,
        result_ids: Sequence[int],
        target_chat_id: int,
        interval_seconds: int = 3,
        skip_duplicates: bool = True,
        progress_callback: Optional[Callable[[ForwardProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> ForwardReport:
        """Forward selected public search results as plain text cards."""
        check_cancelled(cancel_token)
        selected_ids = [int(result_id) for result_id in result_ids if int(result_id) > 0]
        if not selected_ids:
            error = ForwardServiceError("请选择要转发的搜索结果")
            error.error_code = FW002
            raise error
        self._enforce_task_limit(len(selected_ids), "搜索结果")
        if self._search_repository is None:
            error = ForwardServiceError("搜索结果转发未配置结果仓库")
            error.error_code = FW003
            raise error

        try:
            clean_target_chat_id = int(target_chat_id)
        except (TypeError, ValueError) as exc:
            error = ForwardServiceError("请选择有效的目标群或聊天")
            error.error_code = FW001
            raise error from exc
        if clean_target_chat_id == 0:
            error = ForwardServiceError("请选择有效的目标群或聊天")
            error.error_code = FW001
            raise error

        results = self._search_repository.get_results_by_ids(selected_ids)
        if not results:
            error = ForwardServiceError("所选搜索结果不存在，请刷新后重试")
            error.error_code = FW002
            raise error

        task_id = self._new_task_id()
        total_count = len(results)
        task_logger = self._task_logger(task_id)
        task_log_file = self._task_log_file(task_id)
        self._task_repository.create_task(
            task_id=task_id,
            task_type="forward",
            title=f"卡片转发 {total_count} 条",
            source_config={"source": "public_search_results", "result_ids": selected_ids},
            target_config={"target_chat_id": clean_target_chat_id, "forward_mode": "card"},
            total_count=total_count,
            log_file=task_log_file,
        )
        task_logger.info(
            "Forward task %s started: total=%s target_chat_id=%s interval=%ss",
            task_id,
            total_count,
            clean_target_chat_id,
            interval_seconds,
        )

        state = {"done": 0, "success": 0, "failed": 0, "skipped": 0}
        messages: list[TelegramOutgoingMessage] = []
        result_by_id: dict[int, SearchResult] = {}

        try:
            for result in results:
                check_cancelled(cancel_token)
                if result.id is None:
                    continue
                result_by_id[int(result.id)] = result
                skip_reason = self._skip_reason(result, skip_duplicates)
                if skip_reason:
                    self._record_result(
                        task_id,
                        result,
                        clean_target_chat_id,
                        TelegramSendResult(
                            source_id=int(result.id),
                            status="skipped",
                            reason=skip_reason,
                        ),
                        state,
                        total_count,
                        progress_callback,
                        task_logger,
                    )
                    continue
                messages.append(TelegramOutgoingMessage(source_id=int(result.id), text=self.format_card(result)))

            def on_sent(send_result: TelegramSendResult, _sent_done: int, _sent_total: int) -> None:
                result = result_by_id.get(send_result.source_id)
                if result is None:
                    return
                self._record_result(
                    task_id,
                    result,
                    clean_target_chat_id,
                    send_result,
                    state,
                    total_count,
                    progress_callback,
                    task_logger,
                )

            if messages:
                self._send_text_messages(
                    api_id=api_id,
                    api_hash=api_hash,
                    target_chat_id=clean_target_chat_id,
                    messages=messages,
                    interval_seconds=interval_seconds,
                    progress_callback=on_sent,
                    cancel_token=cancel_token,
                )

            final_status = "completed" if state["failed"] == 0 else "completed_with_errors"
            self._task_repository.update_progress(
                task_id=task_id,
                status=final_status,
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=100,
                finished=True,
            )
            task_logger.info(
                "Forward task %s finished: success=%s failed=%s skipped=%s",
                task_id,
                state["success"],
                state["failed"],
                state["skipped"],
            )
            return ForwardReport(
                task_id=task_id,
                target_chat_id=clean_target_chat_id,
                total_count=total_count,
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                log_file=task_log_file,
            )
        except OperationCancelled:
            self._task_repository.update_progress(
                task_id=task_id,
                status="cancelled",
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.info("Forward task %s cancelled by user", task_id)
            raise
        except TelegramServiceError as exc:
            self._task_repository.update_progress(
                task_id=task_id,
                status="failed",
                success_count=state["success"],
                failed_count=max(1, state["failed"]),
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.exception("Forward task %s telegram failure: %s", task_id, exc)
            raise
        except Exception as exc:
            self._task_repository.update_progress(
                task_id=task_id,
                status="failed",
                success_count=state["success"],
                failed_count=max(1, state["failed"]),
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.exception("Forward task %s failed: %s", task_id, exc)
            raise ForwardServiceError("卡片转发失败，请查看 forward.log") from exc

    def forward_message_records(
        self,
        api_id: str,
        api_hash: str,
        messages: Sequence[MessageRecord],
        target_chat_id: int,
        interval_seconds: int = 3,
        progress_callback: Optional[Callable[[ForwardProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> ForwardReport:
        """Forward selected locally backed-up chat records as plain text messages."""
        check_cancelled(cancel_token)
        selected_messages = self._ordered_message_records(messages)
        if not selected_messages:
            error = ForwardServiceError("请选择要转发的聊天记录")
            error.error_code = FW002
            raise error
        self._enforce_task_limit(len(selected_messages), "聊天记录")

        try:
            clean_target_chat_id = int(target_chat_id)
        except (TypeError, ValueError) as exc:
            error = ForwardServiceError("请选择有效的目标群或聊天")
            error.error_code = FW001
            raise error from exc
        if clean_target_chat_id == 0:
            error = ForwardServiceError("请选择有效的目标群或聊天")
            error.error_code = FW001
            raise error

        return self._forward_message_records_to_target(
            api_id=api_id,
            api_hash=api_hash,
            messages=selected_messages,
            target_chat_id=clean_target_chat_id,
            interval_seconds=interval_seconds,
            progress_callback=progress_callback,
            cancel_token=cancel_token,
        )

    def forward_message_records_auto_group(
        self,
        api_id: str,
        api_hash: str,
        messages: Sequence[MessageRecord],
        group_title: str = "",
        interval_seconds: int = 3,
        progress_callback: Optional[Callable[[ForwardProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> ForwardReport:
        """Create one target group and forward selected backed-up chat records into it."""
        check_cancelled(cancel_token)
        if self._group_service is None:
            error = ForwardServiceError("自动建群转发未配置 GroupService")
            error.error_code = FW001
            raise error

        selected_messages = self._ordered_message_records(messages)
        if not selected_messages:
            error = ForwardServiceError("请选择要转发的聊天记录")
            error.error_code = FW002
            raise error
        self._enforce_task_limit(len(selected_messages), "聊天记录")

        clean_group_title = str(group_title).strip() or self._default_message_group_title(selected_messages)
        check_cancelled(cancel_token)
        chat = self._group_service.create_target_group(
            api_id=api_id,
            api_hash=api_hash,
            title=clean_group_title,
            category="chat_history",
        )
        check_cancelled(cancel_token)
        report = self._forward_message_records_to_target(
            api_id=api_id,
            api_hash=api_hash,
            messages=selected_messages,
            target_chat_id=chat.tg_chat_id,
            interval_seconds=interval_seconds,
            progress_callback=progress_callback,
            target_strategy="auto_group",
            target_group_titles=(chat.title,),
            cancel_token=cancel_token,
        )
        return report

    def forward_search_result_cards_auto_group(
        self,
        api_id: str,
        api_hash: str,
        result_ids: Sequence[int],
        group_by: str,
        group_title_prefix: str,
        interval_seconds: int = 3,
        skip_duplicates: bool = True,
        progress_callback: Optional[Callable[[ForwardProgress], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> ForwardReport:
        """Create target groups by category/date and forward selected result cards into them."""
        check_cancelled(cancel_token)
        if self._group_service is None:
            error = ForwardServiceError("自动建群转发未配置 GroupService")
            error.error_code = FW001
            raise error

        selected_ids = [int(result_id) for result_id in result_ids if int(result_id) > 0]
        if not selected_ids:
            error = ForwardServiceError("请选择要转发的搜索结果")
            error.error_code = FW002
            raise error
        self._enforce_task_limit(len(selected_ids), "搜索结果")
        if self._search_repository is None:
            error = ForwardServiceError("搜索结果转发未配置结果仓库")
            error.error_code = FW003
            raise error

        clean_group_by = str(group_by).strip().lower()
        if clean_group_by not in {"category", "date"}:
            error = ForwardServiceError("自动建群策略无效")
            error.error_code = FW001
            raise error

        results = self._search_repository.get_results_by_ids(selected_ids)
        if not results:
            error = ForwardServiceError("所选搜索结果不存在，请刷新后重试")
            error.error_code = FW002
            raise error

        task_id = self._new_task_id()
        total_count = len(results)
        task_logger = self._task_logger(task_id)
        task_log_file = self._task_log_file(task_id)
        buckets = self._bucket_results(results, clean_group_by)
        title_prefix = str(group_title_prefix).strip() or "TG整理"
        self._task_repository.create_task(
            task_id=task_id,
            task_type="forward",
            title=f"自动建群卡片转发 {total_count} 条",
            source_config={"source": "public_search_results", "result_ids": selected_ids},
            target_config={"target_strategy": "auto_group", "group_by": clean_group_by, "group_title_prefix": title_prefix},
            total_count=total_count,
            log_file=task_log_file,
        )
        task_logger.info(
            "Auto-group forward task %s started: total=%s group_by=%s interval=%ss",
            task_id,
            total_count,
            clean_group_by,
            interval_seconds,
        )

        state = {"done": 0, "success": 0, "failed": 0, "skipped": 0}
        created_group_titles: list[str] = []
        try:
            for bucket_key, bucket_results in buckets:
                check_cancelled(cancel_token)
                sendable_results: list[SearchResult] = []
                for result in bucket_results:
                    check_cancelled(cancel_token)
                    if result.id is None:
                        continue
                    skip_reason = self._skip_reason(result, skip_duplicates)
                    if skip_reason:
                        self._record_result(
                            task_id,
                            result,
                            None,
                            TelegramSendResult(
                                source_id=int(result.id),
                                status="skipped",
                                reason=skip_reason,
                            ),
                            state,
                            total_count,
                            progress_callback,
                            task_logger,
                        )
                    else:
                        sendable_results.append(result)

                if not sendable_results:
                    continue

                group_title = self._build_group_title(title_prefix, bucket_key)
                check_cancelled(cancel_token)
                chat = self._group_service.create_target_group(api_id, api_hash, group_title, category=bucket_key)
                check_cancelled(cancel_token)
                created_group_titles.append(chat.title)
                result_by_id = {int(result.id): result for result in sendable_results if result.id is not None}
                messages = [
                    TelegramOutgoingMessage(source_id=int(result.id), text=self.format_card(result))
                    for result in sendable_results
                    if result.id is not None
                ]

                def on_sent(send_result: TelegramSendResult, _sent_done: int, _sent_total: int) -> None:
                    result = result_by_id.get(send_result.source_id)
                    if result is None:
                        return
                    self._record_result(
                        task_id,
                        result,
                        chat.tg_chat_id,
                        send_result,
                        state,
                        total_count,
                        progress_callback,
                        task_logger,
                    )

                self._send_text_messages(
                    api_id=api_id,
                    api_hash=api_hash,
                    target_chat_id=chat.tg_chat_id,
                    messages=messages,
                    interval_seconds=interval_seconds,
                    progress_callback=on_sent,
                    cancel_token=cancel_token,
                )

            final_status = "completed" if state["failed"] == 0 else "completed_with_errors"
            self._task_repository.update_progress(
                task_id=task_id,
                status=final_status,
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=100,
                finished=True,
            )
            task_logger.info(
                "Auto-group forward task %s finished: success=%s failed=%s skipped=%s groups=%s",
                task_id,
                state["success"],
                state["failed"],
                state["skipped"],
                len(created_group_titles),
            )
            return ForwardReport(
                task_id=task_id,
                target_chat_id=0,
                total_count=total_count,
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                log_file=task_log_file,
                target_group_titles=tuple(created_group_titles),
            )
        except OperationCancelled:
            self._task_repository.update_progress(
                task_id=task_id,
                status="cancelled",
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.info("Auto-group forward task %s cancelled by user", task_id)
            raise
        except TelegramServiceError as exc:
            self._task_repository.update_progress(
                task_id=task_id,
                status="failed",
                success_count=state["success"],
                failed_count=max(1, state["failed"]),
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.exception("Auto-group forward task %s telegram failure: %s", task_id, exc)
            raise
        except Exception as exc:
            self._task_repository.update_progress(
                task_id=task_id,
                status="failed",
                success_count=state["success"],
                failed_count=max(1, state["failed"]),
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.exception("Auto-group forward task %s failed: %s", task_id, exc)
            raise ForwardServiceError("自动建群卡片转发失败，请查看 forward.log") from exc

    @staticmethod
    def format_card(result: SearchResult) -> str:
        """Format one public search result as a plain text card."""
        lines = [
            "TGArchiveManager 搜索结果卡片",
            f"关键词：{ForwardService._trim(result.keyword, 80)}",
            f"来源：{ForwardService._trim(result.engine_name, 40)} #{result.rank_no}",
            f"类型：{ForwardService._trim(ForwardService._display_result_type(result), 40)}",
            f"标题：{ForwardService._trim(result.title, 180)}",
        ]
        media_info = ForwardService.result_media_info(result)
        if media_info:
            lines.append(f"媒体信息：{ForwardService._trim(media_info, 120)}")
        if result.summary:
            lines.append(f"摘要：{ForwardService._trim(result.summary, 700)}")
        if result.url:
            lines.append(f"链接：{ForwardService._trim(result.url, 600)}")
        else:
            lines.append("链接：无，仅转发文本卡片")
        if result.tg_chat_id is not None and result.tg_message_id is not None:
            lines.append(f"原消息：chat_id={result.tg_chat_id} message_id={result.tg_message_id}")
        return "\n".join(lines)[:3500]

    @staticmethod
    def format_message_record(message: MessageRecord) -> str:
        """Format one locally backed-up chat message as a plain text record."""
        lines = [
            "TGArchiveManager 聊天记录",
            f"来源：chat_id={message.tg_chat_id} message_id={message.message_id}",
            f"时间：{ForwardService._trim(message.date, 80) or '-'}",
            f"发送者：{ForwardService._trim(message.sender_name, 120) or '-'}",
            f"类型：{ForwardService._trim(message.message_type or message.media_type, 40) or 'unknown'}",
        ]
        content = message.text or message.text_preview
        if content:
            lines.append(f"内容：{ForwardService._trim(content, 2200)}")
        else:
            lines.append("内容：无文本内容")
        if message.has_media:
            media_parts = [ForwardService._trim(message.media_type or "media", 40)]
            if message.file_name:
                media_parts.append(f"文件={ForwardService._trim(message.file_name, 180)}")
            if message.file_size is not None:
                media_parts.append(f"大小={ForwardService._format_size(message.file_size)}")
            media_parts.append("已下载=是" if message.is_downloaded else "已下载=否")
            lines.append(f"媒体：{'，'.join(media_parts)}")
        if ForwardService._is_download_link_media(message):
            lines.append(f"下载链接：{ForwardService._download_link_for_message(message)}")
            if message.source_link:
                lines.append(f"Telegram 原文：{ForwardService._trim(message.source_link, 600)}")
            elif message.local_path:
                lines.append(f"本地文件：{ForwardService._trim(message.local_path, 600)}")
        elif message.source_link:
            lines.append(f"原始链接：{ForwardService._trim(message.source_link, 600)}")
        return "\n".join(lines)[:3500]

    def _forward_message_records_to_target(
        self,
        api_id: str,
        api_hash: str,
        messages: Sequence[MessageRecord],
        target_chat_id: int,
        interval_seconds: int,
        progress_callback: Optional[Callable[[ForwardProgress], None]],
        target_strategy: str = "existing",
        target_group_titles: tuple[str, ...] = (),
        cancel_token: Optional[CancellationToken] = None,
    ) -> ForwardReport:
        check_cancelled(cancel_token)
        selected_messages = self._ordered_message_records(messages)
        task_id = self._new_task_id()
        total_count = len(selected_messages)
        task_logger = self._task_logger(task_id)
        task_log_file = self._task_log_file(task_id)
        source_ids = [int(message.id) for message in selected_messages if message.id is not None]
        self._task_repository.create_task(
            task_id=task_id,
            task_type="forward",
            title=f"聊天记录转发 {total_count} 条",
            source_config={"source": "messages", "message_ids": source_ids},
            target_config={
                "target_chat_id": int(target_chat_id),
                "target_strategy": target_strategy,
                "forward_mode": "message_text",
            },
            total_count=total_count,
            log_file=task_log_file,
        )
        task_logger.info(
            "Message forward task %s started: total=%s target_chat_id=%s interval=%ss",
            task_id,
            total_count,
            target_chat_id,
            interval_seconds,
        )

        state = {"done": 0, "success": 0, "failed": 0, "skipped": 0}
        message_by_source_id: dict[int, MessageRecord] = {}
        outgoing_messages: list[TelegramOutgoingMessage] = []
        for index, message in enumerate(selected_messages, start=1):
            check_cancelled(cancel_token)
            source_id = int(message.id) if message.id is not None else index
            message_by_source_id[source_id] = message
            outgoing_messages.append(
                TelegramOutgoingMessage(source_id=source_id, text=self.format_message_record(message))
            )

        try:
            def on_sent(send_result: TelegramSendResult, _sent_done: int, _sent_total: int) -> None:
                message = message_by_source_id.get(send_result.source_id)
                if message is None:
                    return
                self._record_message_result(
                    task_id,
                    message,
                    int(target_chat_id),
                    send_result,
                    state,
                    total_count,
                    progress_callback,
                    task_logger,
                )

            self._send_text_messages(
                api_id=api_id,
                api_hash=api_hash,
                target_chat_id=int(target_chat_id),
                messages=outgoing_messages,
                interval_seconds=max(0, int(interval_seconds)),
                progress_callback=on_sent,
                cancel_token=cancel_token,
            )

            final_status = "completed" if state["failed"] == 0 else "completed_with_errors"
            self._task_repository.update_progress(
                task_id=task_id,
                status=final_status,
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=100,
                finished=True,
            )
            task_logger.info(
                "Message forward task %s finished: success=%s failed=%s skipped=%s",
                task_id,
                state["success"],
                state["failed"],
                state["skipped"],
            )
            return ForwardReport(
                task_id=task_id,
                target_chat_id=int(target_chat_id),
                total_count=total_count,
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                log_file=task_log_file,
                target_group_titles=target_group_titles,
            )
        except OperationCancelled:
            self._task_repository.update_progress(
                task_id=task_id,
                status="cancelled",
                success_count=state["success"],
                failed_count=state["failed"],
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.info("Message forward task %s cancelled by user", task_id)
            raise
        except TelegramServiceError as exc:
            self._task_repository.update_progress(
                task_id=task_id,
                status="failed",
                success_count=state["success"],
                failed_count=max(1, state["failed"]),
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.exception("Message forward task %s telegram failure: %s", task_id, exc)
            raise
        except Exception as exc:
            self._task_repository.update_progress(
                task_id=task_id,
                status="failed",
                success_count=state["success"],
                failed_count=max(1, state["failed"]),
                skipped_count=state["skipped"],
                progress=self._progress_percent(state["done"], total_count),
                finished=True,
            )
            task_logger.exception("Message forward task %s failed: %s", task_id, exc)
            raise ForwardServiceError("聊天记录转发失败，请查看 forward.log") from exc

    def _record_result(
        self,
        task_id: str,
        result: SearchResult,
        target_chat_id: Optional[int],
        send_result: TelegramSendResult,
        state: dict[str, int],
        total_count: int,
        progress_callback: Optional[Callable[[ForwardProgress], None]],
        task_logger: logging.Logger,
    ) -> None:
        state["done"] += 1
        if send_result.status == "success":
            state["success"] += 1
            forward_status = "success"
            message = f"已发送：{result.title or result.normalized_url}"
        elif send_result.status == "skipped":
            state["skipped"] += 1
            forward_status = "skipped"
            message = f"已跳过：{result.title or result.normalized_url}"
        else:
            state["failed"] += 1
            forward_status = "failed"
            message = f"发送失败：{result.title or result.normalized_url}"

        record = ForwardRecord(
            id=None,
            task_id=task_id,
            source_type="public_search_result",
            source_id=result.id,
            source_chat_id=result.tg_chat_id,
            source_message_id=result.tg_message_id,
            target_chat_id=target_chat_id,
            target_message_id=send_result.target_message_id,
            forward_mode="card",
            status=send_result.status,
            reason=send_result.reason,
            error_code=send_result.error_code,
        )
        self._forward_repository.create_record(record)
        if result.id is not None:
            self._search_repository.update_forward_status(int(result.id), forward_status)

        self._task_repository.update_progress(
            task_id=task_id,
            status="running",
            success_count=state["success"],
            failed_count=state["failed"],
            skipped_count=state["skipped"],
            progress=self._progress_percent(state["done"], total_count),
        )
        task_logger.info(
            "Forward task %s item processed: source_id=%s status=%s target_message_id=%s",
            task_id,
            result.id,
            send_result.status,
            send_result.target_message_id,
        )
        if progress_callback is not None:
            progress_callback(
                ForwardProgress(
                    task_id=task_id,
                    source_id=result.id,
                    done_count=state["done"],
                    total_count=total_count,
                    success_count=state["success"],
                    failed_count=state["failed"],
                    skipped_count=state["skipped"],
                    status=send_result.status,
                    message=message,
                )
            )

    def _record_message_result(
        self,
        task_id: str,
        message: MessageRecord,
        target_chat_id: Optional[int],
        send_result: TelegramSendResult,
        state: dict[str, int],
        total_count: int,
        progress_callback: Optional[Callable[[ForwardProgress], None]],
        task_logger: logging.Logger,
    ) -> None:
        state["done"] += 1
        source_label = f"chat_id={message.tg_chat_id} message_id={message.message_id}"
        if send_result.status == "success":
            state["success"] += 1
            progress_message = f"已发送：{source_label}"
            if self._message_repository is not None and message.id is not None:
                self._message_repository.mark_forwarded(int(message.id))
        elif send_result.status == "skipped":
            state["skipped"] += 1
            progress_message = f"已跳过：{source_label}"
        else:
            state["failed"] += 1
            progress_message = f"发送失败：{source_label}"

        self._forward_repository.create_record(
            ForwardRecord(
                id=None,
                task_id=task_id,
                source_type="message_record",
                source_id=message.id,
                source_chat_id=message.tg_chat_id,
                source_message_id=message.message_id,
                target_chat_id=target_chat_id,
                target_message_id=send_result.target_message_id,
                forward_mode="message_text",
                status=send_result.status,
                reason=send_result.reason,
                error_code=send_result.error_code,
            )
        )

        self._task_repository.update_progress(
            task_id=task_id,
            status="running",
            success_count=state["success"],
            failed_count=state["failed"],
            skipped_count=state["skipped"],
            progress=self._progress_percent(state["done"], total_count),
        )
        task_logger.info(
            "Message forward task %s item processed: source_chat_id=%s source_message_id=%s status=%s target_message_id=%s",
            task_id,
            message.tg_chat_id,
            message.message_id,
            send_result.status,
            send_result.target_message_id,
        )
        if progress_callback is not None:
            progress_callback(
                ForwardProgress(
                    task_id=task_id,
                    source_id=message.id,
                    done_count=state["done"],
                    total_count=total_count,
                    success_count=state["success"],
                    failed_count=state["failed"],
                    skipped_count=state["skipped"],
                    status=send_result.status,
                    message=progress_message,
                )
            )

    def _task_logger(self, task_id: str) -> logging.Logger:
        if self._task_logger_factory is None:
            return self._logger
        return self._task_logger_factory(task_id, "forward")

    def _task_log_file(self, task_id: str) -> str:
        if self._task_log_path_factory is None:
            return self._log_file
        return str(self._task_log_path_factory(task_id, "forward"))

    def _enforce_task_limit(self, count: int, label: str) -> None:
        if self._max_per_task <= 0 or int(count) <= self._max_per_task:
            return
        error = ForwardServiceError(f"单次最多转发 {self._max_per_task} 条{label}，请减少勾选数量")
        error.error_code = FW002
        raise error

    def _send_text_messages(
        self,
        api_id: str,
        api_hash: str,
        target_chat_id: int,
        messages: list[TelegramOutgoingMessage],
        interval_seconds: int,
        progress_callback: Optional[Callable[[TelegramSendResult, int, int], None]],
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramSendResult]:
        check_cancelled(cancel_token)
        send_method = self._telegram_service.send_text_messages
        if self._callable_accepts_cancel_token(send_method):
            return send_method(
                api_id=api_id,
                api_hash=api_hash,
                target_chat_id=target_chat_id,
                messages=messages,
                interval_seconds=interval_seconds,
                progress_callback=progress_callback,
                cancel_token=cancel_token,
            )
        return send_method(
            api_id=api_id,
            api_hash=api_hash,
            target_chat_id=target_chat_id,
            messages=messages,
            interval_seconds=interval_seconds,
            progress_callback=progress_callback,
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

    @staticmethod
    def _new_task_id() -> str:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        return f"forward_{timestamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _trim(value: str, limit: int) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)]}..."

    @staticmethod
    def _skip_reason(result: SearchResult, skip_duplicates: bool) -> str:
        if skip_duplicates and (result.is_duplicate or result.forward_status == "success"):
            return "重复结果或已成功转发"
        return ""

    @staticmethod
    def _display_result_type(result: SearchResult) -> str:
        if result.result_type == "telegraph_page":
            return "Telegraph 图片页面卡片"
        return result.result_type

    @staticmethod
    def result_media_info(result: SearchResult) -> str:
        """Return a compact media-size or Telegraph image-count summary for UI and cards."""
        summary = str(result.summary or "")
        if result.result_type == "telegraph_page":
            image_count = ForwardService._summary_value(summary, "图片数量")
            if image_count:
                return f"Telegraph 图片：{image_count} 张"
            return "Telegraph 图片：未知"

        size_text = ForwardService._summary_value(summary, "大小")
        if size_text:
            return f"文件大小：{size_text}"

        result_type = str(result.result_type or "").strip().lower()
        if result_type in {"photo", "image", "video", "document", "file", "audio", "media"}:
            return "文件大小：未知"
        return ""

    @staticmethod
    def _summary_value(summary: str, label: str) -> str:
        match = re.search(r"(?:^|；)" + re.escape(label) + r"：([^；]+)", str(summary or ""))
        return match.group(1).strip() if match else ""

    @staticmethod
    def _is_download_link_media(message: MessageRecord) -> bool:
        media_type = str(message.media_type or message.message_type or "").strip().lower()
        return media_type in {"photo", "image", "video"}

    @staticmethod
    def _download_link_for_message(message: MessageRecord) -> str:
        return f"tgarchive://download?chat_id={int(message.tg_chat_id)}&message_id={int(message.message_id)}"

    @staticmethod
    def _ordered_message_records(messages: Sequence[MessageRecord]) -> list[MessageRecord]:
        clean_messages = [message for message in messages if isinstance(message, MessageRecord)]
        return sorted(
            clean_messages,
            key=lambda message: (
                str(message.date or ""),
                int(message.tg_chat_id),
                int(message.message_id),
                int(message.id or 0),
            ),
        )

    @staticmethod
    def _default_message_group_title(messages: Sequence[MessageRecord]) -> str:
        first = messages[0] if messages else None
        today = datetime.now().strftime("%Y%m%d")
        if first is None:
            return f"聊天记录_{today}"
        chat_part = str(first.tg_chat_id)
        if len({message.tg_chat_id for message in messages}) > 1:
            chat_part = "多聊天"
        return f"聊天记录_{chat_part}_{today}"[:120]

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

    @staticmethod
    def _progress_percent(done_count: int, total_count: int) -> int:
        if total_count <= 0:
            return 0
        return int(min(100, max(0, round(done_count * 100 / total_count))))

    @staticmethod
    def _bucket_results(results: list[SearchResult], group_by: str) -> list[tuple[str, list[SearchResult]]]:
        buckets: dict[str, list[SearchResult]] = {}
        for result in results:
            if group_by == "date":
                key = result.created_at[:10] if result.created_at else "unknown_date"
            else:
                key = result.result_type or "unknown"
            buckets.setdefault(key, []).append(result)
        return list(buckets.items())

    @staticmethod
    def _build_group_title(prefix: str, key: str) -> str:
        safe_key = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_.-]+", "_", str(key).strip()).strip("_") or "未分类"
        today = datetime.now().strftime("%Y%m%d")
        title = f"{prefix}_{safe_key}_{today}"
        return title[:120]
