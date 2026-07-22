"""Telegram account login service built around Telethon."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from database.models import Chat
from database.repositories import AccountRepository, ChatRepository
from services.cancellation import CancellationToken, OperationCancelled, async_sleep_with_cancel, check_cancelled
from utils.error_codes import SE001, TG001, TG004, TG005, TG006


class TelegramServiceError(RuntimeError):
    """Base error for Telegram service failures."""

    error_code = TG005


class TelegramDependencyError(TelegramServiceError):
    """Raised when Telethon is unavailable."""


class TelegramLoginError(TelegramServiceError):
    """Raised when login cannot be completed."""


class TelegramPasswordRequired(TelegramServiceError):
    """Raised when Telegram requires two-step verification."""

    error_code = TG006


@dataclass(frozen=True)
class TelegramAccountInfo:
    """Sanitized account information safe to show in the UI."""

    phone: str
    display_name: str
    username: str
    session_path: str


@dataclass(frozen=True)
class CodeRequestResult:
    """Result returned after Telegram sends a login verification code."""

    phone: str
    session_path: str


@dataclass(frozen=True)
class TelegramBotResponse:
    """Sanitized bot response content used by public search providers."""

    message_id: int
    text: str
    button_urls: list[str]
    button_texts: list[str]
    text_links: list["TelegramTextLink"]
    media_path: str = ""


@dataclass(frozen=True)
class TelegramTextLink:
    """A URL attached to a text range through Telegram message entities."""

    text: str
    url: str


@dataclass(frozen=True)
class TelegramOutgoingMessage:
    """Text message to send through Telegram with an application source id."""

    source_id: int
    text: str


@dataclass(frozen=True)
class TelegramSendResult:
    """Result of sending one text message through Telegram."""

    source_id: int
    status: str
    target_message_id: Optional[int] = None
    reason: str = ""
    error_code: str = ""


@dataclass(frozen=True)
class TelegramArchivedMessage:
    """Sanitized Telegram message metadata for local backup."""

    tg_chat_id: int
    message_id: int
    sender_id: Optional[int]
    sender_name: str
    date: str
    text: str
    text_preview: str
    message_type: str
    has_media: bool = False
    media_type: str = ""
    media_id: str = ""
    file_name: str = ""
    file_size: Optional[int] = None
    is_protected: bool = False
    source_link: str = ""
    webpage_url: str = ""
    external_urls: tuple[str, ...] = ()
    chat_title: str = ""


@dataclass(frozen=True)
class TelegramMediaDownloadResult:
    """Result of downloading one Telegram message media file."""

    tg_chat_id: int
    message_id: int
    status: str
    local_path: str = ""
    file_name: str = ""
    file_size: Optional[int] = None
    downloaded_image_count: int = 0
    image_count: int = 0
    error_code: str = ""
    error_message: str = ""


class TelegramService:
    """Coordinate Telethon login without exposing secrets to UI code."""

    def __init__(
        self,
        project_root: Path,
        config: Dict[str, Any],
        account_repository: AccountRepository,
        logger: logging.Logger,
        chat_repository: Optional[ChatRepository] = None,
    ):
        self._project_root = Path(project_root)
        self._config = config
        self._account_repository = account_repository
        self._chat_repository = chat_repository
        self._logger = logger
        self._pending_phone: Optional[str] = None
        self._pending_phone_code_hash: Optional[str] = None
        self._pending_api_id: Optional[int] = None
        self._pending_api_hash: Optional[str] = None

    @property
    def session_file_path(self) -> Path:
        """Return the configured Telethon session file path."""
        return self._session_base_path().with_suffix(".session")

    @staticmethod
    def dependencies_available() -> bool:
        """Return whether Telethon can be imported in this Python environment."""
        try:
            import telethon  # noqa: F401
        except ImportError:
            return False
        return True

    def send_code(self, api_id: str, api_hash: str, phone: str) -> CodeRequestResult:
        """Send the Telegram verification code request."""
        clean_phone = self._normalize_phone(phone)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")

        self._logger.info("Requesting Telegram login code for phone ending %s", self._phone_suffix(clean_phone))
        try:
            result = self._run_async(self._send_code_async(parsed_api_id, clean_api_hash, clean_phone))
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram login code request failed: %s", exc)
            raise TelegramLoginError("验证码发送失败，请查看 error.log") from exc

        self._pending_phone = clean_phone
        self._pending_phone_code_hash = result.phone_code_hash
        self._pending_api_id = parsed_api_id
        self._pending_api_hash = clean_api_hash
        self._logger.info("Telegram login code request completed for phone ending %s", self._phone_suffix(clean_phone))
        return CodeRequestResult(phone=clean_phone, session_path=str(self.session_file_path))

    def sign_in_with_code(self, code: str) -> TelegramAccountInfo:
        """Complete Telegram login with a verification code."""
        clean_code = self._require_value(code, "verification_code")
        if not self._pending_phone or not self._pending_phone_code_hash:
            raise TelegramLoginError("请先发送验证码")
        if self._pending_api_id is None or not self._pending_api_hash:
            raise TelegramLoginError("登录上下文已失效，请重新发送验证码")

        self._logger.info("Submitting Telegram login code for phone ending %s", self._phone_suffix(self._pending_phone))
        try:
            account = self._run_async(
                self._sign_in_code_async(
                    self._pending_api_id,
                    self._pending_api_hash,
                    self._pending_phone,
                    clean_code,
                    self._pending_phone_code_hash,
                )
            )
        except TelegramPasswordRequired:
            self._logger.info(
                "Telegram two-step verification required for phone ending %s",
                self._phone_suffix(self._pending_phone),
            )
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram login with code failed: %s", exc)
            raise TelegramLoginError("验证码登录失败，请查看 error.log") from exc

        return self._save_account(account)

    def sign_in_with_password(self, password: str) -> TelegramAccountInfo:
        """Complete Telegram two-step verification with a password."""
        clean_password = self._require_value(password, "password")
        if self._pending_api_id is None or not self._pending_api_hash:
            raise TelegramLoginError("登录上下文已失效，请重新发送验证码")

        self._logger.info("Submitting Telegram two-step verification password")
        try:
            account = self._run_async(
                self._sign_in_password_async(self._pending_api_id, self._pending_api_hash, clean_password)
            )
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram two-step verification failed: %s", exc)
            raise TelegramLoginError("二步验证失败，请查看 error.log") from exc

        return self._save_account(account)

    def restore_session(self, api_id: str, api_hash: str) -> TelegramAccountInfo:
        """Restore an existing authorized Telethon session."""
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")

        self._logger.info("Restoring Telegram session from %s", self.session_file_path)
        try:
            account = self._run_async(self._restore_session_async(parsed_api_id, clean_api_hash))
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram session restore failed: %s", exc)
            raise TelegramLoginError("session 恢复失败，请重新登录") from exc

        return self._save_account(account)

    def logout(self, api_id: str, api_hash: str) -> None:
        """Log out the current Telegram session when possible."""
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")

        self._logger.info("Logging out Telegram session")
        try:
            self._run_async(self._logout_async(parsed_api_id, clean_api_hash))
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram logout failed: %s", exc)
            raise TelegramLoginError("退出登录失败，请查看 error.log") from exc

        self._pending_phone = None
        self._pending_phone_code_hash = None
        self._pending_api_id = None
        self._pending_api_hash = None
        self._logger.info("Telegram logout completed")

    def sync_chats(self, api_id: str, api_hash: str) -> list[Chat]:
        """Synchronize the authorized account's accessible Telegram chats."""
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")

        self._logger.info("Synchronizing Telegram chat list")
        try:
            chats = self._run_async(self._sync_chats_async(parsed_api_id, clean_api_hash))
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram chat sync failed: %s", exc)
            raise TelegramLoginError("聊天列表同步失败，请查看 error.log") from exc

        if self._chat_repository is not None:
            chats = self._chat_repository.upsert_many(chats)

        self._logger.info("Telegram chat sync completed: %s chats", len(chats))
        return chats

    def query_search_bot(
        self,
        api_id: str,
        api_hash: str,
        bot_username: str,
        keyword: str,
        response_limit: int = 10,
        timeout_seconds: int = 30,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramBotResponse]:
        """Send a keyword to a configured Telegram bot and collect its responses."""
        check_cancelled(cancel_token)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        clean_bot_username = self._require_value(bot_username, "bot_username")
        clean_keyword = self._require_value(keyword, "keyword")

        self._logger.info("Querying Telegram search bot '%s'", clean_bot_username)
        try:
            responses = self._run_async(
                self._query_search_bot_async(
                    parsed_api_id,
                    clean_api_hash,
                    clean_bot_username,
                    clean_keyword,
                    response_limit,
                    timeout_seconds,
                    cancel_token,
                )
            )
        except OperationCancelled:
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram search bot query failed: %s", exc)
            raise TelegramLoginError("公开搜索 Bot 调用失败，请查看 error.log") from exc

        if not responses:
            error = TelegramLoginError("搜索 Bot 无响应")
            error.error_code = SE001
            raise error

        self._logger.info("Telegram search bot returned %s response messages", len(responses))
        return responses

    def click_bot_button(self, api_id: str, api_hash: str, bot_username: str, message_id: int, button_text: str) -> None:
        """Click one button in a bot message after the user manually selects it."""
        self.click_bot_button_and_collect_responses(
            api_id=api_id,
            api_hash=api_hash,
            bot_username=bot_username,
            message_id=message_id,
            button_text=button_text,
        )

    def click_bot_button_and_collect_responses(
        self,
        api_id: str,
        api_hash: str,
        bot_username: str,
        message_id: int,
        button_text: str,
        response_limit: int = 10,
        timeout_seconds: int = 30,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramBotResponse]:
        """Click one bot verification button and collect the follow-up bot responses."""
        check_cancelled(cancel_token)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        clean_bot_username = self._require_value(bot_username, "bot_username")
        clean_button_text = self._require_value(button_text, "button_text")
        parsed_message_id = int(message_id)
        if parsed_message_id <= 0:
            raise TelegramLoginError("验证消息 ID 无效，请重新搜索获取验证信息")

        self._logger.info(
            "Submitting manual bot verification selection for bot '%s' message_id=%s",
            clean_bot_username,
            parsed_message_id,
        )
        try:
            responses = self._run_async(
                self._click_bot_button_and_collect_responses_async(
                    parsed_api_id,
                    clean_api_hash,
                    clean_bot_username,
                    parsed_message_id,
                    clean_button_text,
                    response_limit,
                    timeout_seconds,
                    cancel_token,
                )
            )
        except OperationCancelled:
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram bot verification submit failed: %s", exc)
            raise TelegramLoginError("Bot 验证提交失败，请查看 error.log") from exc

        self._logger.info("Manual bot verification selection submitted, collected %s responses", len(responses))
        return responses

    def create_group(self, api_id: str, api_hash: str, title: str) -> Chat:
        """Create a Telegram supergroup owned by the current account."""
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        clean_title = self._require_value(title, "title")

        self._logger.info("Creating Telegram group '%s'", clean_title)
        try:
            chat = self._run_async(self._create_group_async(parsed_api_id, clean_api_hash, clean_title))
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram group creation failed: %s", exc)
            raise TelegramLoginError("Telegram 群创建失败，请查看 error.log") from exc

        self._logger.info("Telegram group created: title='%s' chat_id=%s", chat.title, chat.tg_chat_id)
        return chat

    def send_text_messages(
        self,
        api_id: str,
        api_hash: str,
        target_chat_id: int,
        messages: list[TelegramOutgoingMessage],
        interval_seconds: int = 3,
        progress_callback: Optional[Callable[[TelegramSendResult, int, int], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramSendResult]:
        """Send text messages to a Telegram chat using one connected client."""
        check_cancelled(cancel_token)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        parsed_target_chat_id = int(target_chat_id)
        clean_messages = [
            TelegramOutgoingMessage(source_id=int(message.source_id), text=str(message.text))
            for message in messages
            if str(message.text).strip()
        ]
        clean_interval = max(0, int(interval_seconds))
        if not clean_messages:
            return []

        self._logger.info(
            "Sending %s text card messages to target_chat_id=%s interval=%ss",
            len(clean_messages),
            parsed_target_chat_id,
            clean_interval,
        )
        try:
            return self._run_async(
                self._send_text_messages_async(
                    parsed_api_id,
                    clean_api_hash,
                    parsed_target_chat_id,
                    clean_messages,
                    clean_interval,
                    progress_callback,
                    cancel_token,
                )
            )
        except OperationCancelled:
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram text card sending failed: %s", exc)
            raise TelegramLoginError("Telegram 卡片发送失败，请查看 error.log") from exc

    def fetch_chat_messages(
        self,
        api_id: str,
        api_hash: str,
        tg_chat_id: int,
        limit: int = 100,
        min_message_id: int = 0,
        date_from: str = "",
        date_to: str = "",
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramArchivedMessage]:
        """Fetch accessible chat messages for local backup without downloading media."""
        check_cancelled(cancel_token)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        parsed_chat_id = int(tg_chat_id)
        capped_limit = max(1, min(int(limit), 5000))

        self._logger.info(
            "Fetching Telegram messages: chat_id=%s limit=%s min_message_id=%s",
            parsed_chat_id,
            capped_limit,
            int(min_message_id),
        )
        try:
            messages = self._run_async(
                self._fetch_chat_messages_async(
                    parsed_api_id,
                    clean_api_hash,
                    parsed_chat_id,
                    capped_limit,
                    int(min_message_id),
                    str(date_from).strip(),
                    str(date_to).strip(),
                    cancel_token,
                )
            )
        except OperationCancelled:
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram message fetch failed: %s", exc)
            raise TelegramLoginError("Telegram 消息备份读取失败，请查看 error.log") from exc

        self._logger.info("Telegram message fetch completed: %s messages", len(messages))
        return messages

    def search_joined_messages(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int = 100,
        target_chat_ids: Optional[Sequence[int]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramArchivedMessage]:
        """Search messages in joined channels/groups, optionally limited to selected chats."""
        check_cancelled(cancel_token)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        clean_keyword = self._require_value(keyword, "keyword")
        capped_max_results = max(1, min(int(max_results), 100))
        parsed_target_chat_ids = self._parse_target_chat_ids(target_chat_ids)
        scope_text = "全部已加入频道/群聊" if parsed_target_chat_ids is None else f"{len(parsed_target_chat_ids)} 个指定频道/群聊"

        self._logger.info(
            "Searching joined Telegram channels/groups: keyword='%s' max_results=%s scope=%s",
            clean_keyword,
            capped_max_results,
            scope_text,
        )
        try:
            messages = self._run_async(
                self._search_joined_messages_async(
                    parsed_api_id,
                    clean_api_hash,
                    clean_keyword,
                    capped_max_results,
                    parsed_target_chat_ids,
                    cancel_token,
                )
            )
        except OperationCancelled:
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram native message search failed: %s", exc)
            raise TelegramLoginError("Telegram 原生搜索失败，请查看 error.log") from exc

        self._logger.info("Telegram native message search completed: %s messages", len(messages))
        return messages

    def download_archived_message_media(
        self,
        api_id: str,
        api_hash: str,
        tg_chat_id: int,
        message_id: int,
        download_dir: str,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        """Download media from one accessible Telegram message."""
        check_cancelled(cancel_token)
        parsed_api_id = self._parse_api_id(api_id)
        clean_api_hash = self._require_value(api_hash, "api_hash")
        parsed_chat_id = int(tg_chat_id)
        parsed_message_id = int(message_id)
        target_dir = Path(str(download_dir))
        if not target_dir.is_absolute():
            target_dir = self._project_root / target_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        self._logger.info("Downloading Telegram media: chat_id=%s message_id=%s", parsed_chat_id, parsed_message_id)
        try:
            return self._run_async(
                self._download_archived_message_media_async(
                    parsed_api_id,
                    clean_api_hash,
                    parsed_chat_id,
                    parsed_message_id,
                    target_dir,
                    progress_callback,
                    cancel_token,
                )
            )
        except OperationCancelled:
            raise
        except TelegramServiceError:
            raise
        except Exception as exc:
            self._logger.exception("Telegram media download failed: %s", exc)
            raise TelegramLoginError("Telegram 媒体下载失败，请查看 error.log") from exc

    async def _send_code_async(self, api_id: int, api_hash: str, phone: str):
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if await client.is_user_authorized():
                return type("AuthorizedCodeResult", (), {"phone_code_hash": ""})()
            return await client.send_code_request(phone)
        finally:
            await client.disconnect()

    async def _sign_in_code_async(
        self,
        api_id: int,
        api_hash: str,
        phone: str,
        code: str,
        phone_code_hash: str,
    ) -> TelegramAccountInfo:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                except modules.SessionPasswordNeededError as exc:
                    raise TelegramPasswordRequired("需要输入二步验证密码") from exc
            user = await client.get_me()
            return self._account_info_from_user(user, phone)
        finally:
            await client.disconnect()

    async def _sign_in_password_async(self, api_id: int, api_hash: str, password: str) -> TelegramAccountInfo:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                await client.sign_in(password=password)
            user = await client.get_me()
            phone = getattr(user, "phone", None) or self._pending_phone or ""
            return self._account_info_from_user(user, phone)
        finally:
            await client.disconnect()

    async def _restore_session_async(self, api_id: int, api_hash: str) -> TelegramAccountInfo:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TelegramLoginError("当前 session 未授权")
            user = await client.get_me()
            phone = getattr(user, "phone", None) or ""
            return self._account_info_from_user(user, phone)
        finally:
            await client.disconnect()

    async def _logout_async(self, api_id: int, api_hash: str) -> None:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            await client.disconnect()

    async def _sync_chats_async(self, api_id: int, api_hash: str) -> list[Chat]:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            dialog_filters = await self._get_dialog_filters(client, modules)
            chats: list[Chat] = []
            async for dialog in client.iter_dialogs():
                chat = self._chat_from_dialog(dialog)
                if dialog_filters is None:
                    chat = replace(chat, telegram_folder_names=None)
                else:
                    chat = replace(
                        chat,
                        telegram_folder_names=self._join_folder_names(
                            self._dialog_filter_names_for_dialog(modules, dialog, dialog_filters)
                        ),
                    )
                chats.append(chat)
            return chats
        finally:
            await client.disconnect()

    async def _query_search_bot_async(
        self,
        api_id: int,
        api_hash: str,
        bot_username: str,
        keyword: str,
        response_limit: int,
        timeout_seconds: int,
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramBotResponse]:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            check_cancelled(cancel_token)
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            try:
                check_cancelled(cancel_token)
                bot = await client.get_entity(bot_username)
                check_cancelled(cancel_token)
                sent_message = await client.send_message(bot, keyword)
            except modules.FloodWaitError as exc:
                error = TelegramLoginError(f"Telegram FloodWait 限流：{exc.seconds} 秒")
                error.error_code = TG004
                raise error from exc

            min_id = int(getattr(sent_message, "id", 0) or 0)
            check_cancelled(cancel_token)
            return await self._collect_bot_responses(
                client=client,
                bot=bot,
                min_message_id=min_id,
                response_limit=response_limit,
                timeout_seconds=timeout_seconds,
                include_min_message=False,
                cancel_token=cancel_token,
            )
        finally:
            await client.disconnect()

    async def _click_bot_button_and_collect_responses_async(
        self,
        api_id: int,
        api_hash: str,
        bot_username: str,
        message_id: int,
        button_text: str,
        response_limit: int,
        timeout_seconds: int,
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramBotResponse]:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            check_cancelled(cancel_token)
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            try:
                check_cancelled(cancel_token)
                bot = await client.get_entity(bot_username)
                message = await client.get_messages(bot, ids=message_id)
                if message is None:
                    raise TelegramLoginError("未找到验证消息，请重新搜索获取最新验证信息")
                check_cancelled(cancel_token)
                await self._click_message_button(message, button_text, (modules.FloodWaitError,))
                check_cancelled(cancel_token)
                return await self._collect_bot_responses(
                    client=client,
                    bot=bot,
                    min_message_id=message_id,
                    response_limit=response_limit,
                    timeout_seconds=timeout_seconds,
                    include_min_message=True,
                    cancel_token=cancel_token,
                )
            except modules.FloodWaitError as exc:
                error = TelegramLoginError(f"Telegram FloodWait 限流：{exc.seconds} 秒")
                error.error_code = TG004
                raise error from exc
        finally:
            await client.disconnect()

    async def _collect_bot_responses(
        self,
        client: Any,
        bot: Any,
        min_message_id: int,
        response_limit: int,
        timeout_seconds: int,
        include_min_message: bool,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramBotResponse]:
        """Collect bot messages until link candidates become stable or the timeout expires."""
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        responses_by_id: dict[int, TelegramBotResponse] = {}
        last_change_at = time.monotonic()

        while time.monotonic() < deadline:
            check_cancelled(cancel_token)
            changed = False
            async for message in client.iter_messages(bot, limit=response_limit):
                check_cancelled(cancel_token)
                message_id = int(getattr(message, "id", 0) or 0)
                if include_min_message:
                    if message_id < min_message_id:
                        continue
                elif message_id <= min_message_id:
                    continue
                response = await self._bot_response_from_message_with_media(client, message)
                previous = responses_by_id.get(message_id)
                if previous != response:
                    responses_by_id[message_id] = response
                    changed = True

            if changed:
                last_change_at = time.monotonic()

            responses = sorted(responses_by_id.values(), key=lambda response: response.message_id)
            has_link_candidates = any(self._bot_response_has_link_candidates(response) for response in responses)
            if has_link_candidates and time.monotonic() - last_change_at >= 2:
                break
            await async_sleep_with_cancel(cancel_token, 1)

        return sorted(responses_by_id.values(), key=lambda response: response.message_id)

    async def _create_group_async(self, api_id: int, api_hash: str, title: str) -> Chat:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            try:
                result = await client(
                    modules.functions.channels.CreateChannelRequest(
                        title=title,
                        about="Created by TGArchiveManager",
                        megagroup=True,
                    )
                )
            except modules.FloodWaitError as exc:
                error = TelegramLoginError(f"Telegram FloodWait 限流：{exc.seconds} 秒")
                error.error_code = TG004
                raise error from exc

            chats = list(getattr(result, "chats", []) or [])
            if not chats:
                raise TelegramLoginError("Telegram 未返回新建群信息")
            channel = chats[0]
            return Chat(
                id=None,
                tg_chat_id=int(modules.utils.get_peer_id(channel)),
                title=str(getattr(channel, "title", None) or title),
                username=str(getattr(channel, "username", None) or ""),
                type="group",
                tag="",
                is_created_by_tool=True,
            )
        finally:
            await client.disconnect()

    async def _send_text_messages_async(
        self,
        api_id: int,
        api_hash: str,
        target_chat_id: int,
        messages: list[TelegramOutgoingMessage],
        interval_seconds: int,
        progress_callback: Optional[Callable[[TelegramSendResult, int, int], None]],
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramSendResult]:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            check_cancelled(cancel_token)
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            target = await client.get_entity(target_chat_id)
            total = len(messages)
            results: list[TelegramSendResult] = []
            for index, outgoing in enumerate(messages):
                check_cancelled(cancel_token)
                if index > 0 and interval_seconds > 0:
                    await async_sleep_with_cancel(cancel_token, interval_seconds)

                result: TelegramSendResult
                try:
                    check_cancelled(cancel_token)
                    sent_message = await client.send_message(target, outgoing.text, link_preview=False)
                    result = TelegramSendResult(
                        source_id=outgoing.source_id,
                        status="success",
                        target_message_id=int(getattr(sent_message, "id", 0) or 0),
                    )
                except modules.FloodWaitError as exc:
                    result = TelegramSendResult(
                        source_id=outgoing.source_id,
                        status="failed",
                        reason=f"Telegram FloodWait 限流：{exc.seconds} 秒",
                        error_code=TG004,
                    )
                except Exception as exc:
                    result = TelegramSendResult(
                        source_id=outgoing.source_id,
                        status="failed",
                        reason=str(exc)[:240],
                        error_code=TG005,
                    )

                results.append(result)
                if progress_callback is not None:
                    progress_callback(result, index + 1, total)
                check_cancelled(cancel_token)
            return results
        finally:
            await client.disconnect()

    async def _fetch_chat_messages_async(
        self,
        api_id: int,
        api_hash: str,
        tg_chat_id: int,
        limit: int,
        min_message_id: int,
        date_from: str,
        date_to: str,
        cancel_token: Optional[CancellationToken],
    ) -> list[TelegramArchivedMessage]:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            check_cancelled(cancel_token)
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            entity = await self._entity_from_chat_id(client, modules, tg_chat_id)
            from_dt = self._parse_datetime_filter(date_from, end_of_day=False)
            to_dt = self._parse_datetime_filter(date_to, end_of_day=True)
            messages: list[TelegramArchivedMessage] = []
            async for message in client.iter_messages(entity, limit=limit):
                check_cancelled(cancel_token)
                message_id = int(getattr(message, "id", 0) or 0)
                if message_id <= int(min_message_id):
                    continue
                message_date = self._message_datetime(message)
                if to_dt is not None and message_date is not None and message_date > to_dt:
                    continue
                if from_dt is not None and message_date is not None and message_date < from_dt:
                    break
                messages.append(self._archived_message_from_message(message, tg_chat_id, entity))
            return sorted(messages, key=lambda item: item.message_id)
        finally:
            await client.disconnect()

    async def _search_joined_messages_async(
        self,
        api_id: int,
        api_hash: str,
        keyword: str,
        max_results: int,
        target_chat_ids: Optional[set[int]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> list[TelegramArchivedMessage]:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            check_cancelled(cancel_token)
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            results: list[TelegramArchivedMessage] = []
            seen_messages: set[tuple[int, int]] = set()
            async for dialog in client.iter_dialogs():
                check_cancelled(cancel_token)
                if len(results) >= max_results:
                    break
                if not self._dialog_is_joined_search_target(dialog):
                    continue

                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                tg_chat_id = self._dialog_tg_chat_id(modules, dialog, entity)
                if target_chat_ids is not None and not self._dialog_matches_target_chat_ids(
                    modules,
                    dialog,
                    entity,
                    target_chat_ids,
                ):
                    continue
                remaining = max_results - len(results)
                try:
                    async for message in client.iter_messages(entity, search=keyword, limit=remaining):
                        check_cancelled(cancel_token)
                        message_id = int(getattr(message, "id", 0) or 0)
                        if message_id <= 0:
                            continue
                        message_key = (tg_chat_id, message_id)
                        if message_key in seen_messages:
                            continue
                        seen_messages.add(message_key)
                        results.append(self._archived_message_from_message(message, tg_chat_id, entity))
                        if len(results) >= max_results:
                            break
                except modules.FloodWaitError as exc:
                    error = TelegramLoginError(f"Telegram FloodWait 限流：{exc.seconds} 秒")
                    error.error_code = TG004
                    raise error from exc
                except Exception as exc:
                    self._logger.warning(
                        "Telegram native search skipped dialog '%s' chat_id=%s: %s",
                        self._entity_title(entity),
                        tg_chat_id,
                        exc,
                    )

            return sorted(results, key=lambda item: item.date or "", reverse=True)[:max_results]
        finally:
            await client.disconnect()

    async def _download_archived_message_media_async(
        self,
        api_id: int,
        api_hash: str,
        tg_chat_id: int,
        message_id: int,
        download_dir: Path,
        progress_callback: Optional[Callable[[int, Optional[int]], None]] = None,
        cancel_token: Optional[CancellationToken] = None,
    ) -> TelegramMediaDownloadResult:
        modules = self._telethon_modules()
        client = modules.TelegramClient(str(self._session_base_path()), api_id, api_hash)
        await client.connect()
        try:
            check_cancelled(cancel_token)
            if not await client.is_user_authorized():
                error = TelegramLoginError("当前 session 未授权，请先登录")
                error.error_code = TG001
                raise error

            entity = await self._entity_from_chat_id(client, modules, tg_chat_id)
            check_cancelled(cancel_token)
            message = await client.get_messages(entity, ids=message_id)
            if message is None:
                return TelegramMediaDownloadResult(
                    tg_chat_id=tg_chat_id,
                    message_id=message_id,
                    status="failed",
                    error_code=TG005,
                    error_message="消息不存在或无权限访问",
                )
            if getattr(message, "media", None) is None:
                return TelegramMediaDownloadResult(
                    tg_chat_id=tg_chat_id,
                    message_id=message_id,
                    status="skipped",
                    error_code="DL002",
                    error_message="消息没有媒体",
                )

            try:
                def on_download_progress(current: int, total: Optional[int]) -> None:
                    check_cancelled(cancel_token)
                    if progress_callback is not None:
                        progress_callback(current, total)

                downloaded = await client.download_media(
                    message,
                    file=str(download_dir),
                    progress_callback=on_download_progress,
                )
            except OperationCancelled:
                raise
            except modules.FloodWaitError as exc:
                return TelegramMediaDownloadResult(
                    tg_chat_id=tg_chat_id,
                    message_id=message_id,
                    status="failed",
                    error_code=TG004,
                    error_message=f"Telegram FloodWait 限流：{exc.seconds} 秒",
                )
            except Exception as exc:
                return TelegramMediaDownloadResult(
                    tg_chat_id=tg_chat_id,
                    message_id=message_id,
                    status="failed",
                    error_code=TG005,
                    error_message=str(exc)[:240],
                )

            path = str(downloaded or "")
            if not path:
                return TelegramMediaDownloadResult(
                    tg_chat_id=tg_chat_id,
                    message_id=message_id,
                    status="failed",
                    error_code=TG005,
                    error_message="Telegram 未返回下载文件路径",
                )
            file_path = Path(path)
            size = file_path.stat().st_size if file_path.exists() else None
            return TelegramMediaDownloadResult(
                tg_chat_id=tg_chat_id,
                message_id=message_id,
                status="success",
                local_path=str(file_path),
                file_name=file_path.name,
                file_size=size,
            )
        finally:
            await client.disconnect()

    @staticmethod
    async def _entity_from_chat_id(client: Any, modules: Any, tg_chat_id: int) -> Any:
        """Resolve a stored Telegram chat id to a Telethon entity."""
        parsed_chat_id = int(tg_chat_id)
        try:
            return await client.get_entity(parsed_chat_id)
        except Exception as first_exc:
            try:
                peer_id, peer_type = modules.utils.resolve_id(parsed_chat_id)
                return await client.get_entity(peer_type(peer_id))
            except Exception as fallback_exc:
                raise first_exc from fallback_exc

    @staticmethod
    async def _click_message_button(
        message: Any,
        button_text: str,
        fatal_error_types: tuple[type[BaseException], ...] = (),
    ) -> None:
        clean_button_text = str(button_text).strip()
        try:
            await message.click(text=clean_button_text)
            return
        except Exception as first_exc:
            if isinstance(first_exc, fatal_error_types):
                raise
            coordinates = TelegramService._find_button_coordinates(getattr(message, "buttons", None), clean_button_text)
            if coordinates is None:
                raise TelegramLoginError(f"未找到验证按钮：{clean_button_text}") from first_exc
            row_index, column_index = coordinates
            try:
                await message.click(row_index, column_index)
            except Exception as second_exc:
                if isinstance(second_exc, fatal_error_types):
                    raise
                raise TelegramLoginError("验证按钮点击失败，请重新搜索获取最新验证信息") from second_exc

    def _save_account(self, account_info: TelegramAccountInfo) -> TelegramAccountInfo:
        self._account_repository.upsert_account(
            phone=account_info.phone,
            display_name=account_info.display_name,
            username=account_info.username,
            session_path=account_info.session_path,
        )
        self._logger.info(
            "Telegram login succeeded for user '%s' phone ending %s",
            account_info.username or account_info.display_name,
            self._phone_suffix(account_info.phone),
        )
        return account_info

    def _account_info_from_user(self, user: Any, fallback_phone: str) -> TelegramAccountInfo:
        first_name = getattr(user, "first_name", None) or ""
        last_name = getattr(user, "last_name", None) or ""
        display_name = f"{first_name} {last_name}".strip() or getattr(user, "username", "") or "Telegram User"
        username = getattr(user, "username", None) or ""
        phone = getattr(user, "phone", None) or fallback_phone
        return TelegramAccountInfo(
            phone=str(phone),
            display_name=display_name,
            username=str(username),
            session_path=str(self.session_file_path),
        )

    @staticmethod
    def _chat_from_dialog(dialog: Any) -> Chat:
        entity = getattr(dialog, "entity", None)
        message = getattr(dialog, "message", None)
        title = (
            getattr(dialog, "name", None)
            or getattr(entity, "title", None)
            or " ".join(
                part
                for part in (
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                )
                if part
            )
            or getattr(entity, "username", None)
            or "Untitled"
        )

        if getattr(dialog, "is_user", False):
            chat_type = "private"
        elif getattr(dialog, "is_group", False):
            chat_type = "group"
        elif getattr(dialog, "is_channel", False):
            chat_type = "channel"
        else:
            chat_type = "unknown"

        return Chat(
            id=None,
            tg_chat_id=int(getattr(dialog, "id", 0)),
            title=str(title),
            username=str(getattr(entity, "username", None) or ""),
            type=chat_type,
            tag="",
            is_created_by_tool=False,
            last_message_id=getattr(message, "id", None),
            last_backup_message_id=None,
        )

    async def _get_dialog_filters(self, client: Any, modules: Any) -> Optional[list[Any]]:
        request_class = getattr(getattr(modules.functions, "messages", None), "GetDialogFiltersRequest", None)
        if request_class is None:
            self._logger.warning("Telethon 当前版本不支持读取 Telegram 官方聊天分组")
            return None

        try:
            result = await client(request_class())
        except Exception as exc:
            self._logger.warning("读取 Telegram 官方聊天分组失败，本次仅同步聊天列表：%s", exc)
            return None

        filters = getattr(result, "filters", result)
        return list(filters or [])

    @classmethod
    def _dialog_filter_names_for_dialog(cls, modules: Any, dialog: Any, dialog_filters: Sequence[Any]) -> list[str]:
        """Return Telegram official folder names that contain one dialog."""
        names: list[str] = []
        for dialog_filter in dialog_filters:
            if cls._is_default_dialog_filter(dialog_filter):
                continue
            title = cls._dialog_filter_title(dialog_filter)
            if not title:
                continue
            if cls._dialog_matches_filter(modules, dialog, dialog_filter):
                names.append(title)
        return names

    @staticmethod
    def _is_default_dialog_filter(dialog_filter: Any) -> bool:
        return type(dialog_filter).__name__ == "DialogFilterDefault"

    @staticmethod
    def _dialog_filter_title(dialog_filter: Any) -> str:
        title = getattr(dialog_filter, "title", "")
        text = getattr(title, "text", None)
        return str(text if text is not None else title).strip()

    @classmethod
    def _dialog_matches_filter(cls, modules: Any, dialog: Any, dialog_filter: Any) -> bool:
        entity = getattr(dialog, "entity", None)
        dialog_ids = cls._dialog_candidate_chat_ids(modules, dialog, entity)
        exclude_ids = cls._dialog_filter_peer_ids(modules, getattr(dialog_filter, "exclude_peers", None))
        if dialog_ids.intersection(exclude_ids):
            return False

        include_ids = cls._dialog_filter_peer_ids(modules, getattr(dialog_filter, "include_peers", None))
        include_ids.update(cls._dialog_filter_peer_ids(modules, getattr(dialog_filter, "pinned_peers", None)))
        if dialog_ids.intersection(include_ids):
            return True

        return cls._dialog_matches_filter_flags(dialog, entity, dialog_filter)

    @classmethod
    def _dialog_filter_peer_ids(cls, modules: Any, peers: Any) -> set[int]:
        ids: set[int] = set()
        for peer in peers or []:
            ids.update(cls._peer_candidate_ids(modules, peer))
        return ids

    @classmethod
    def _peer_candidate_ids(cls, modules: Any, peer: Any) -> set[int]:
        if peer is None:
            return set()

        inner_peer = getattr(peer, "peer", None)
        if inner_peer is not None and inner_peer is not peer:
            return cls._peer_candidate_ids(modules, inner_peer)

        if type(peer).__name__ == "InputDialogPeerFolder":
            return set()

        ids: set[int] = set()
        try:
            ids.add(int(modules.utils.get_peer_id(peer)))
        except Exception:
            pass

        for attr_name in ("id", "user_id", "chat_id", "channel_id", "peer_id"):
            try:
                parsed = int(getattr(peer, attr_name))
            except (AttributeError, TypeError, ValueError):
                continue
            if parsed == 0:
                continue
            ids.add(parsed)
            ids.add(-parsed)
            if parsed > 0:
                ids.add(-(1000000000000 + parsed))
        return ids

    @staticmethod
    def _dialog_matches_filter_flags(dialog: Any, entity: Any, dialog_filter: Any) -> bool:
        is_user = bool(getattr(dialog, "is_user", False))
        is_group = bool(getattr(dialog, "is_group", False) or getattr(entity, "megagroup", False))
        is_channel = bool(getattr(dialog, "is_channel", False))
        is_broadcast = bool(is_channel and not is_group)
        is_bot = bool(getattr(entity, "bot", False))
        is_contact = bool(getattr(entity, "contact", False))

        if getattr(dialog_filter, "broadcasts", False) and is_broadcast:
            return True
        if getattr(dialog_filter, "groups", False) and is_group:
            return True
        if getattr(dialog_filter, "bots", False) and is_bot:
            return True
        if getattr(dialog_filter, "contacts", False) and is_user and is_contact:
            return True
        if getattr(dialog_filter, "non_contacts", False) and is_user and not is_contact and not is_bot:
            return True
        return False

    @staticmethod
    def _join_folder_names(folder_names: Sequence[str]) -> str:
        return "，".join(dict.fromkeys(str(name).strip() for name in folder_names if str(name).strip()))

    @classmethod
    def _archived_message_from_message(cls, message: Any, tg_chat_id: int, entity: Any) -> TelegramArchivedMessage:
        text = str(getattr(message, "raw_text", None) or getattr(message, "message", None) or "")
        file_info = getattr(message, "file", None)
        file_name = str(getattr(file_info, "name", None) or "")
        file_size = getattr(file_info, "size", None)
        media_type = cls._media_type_from_message(message)
        message_type = media_type if media_type else "text"
        message_id = int(getattr(message, "id", 0) or 0)
        sender = getattr(message, "sender", None)
        sender_name = cls._sender_display_name(sender)
        media_id = cls._media_id_from_message(message)
        source_link = cls._source_link_from_entity(entity, message_id)
        webpage_url = cls._webpage_url_from_message(message)
        external_urls = cls._external_urls_from_message(text, message, webpage_url)
        has_media = getattr(message, "media", None) is not None
        if cls._contains_telegraph_url("\n".join([text, webpage_url, "\n".join(external_urls)])):
            message_type = "telegraph_page"
            has_media = False
            media_type = ""
            media_id = ""
            file_name = ""
            file_size = None
        return TelegramArchivedMessage(
            tg_chat_id=int(tg_chat_id),
            message_id=message_id,
            sender_id=getattr(message, "sender_id", None),
            sender_name=sender_name,
            date=cls._format_message_date(getattr(message, "date", None)),
            text=text,
            text_preview=cls._text_preview(text),
            message_type=message_type,
            has_media=has_media,
            media_type=media_type,
            media_id=media_id,
            file_name=file_name,
            file_size=file_size,
            source_link=source_link,
            webpage_url=webpage_url,
            external_urls=external_urls,
            chat_title=cls._entity_title(entity),
        )

    @staticmethod
    def _dialog_is_joined_search_target(dialog: Any) -> bool:
        return bool(getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False))

    @staticmethod
    def _dialog_tg_chat_id(modules: Any, dialog: Any, entity: Any) -> int:
        try:
            return int(modules.utils.get_peer_id(entity))
        except Exception:
            return int(getattr(dialog, "id", 0) or 0)

    @classmethod
    def _dialog_matches_target_chat_ids(
        cls,
        modules: Any,
        dialog: Any,
        entity: Any,
        target_chat_ids: set[int],
    ) -> bool:
        """Return whether a Telethon dialog matches one selected local chat id."""
        if not target_chat_ids:
            return False
        return bool(cls._dialog_candidate_chat_ids(modules, dialog, entity).intersection(target_chat_ids))

    @staticmethod
    def _dialog_candidate_chat_ids(modules: Any, dialog: Any, entity: Any) -> set[int]:
        ids: set[int] = set()
        try:
            ids.add(int(modules.utils.get_peer_id(entity)))
        except Exception:
            pass

        for value in (getattr(dialog, "id", None), getattr(entity, "id", None)):
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                continue
            if parsed == 0:
                continue
            ids.add(parsed)
            ids.add(-parsed)
            if parsed > 0:
                ids.add(-(1000000000000 + parsed))
        return ids

    @staticmethod
    def _entity_title(entity: Any) -> str:
        title = (
            getattr(entity, "title", None)
            or " ".join(
                part
                for part in (
                    getattr(entity, "first_name", None),
                    getattr(entity, "last_name", None),
                )
                if part
            )
            or getattr(entity, "username", None)
            or ""
        )
        return str(title)

    @staticmethod
    def _media_type_from_message(message: Any) -> str:
        if getattr(message, "photo", None) is not None:
            return "photo"
        file_info = getattr(message, "file", None)
        mime_type = str(getattr(file_info, "mime_type", None) or "").lower()
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type:
            return "document"
        return "media" if getattr(message, "media", None) is not None else ""

    @staticmethod
    def _webpage_url_from_message(message: Any) -> str:
        candidates = [
            getattr(message, "web_preview", None),
            getattr(getattr(message, "media", None), "webpage", None),
            getattr(message, "webpage", None),
        ]
        for candidate in candidates:
            for attr_name in ("url", "display_url"):
                value = getattr(candidate, attr_name, None)
                if value:
                    return str(value).strip()
        return ""

    @classmethod
    def _external_urls_from_message(cls, text: str, message: Any, webpage_url: str) -> tuple[str, ...]:
        urls: list[str] = []
        urls.extend(cls._extract_urls_from_text(text))
        if webpage_url:
            urls.append(webpage_url)
        urls.extend(link.url for link in cls._extract_entity_links(str(text), getattr(message, "entities", None)))
        urls.extend(cls._extract_button_urls(getattr(message, "buttons", None)))
        urls.extend(cls._extract_button_urls(getattr(message, "reply_markup", None)))
        return tuple(cls._dedup_urls(urls))

    @staticmethod
    def _extract_urls_from_text(text: str) -> list[str]:
        if not text:
            return []
        pattern = re.compile(
            r"https?://[^\s<>()\"']+|t\.me/[^\s<>()\"']+|(?:www\.)?telegra\.ph/[^\s<>()\"']+",
            re.IGNORECASE,
        )
        return [match.group(0).rstrip(".,;，。；)") for match in pattern.finditer(str(text))]

    @staticmethod
    def _dedup_urls(urls: Sequence[str]) -> list[str]:
        values: list[str] = []
        seen: set[str] = set()
        for raw_url in urls:
            url = str(raw_url or "").strip()
            if not url:
                continue
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            values.append(url)
        return values

    @staticmethod
    def _is_telegraph_url(url: str) -> bool:
        return bool(re.match(r"^(?:https?://)?(?:www\.)?telegra\.ph(?:/|$)", str(url or "").strip(), re.IGNORECASE))

    @staticmethod
    def _contains_telegraph_url(text: str) -> bool:
        return bool(re.search(r"(?:https?://)?(?:www\.)?telegra\.ph(?:/|$)", str(text or ""), re.IGNORECASE))

    @staticmethod
    def _media_id_from_message(message: Any) -> str:
        media = getattr(message, "media", None)
        document = getattr(media, "document", None)
        photo = getattr(media, "photo", None)
        value = getattr(document, "id", None) or getattr(photo, "id", None) or getattr(media, "id", None)
        return "" if value is None else str(value)

    @staticmethod
    def _sender_display_name(sender: Any) -> str:
        if sender is None:
            return ""
        title = getattr(sender, "title", None)
        if title:
            return str(title)
        parts = [
            str(part)
            for part in (getattr(sender, "first_name", None), getattr(sender, "last_name", None))
            if part
        ]
        return " ".join(parts).strip() or str(getattr(sender, "username", None) or "")

    @staticmethod
    def _source_link_from_entity(entity: Any, message_id: int) -> str:
        username = getattr(entity, "username", None)
        if username and message_id > 0:
            return f"https://t.me/{username}/{message_id}"
        entity_id = getattr(entity, "id", None)
        if entity_id and message_id > 0 and (
            getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False)
        ):
            return f"https://t.me/c/{abs(int(entity_id))}/{message_id}"
        return ""

    @staticmethod
    def _text_preview(text: str, limit: int = 160) -> str:
        value = " ".join(str(text or "").split())
        if len(value) <= limit:
            return value
        return f"{value[: max(0, limit - 3)]}..."

    @staticmethod
    def _format_message_date(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _message_datetime(message: Any) -> Optional[datetime]:
        value = getattr(message, "date", None)
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        try:
            return datetime.fromisoformat(str(value)).replace(tzinfo=None)
        except ValueError:
            return None

    @staticmethod
    def _parse_datetime_filter(value: str, end_of_day: bool) -> Optional[datetime]:
        clean = str(value or "").strip()
        if not clean:
            return None
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean):
                suffix = "23:59:59" if end_of_day else "00:00:00"
                return datetime.fromisoformat(f"{clean} {suffix}")
            return datetime.fromisoformat(clean).replace(tzinfo=None)
        except ValueError:
            return None

    async def _bot_response_from_message_with_media(self, client: Any, message: Any) -> TelegramBotResponse:
        media_path = await self._download_message_media(client, message)
        return self._bot_response_from_message(message, media_path=media_path)

    async def _download_message_media(self, client: Any, message: Any) -> str:
        if getattr(message, "media", None) is None:
            return ""

        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            return ""

        media_dir = self._project_root / "data" / "verification_media"
        media_dir.mkdir(parents=True, exist_ok=True)
        file_info = getattr(message, "file", None)
        extension = str(getattr(file_info, "ext", None) or ".jpg").strip() or ".jpg"
        if not extension.startswith("."):
            extension = f".{extension}"
        target_path = media_dir / f"bot_verification_{message_id}{extension}"
        if target_path.exists():
            return str(target_path)

        try:
            downloaded = await client.download_media(message, file=str(target_path))
        except Exception as exc:
            self._logger.warning("Telegram bot response media download failed for message_id=%s: %s", message_id, exc)
            return ""
        return str(downloaded or target_path) if target_path.exists() or downloaded else ""

    @staticmethod
    def _bot_response_from_message(message: Any, media_path: str = "") -> TelegramBotResponse:
        text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
        button_urls = TelegramService._extract_button_urls(getattr(message, "buttons", None))
        button_urls.extend(TelegramService._extract_button_urls(getattr(message, "reply_markup", None)))
        preview_url = TelegramService._webpage_url_from_message(message)
        if preview_url:
            button_urls.append(preview_url)
        button_texts = TelegramService._extract_button_texts(getattr(message, "buttons", None))
        button_texts.extend(TelegramService._extract_button_texts(getattr(message, "reply_markup", None)))
        text_links = TelegramService._extract_entity_links(str(text), getattr(message, "entities", None))
        return TelegramBotResponse(
            message_id=int(getattr(message, "id", 0) or 0),
            text=str(text),
            button_urls=list(dict.fromkeys(button_urls)),
            button_texts=list(dict.fromkeys(text for text in button_texts if text)),
            text_links=text_links,
            media_path=str(media_path or ""),
        )

    @staticmethod
    def _extract_entity_links(text: str, entities: Any) -> list[TelegramTextLink]:
        links: list[TelegramTextLink] = []
        if not entities:
            return links

        for entity in entities:
            display_text = TelegramService._entity_text(text, entity)
            url = getattr(entity, "url", None)
            if not url and TelegramService._looks_like_url_entity(entity):
                url = display_text
            if url:
                links.append(TelegramTextLink(text=display_text.strip(), url=str(url).strip()))
        return [link for link in links if link.url]

    @staticmethod
    def _entity_text(text: str, entity: Any) -> str:
        offset = int(getattr(entity, "offset", 0) or 0)
        length = int(getattr(entity, "length", 0) or 0)
        if length <= 0:
            return ""
        encoded = text.encode("utf-16-le")
        start = max(0, offset * 2)
        end = max(start, (offset + length) * 2)
        return encoded[start:end].decode("utf-16-le", errors="ignore")

    @staticmethod
    def _looks_like_url_entity(entity: Any) -> bool:
        return "url" in type(entity).__name__.lower()

    @staticmethod
    def _extract_button_urls(source: Any) -> list[str]:
        urls: list[str] = []
        if source is None:
            return urls

        if isinstance(source, (list, tuple)):
            for item in source:
                urls.extend(TelegramService._extract_button_urls(item))
            return urls

        url = getattr(source, "url", None)
        if url:
            urls.append(str(url))

        nested_button = getattr(source, "button", None)
        if nested_button is not None and nested_button is not source:
            urls.extend(TelegramService._extract_button_urls(nested_button))

        for attr_name in ("buttons", "rows"):
            nested = getattr(source, attr_name, None)
            if nested is not None and nested is not source:
                urls.extend(TelegramService._extract_button_urls(nested))

        return urls

    @staticmethod
    def _extract_button_texts(source: Any) -> list[str]:
        texts: list[str] = []
        if source is None:
            return texts

        if isinstance(source, (list, tuple)):
            for item in source:
                texts.extend(TelegramService._extract_button_texts(item))
            return texts

        text = getattr(source, "text", None)
        if text:
            texts.append(str(text).strip())

        nested_button = getattr(source, "button", None)
        if nested_button is not None and nested_button is not source:
            texts.extend(TelegramService._extract_button_texts(nested_button))

        for attr_name in ("buttons", "rows"):
            nested = getattr(source, attr_name, None)
            if nested is not None and nested is not source:
                texts.extend(TelegramService._extract_button_texts(nested))

        return texts

    @staticmethod
    def _find_button_coordinates(buttons: Any, button_text: str) -> Optional[tuple[int, int]]:
        target = str(button_text).strip()
        if not target or not buttons:
            return None

        for row_index, row in enumerate(buttons):
            if isinstance(row, (list, tuple)):
                row_buttons = list(row)
            else:
                row_buttons = list(getattr(row, "buttons", None) or [row])

            for column_index, button in enumerate(row_buttons):
                text = getattr(button, "text", None)
                nested_button = getattr(button, "button", None)
                if not text and nested_button is not None:
                    text = getattr(nested_button, "text", None)
                if str(text or "").strip() == target:
                    return row_index, column_index
        return None

    @staticmethod
    def _bot_response_has_link_candidates(response: TelegramBotResponse) -> bool:
        if response.button_urls:
            return True
        if response.text_links:
            return True
        return bool(re.search(r"https?://|(?:^|\s)t\.me/", response.text, re.IGNORECASE))

    def _session_base_path(self) -> Path:
        telegram_config = self._config.get("telegram", {})
        session_dir = Path(str(telegram_config.get("session_dir", "sessions")))
        if not session_dir.is_absolute():
            session_dir = self._project_root / session_dir
        session_dir.mkdir(parents=True, exist_ok=True)

        session_name = str(telegram_config.get("session_name", "user") or "user")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_name).strip("._") or "user"
        return session_dir / safe_name

    def _telethon_modules(self):
        try:
            from telethon import TelegramClient
            from telethon import functions, utils
            from telethon.errors import FloodWaitError, SessionPasswordNeededError
        except ImportError as exc:
            raise TelegramDependencyError("Telethon 未安装，请先执行 pip install -r requirements.txt") from exc

        return type(
            "TelethonModules",
            (),
            {
                "TelegramClient": TelegramClient,
                "functions": functions,
                "utils": utils,
                "FloodWaitError": FloodWaitError,
                "SessionPasswordNeededError": SessionPasswordNeededError,
            },
        )

    def _run_async(self, coroutine):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        coroutine.close()
        raise TelegramLoginError("TelegramService 不能在正在运行的事件循环中直接调用")

    @staticmethod
    def _parse_target_chat_ids(target_chat_ids: Optional[Sequence[int]]) -> Optional[set[int]]:
        if target_chat_ids is None:
            return None
        parsed_ids: set[int] = set()
        for value in target_chat_ids:
            try:
                parsed_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        return parsed_ids

    @staticmethod
    def _parse_api_id(api_id: str) -> int:
        value = str(api_id).strip()
        if not value:
            raise TelegramLoginError("请输入 api_id")
        try:
            return int(value)
        except ValueError as exc:
            raise TelegramLoginError("api_id 必须是数字") from exc

    @staticmethod
    def _normalize_phone(phone: str) -> str:
        value = str(phone).strip()
        if not value:
            raise TelegramLoginError("请输入手机号")
        return value

    @staticmethod
    def _require_value(value: str, field_name: str) -> str:
        text = str(value).strip()
        if not text:
            raise TelegramLoginError(f"请输入 {field_name}")
        return text

    @staticmethod
    def _phone_suffix(phone: str) -> str:
        digits = re.sub(r"\D+", "", phone)
        if len(digits) <= 4:
            return "****"
        return digits[-4:]
