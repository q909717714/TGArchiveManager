"""Stage 1 service tests."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import unittest
import asyncio
from pathlib import Path
from types import SimpleNamespace

from database.db import DatabaseManager
from database.models import Chat
from database.models import DownloadRecord
from database.models import FileRecord
from database.models import ForwardRecord
from database.models import MessageRecord
from database.models import SearchResult
from database.models import TelegraphImage
from database.models import TelegraphPage
from database.repositories import (
    AccountRepository,
    ChatRepository,
    DownloadRecordRepository,
    FileRepository,
    ForwardRepository,
    GroupRepository,
    MessageRepository,
    PublicSearchRepository,
    TaskRepository,
    TelegraphRepository,
)
from parsers.bot_result_parser import BotResultParser, ParsedBotResult
from parsers.result_normalizer import ResultNormalizer
from parsers.telegram_link_parser import TelegramLinkParser
from providers.base_provider import BaseSearchProvider, SearchProviderVerificationRequired
from providers.jisou_provider import JisouProvider
from providers.telegram_native_provider import TelegramNativeSearchProvider
from services.backup_service import BackupService
from services.cancellation import CancellationToken, OperationCancelled, check_cancelled
from services.config_service import ConfigService
from services.download_service import DownloadService
from services.forward_service import ForwardService, ForwardServiceError
from services.group_service import GroupService
from services.export_service import ExportService
from services.local_search_service import LocalSearchQuery, LocalSearchService
from services.log_service import LogQuery, LogService
from services.public_search_service import PublicSearchService
from services.service_factory import ApplicationContext, ServiceFactory
from services.telegraph_service import TelegraphService
from services.telegram_service import (
    TelegramArchivedMessage,
    TelegramLoginError,
    TelegramMediaDownloadResult,
    TelegramSendResult,
    TelegramService,
)
from ui.backup_page import BackupPage
from utils.error_codes import describe_error_code, is_known_error_code


class Stage1ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = Path(tempfile.mkdtemp(prefix="tg_archive_manager_test_"))
        source_config = Path(__file__).resolve().parents[1] / "config" / "config.yaml.example"
        target_config_dir = self._temp_dir / "config"
        target_config_dir.mkdir(parents=True)
        shutil.copyfile(source_config, target_config_dir / "config.yaml.example")

    def tearDown(self) -> None:
        for logger in self._project_loggers():
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def _project_loggers(self) -> list[logging.Logger]:
        loggers = [logging.getLogger(LogService.LOGGER_NAME)]
        prefix = f"{LogService.LOGGER_NAME}."
        for name, candidate in logging.Logger.manager.loggerDict.items():
            if name.startswith(prefix) and isinstance(candidate, logging.Logger):
                loggers.append(candidate)
        return loggers

    def test_config_loads_and_creates_runtime_directories(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()

        self.assertEqual(config["app"]["name"], "TGArchiveManager")
        self.assertTrue((self._temp_dir / "logs").is_dir())
        self.assertTrue((self._temp_dir / "data").is_dir())
        self.assertTrue((self._temp_dir / "sessions").is_dir())

    def test_config_saves_telegram_api_credentials_to_user_config(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config_service.load()

        config_service.save_telegram_api_credentials(" 123456 ", " abcdef ")

        user_config_path = self._temp_dir / "config" / "config.yaml"
        self.assertTrue(user_config_path.exists())
        reloaded = ConfigService(self._temp_dir)
        reloaded.load()
        self.assertEqual(reloaded.get("telegram.api_id"), "123456")
        self.assertEqual(reloaded.get("telegram.api_hash"), "abcdef")

    def test_config_save_config_persists_general_settings(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        config["download"]["root_dir"] = "custom_downloads"
        config["download"]["max_file_size_mb"] = 12
        config["forward"]["max_per_task"] = 7

        config_service.save_config(config)

        reloaded = ConfigService(self._temp_dir)
        reloaded.load()
        self.assertEqual(reloaded.get("download.root_dir"), "custom_downloads")
        self.assertEqual(reloaded.get("download.max_file_size_mb"), 12)
        self.assertEqual(reloaded.get("forward.max_per_task"), 7)
        self.assertTrue((self._temp_dir / "custom_downloads").is_dir())

    def test_service_factory_builds_common_services(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        factory = ServiceFactory(
            ApplicationContext(
                project_root=self._temp_dir,
                config_service=config_service,
                log_service=log_service,
                database=database,
            )
        )

        telegram_service = factory.telegram_service(chat_repository=ChatRepository(database))
        self.assertEqual(telegram_service.session_file_path, self._temp_dir / "sessions" / "user.session")
        self.assertIsInstance(factory.download_service(telegram_service=telegram_service), DownloadService)
        self.assertIsInstance(
            factory.forward_service(
                search_repository=None,
                telegram_service=telegram_service,
                message_repository=MessageRepository(database),
            ),
            ForwardService,
        )
        self.assertIsInstance(
            factory.bot_public_search_service({"engine_name": "jisou", "username": "@jisou"}),
            PublicSearchService,
        )

    def test_central_error_codes_cover_worker_fallbacks(self) -> None:
        self.assertTrue(is_known_error_code("SE000"))
        self.assertTrue(is_known_error_code("TG000"))
        self.assertIn("skipped", describe_error_code("DL002").lower())

    def test_config_restores_bundled_example_when_packaged_root_is_empty(self) -> None:
        runtime_root = self._temp_dir / "runtime_root"
        runtime_root.mkdir()
        bundled_root = self._temp_dir / "bundled_root"
        bundled_config_dir = bundled_root / "config"
        bundled_config_dir.mkdir(parents=True)
        source_config = self._temp_dir / "config" / "config.yaml.example"
        shutil.copyfile(source_config, bundled_config_dir / "config.yaml.example")

        sentinel = object()
        old_meipass = getattr(sys, "_MEIPASS", sentinel)
        sys._MEIPASS = str(bundled_root)
        try:
            config_service = ConfigService(runtime_root)
            config = config_service.load()
        finally:
            if old_meipass is sentinel:
                delattr(sys, "_MEIPASS")
            else:
                sys._MEIPASS = old_meipass

        self.assertEqual(config["app"]["name"], "TGArchiveManager")
        self.assertTrue((runtime_root / "config" / "config.yaml.example").exists())

    def test_logging_and_database_initialize(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        logger = log_service.configure()
        logger.info("stage1 test")

        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        self.assertTrue(log_service.app_log_path.exists())
        self.assertIn("stage1 test", log_service.app_log_path.read_text(encoding="utf-8"))
        self.assertIn("accounts", set(database.table_names()))
        self.assertIn("public_search_results", set(database.table_names()))

    def test_log_redacts_explicit_sensitive_fields_without_hiding_status_text(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        logger = log_service.configure()

        logger.info("api_hash=SECRET_VALUE")
        logger.info("Telegram login code request visible")

        text = log_service.app_log_path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET_VALUE", text)
        self.assertIn("[redacted sensitive log message]", text)
        self.assertIn("Telegram login code request visible", text)

    def test_log_service_filters_exports_and_extracts_error_detail(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        logger = log_service.get_logger("stage9")

        logger.info("task_id=stage9_ok normal message")
        logger.error("task_id=stage9_fail EX001 export failed")

        entries = log_service.read_entries(
            LogQuery(
                module="stage9",
                level="ERROR",
                task_id="stage9_fail",
                keyword="EX001",
                files=(log_service.app_log_path,),
            )
        )

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].error_code, "EX001")
        self.assertEqual(entries[0].task_id, "stage9_fail")

        exported = log_service.export_entries(entries)
        self.assertTrue(exported.exists())
        self.assertIn("EX001", exported.read_text(encoding="utf-8"))
        detail = log_service.latest_error_detail(entries)
        self.assertIn("stage9_fail", detail)
        self.assertIn("Export service failed", detail)

    def test_log_service_creates_task_specific_log_file(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()

        task_logger = log_service.get_task_logger("backup_test_001", "backup")
        task_logger.warning("task_id=backup_test_001 DL001 task log visible")
        task_path = log_service.task_log_path("backup_test_001", "backup")

        self.assertTrue(task_path.exists())
        entries = log_service.read_entries(LogQuery(files=(task_path,), task_id="backup_test_001", keyword="DL001"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].error_code, "DL001")

    def test_log_service_applies_retention_and_module_file_switches(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        config["logs"]["retention_days"] = 1
        config["logs"]["save_download_log"] = False
        logs_dir = self._temp_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        old_log = logs_dir / "old.log"
        old_log.write_text("old", encoding="utf-8")
        old_timestamp = 1
        os.utime(old_log, (old_timestamp, old_timestamp))

        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        download_logger = log_service.get_file_logger("download", "download.log")
        download_logger.info("download module log switch test")

        self.assertFalse(old_log.exists())
        self.assertFalse((logs_dir / "download.log").exists())
        self.assertIn("download module log switch test", log_service.app_log_path.read_text(encoding="utf-8"))

    def test_account_repository_upserts_latest_account(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = AccountRepository(database)
        repository.upsert_account("+8613800000000", "Alice", "alice", "sessions/user.session")
        repository.upsert_account("+8613800000000", "Alice A", "alice_a", "sessions/user.session")

        account = repository.latest_account()
        self.assertIsNotNone(account)
        self.assertEqual(account.phone, "+8613800000000")
        self.assertEqual(account.display_name, "Alice A")
        self.assertEqual(account.username, "alice_a")

    def test_telegram_service_validates_inputs_before_network_use(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        config["telegram"]["session_name"] = "user:bad/name"
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()
        repository = AccountRepository(database)
        service = TelegramService(self._temp_dir, config, repository, log_service.get_logger("telegram"))

        self.assertEqual(service.session_file_path, self._temp_dir / "sessions" / "user_bad_name.session")
        with self.assertRaises(TelegramLoginError):
            service.send_code("", "hash", "+8613800000000")

    def test_chat_repository_upsert_filter_and_tag_update(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = ChatRepository(database)
        repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=1001,
                title="Archive Group",
                username="archive_group",
                type="group",
                telegram_folder_names="资料频道",
                last_message_id=88,
            )
        )
        repository.update_tag(1001, "work")
        repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=1001,
                title="Archive Group Renamed",
                username="archive_group",
                type="group",
                last_message_id=99,
            )
        )

        all_chats = repository.list_chats()
        filtered = repository.list_chats("work")
        folder_filtered = repository.list_chats("资料频道")

        self.assertEqual(len(all_chats), 1)
        self.assertEqual(all_chats[0].title, "Archive Group Renamed")
        self.assertEqual(all_chats[0].tag, "work")
        self.assertEqual(all_chats[0].telegram_folder_names, "资料频道")
        self.assertEqual(all_chats[0].last_message_id, 99)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(len(folder_filtered), 1)

        repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=1001,
                title="Archive Group Renamed",
                username="archive_group",
                type="group",
                telegram_folder_names="",
                last_message_id=100,
            )
        )
        self.assertEqual(repository.get_by_tg_chat_id(1001).telegram_folder_names, "")

    def test_chat_repository_deletes_negative_chat_id_and_group_registration(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        chat_repository = ChatRepository(database)
        group_repository = GroupRepository(database)
        chat = chat_repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1001,
                title="Archive Channel",
                username="archive",
                type="channel",
                is_created_by_tool=True,
            )
        )
        group_repository.upsert_group(chat, category="demo")

        deleted = chat_repository.delete_chats_by_tg_chat_ids([-1001])

        self.assertEqual(deleted, 1)
        self.assertIsNone(chat_repository.get_by_tg_chat_id(-1001))
        self.assertEqual(group_repository.list_groups(), [])

    def test_telegram_dialog_is_normalized_to_chat_model(self) -> None:
        dialog = SimpleNamespace(
            id=-100123,
            name="Channel Title",
            is_user=False,
            is_group=False,
            is_channel=True,
            entity=SimpleNamespace(username="channel_name"),
            message=SimpleNamespace(id=456),
        )

        chat = TelegramService._chat_from_dialog(dialog)

        self.assertEqual(chat.tg_chat_id, -100123)
        self.assertEqual(chat.title, "Channel Title")
        self.assertEqual(chat.username, "channel_name")
        self.assertEqual(chat.type, "channel")
        self.assertEqual(chat.last_message_id, 456)

    def test_telegram_entity_from_chat_id_falls_back_to_resolved_peer(self) -> None:
        from telethon import types, utils

        channel_id = 123456
        marked_id = utils.get_peer_id(types.PeerChannel(channel_id))

        class _FallbackClient:
            def __init__(self):
                self.calls = []

            async def get_entity(self, value):
                self.calls.append(value)
                if isinstance(value, int):
                    raise ValueError("missing marked id")
                return SimpleNamespace(peer=value)

        client = _FallbackClient()

        entity = asyncio.run(
            TelegramService._entity_from_chat_id(
                client,
                SimpleNamespace(utils=utils),
                marked_id,
            )
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[0], marked_id)
        self.assertIsInstance(client.calls[1], types.PeerChannel)
        self.assertEqual(client.calls[1].channel_id, channel_id)
        self.assertIs(entity.peer, client.calls[1])

    def test_telegram_dialog_filters_are_mapped_to_chat_folder_names(self) -> None:
        from telethon import types, utils

        channel_id = 123456
        dialog = SimpleNamespace(
            id=utils.get_peer_id(types.PeerChannel(channel_id)),
            name="Channel Title",
            is_user=False,
            is_group=False,
            is_channel=True,
            entity=SimpleNamespace(id=channel_id, username="channel_name", megagroup=False),
            message=SimpleNamespace(id=456),
        )
        direct_filter = types.DialogFilter(
            id=2,
            title=types.TextWithEntities("资料频道", []),
            pinned_peers=[],
            include_peers=[types.InputPeerChannel(channel_id, 0)],
            exclude_peers=[],
        )
        broadcast_filter = types.DialogFilter(
            id=3,
            title=types.TextWithEntities("全部频道", []),
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[],
            broadcasts=True,
        )
        excluded_filter = types.DialogFilter(
            id=4,
            title=types.TextWithEntities("排除频道", []),
            pinned_peers=[],
            include_peers=[],
            exclude_peers=[types.InputPeerChannel(channel_id, 0)],
            broadcasts=True,
        )

        names = TelegramService._dialog_filter_names_for_dialog(
            SimpleNamespace(utils=utils),
            dialog,
            [direct_filter, broadcast_filter, excluded_filter],
        )

        self.assertEqual(names, ["资料频道", "全部频道"])
        self.assertEqual(TelegramService._join_folder_names(names), "资料频道，全部频道")

    def test_telegram_bot_response_extracts_nested_button_urls(self) -> None:
        message = SimpleNamespace(
            id=9,
            raw_text="处理中",
            buttons=[[SimpleNamespace(button=SimpleNamespace(text="进入A", url="https://t.me/demo/1"))]],
            reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[SimpleNamespace(text="进入B", url="https://t.me/demo/2")])]),
        )

        response = TelegramService._bot_response_from_message(message)

        self.assertEqual(response.button_urls, ["https://t.me/demo/1", "https://t.me/demo/2"])
        self.assertEqual(response.button_texts, ["进入A", "进入B"])
        self.assertTrue(TelegramService._bot_response_has_link_candidates(response))

    def test_telegram_service_finds_button_coordinates_by_text(self) -> None:
        buttons = [
            [SimpleNamespace(text="3"), SimpleNamespace(text="5")],
            [SimpleNamespace(button=SimpleNamespace(text="8"))],
        ]

        self.assertEqual(TelegramService._find_button_coordinates(buttons, "8"), (1, 0))
        self.assertIsNone(TelegramService._find_button_coordinates(buttons, "13"))

    def test_telegram_bot_response_extracts_text_url_entities(self) -> None:
        message = SimpleNamespace(
            id=10,
            raw_text="龙华结果 文本",
            buttons=[],
            reply_markup=None,
            entities=[SimpleNamespace(offset=0, length=4, url="https://t.me/demo_channel")],
        )

        response = TelegramService._bot_response_from_message(message)

        self.assertEqual(len(response.text_links), 1)
        self.assertEqual(response.text_links[0].text, "龙华结果")
        self.assertEqual(response.text_links[0].url, "https://t.me/demo_channel")
        self.assertTrue(TelegramService._bot_response_has_link_candidates(response))

    def test_telegram_archived_message_collects_hidden_telegraph_urls(self) -> None:
        entity = SimpleNamespace(id=1001, username="source_channel", title="Source", broadcast=True)
        hidden_message = SimpleNamespace(
            id=42,
            raw_text="Open",
            message="Open",
            entities=[SimpleNamespace(offset=0, length=4, url="https://telegra.ph/Hidden-07-03")],
            buttons=[],
            reply_markup=None,
            web_preview=None,
            media=None,
            file=None,
            sender=SimpleNamespace(first_name="Alice"),
            sender_id=101,
            date="2026-07-03T10:00:00",
        )
        preview_message = SimpleNamespace(
            id=43,
            raw_text="Preview",
            message="Preview",
            entities=[],
            buttons=[],
            reply_markup=None,
            web_preview=SimpleNamespace(url="https://telegra.ph/Preview-07-03"),
            media=None,
            file=None,
            sender=SimpleNamespace(first_name="Bob"),
            sender_id=102,
            date="2026-07-03T10:01:00",
        )

        hidden_archived = TelegramService._archived_message_from_message(hidden_message, -1001001, entity)
        preview_archived = TelegramService._archived_message_from_message(preview_message, -1001001, entity)
        hidden_record = BackupService._message_record_from_archived(hidden_archived)

        self.assertEqual(hidden_archived.message_type, "telegraph_page")
        self.assertFalse(hidden_archived.has_media)
        self.assertIn("https://telegra.ph/Hidden-07-03", hidden_archived.external_urls)
        self.assertIn("https://telegra.ph/Hidden-07-03", hidden_record.external_urls)
        self.assertEqual(preview_archived.message_type, "telegraph_page")
        self.assertIn("https://telegra.ph/Preview-07-03", preview_archived.external_urls)

    def test_bot_result_parser_and_normalizer_deduplicate_urls(self) -> None:
        response = SimpleNamespace(
            text="标题 A\nhttps://t.me/demo/123?utm_source=x\n摘要内容",
            button_urls=["https://t.me/demo/123", "https://t.me/other"],
        )

        parsed = BotResultParser().parse_messages([response])
        normalized = ResultNormalizer().normalize("demo", "jisou", parsed, 100)

        self.assertEqual(len(parsed), 3)
        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0].result_type, "message")
        self.assertEqual(normalized[0].tg_username, "demo")
        self.assertEqual(normalized[0].tg_message_id, 123)
        self.assertEqual(normalized[0].normalized_url, "https://t.me/demo/123")

    def test_bot_result_parser_falls_back_to_ranked_text_results(self) -> None:
        response = SimpleNamespace(
            text="🥇龙华 A 结果 🥈龙华 B 结果 🥉龙华 C 结果 🎖龙华 D 结果",
            button_urls=[],
        )

        parsed = BotResultParser().parse_messages([response])
        normalized = ResultNormalizer().normalize("龙华", "jisou", parsed, 100)

        self.assertEqual(len(parsed), 4)
        self.assertEqual(len(normalized), 4)
        self.assertEqual(normalized[0].title, "龙华 A 结果")
        self.assertEqual(normalized[0].url, "")
        self.assertEqual(normalized[0].result_type, "unknown")
        self.assertTrue(normalized[0].normalized_url.startswith("text:jisou:"))
        self.assertEqual(normalized[0].forward_status, "card_only")

    def test_bot_result_parser_pairs_text_links_with_ranked_chunks(self) -> None:
        response = SimpleNamespace(
            text="🥇龙华 A 结果 🥈龙华 B 结果",
            button_urls=[],
            text_links=[
                SimpleNamespace(text="龙华 A 结果", url="https://t.me/a"),
                SimpleNamespace(text="龙华 B 结果", url="https://t.me/b"),
            ],
        )

        parsed = BotResultParser().parse_messages([response])
        normalized = ResultNormalizer().normalize("龙华", "jisou", parsed, 100)

        self.assertEqual(len(parsed), 2)
        self.assertEqual(normalized[0].title, "龙华 A 结果")
        self.assertEqual(normalized[0].url, "https://t.me/a")
        self.assertEqual(normalized[1].url, "https://t.me/b")

    def test_telegram_link_parser_classifies_invite_and_bot_links(self) -> None:
        invite = TelegramLinkParser.parse("t.me/+abcdef")
        bot = TelegramLinkParser.parse("https://t.me/examplebot")

        self.assertEqual(invite.result_type, "invite")
        self.assertEqual(bot.result_type, "bot")

    def test_telegram_link_parser_classifies_telegraph_pages(self) -> None:
        info = TelegramLinkParser.parse("https://telegra.ph/Demo-Page-07-03?utm_source=x")
        bare_urls = TelegramLinkParser.extract_urls("see telegra.ph/Bare-Page-07-03")

        self.assertEqual(info.result_type, "telegraph_page")
        self.assertEqual(info.normalized_url, "https://telegra.ph/Demo-Page-07-03")
        self.assertEqual(bare_urls, ["telegra.ph/Bare-Page-07-03"])
        self.assertEqual(
            TelegramLinkParser.parse(bare_urls[0]).normalized_url,
            "https://telegra.ph/Bare-Page-07-03",
        )

    def test_telegraph_service_parses_page_metadata_images_and_telegram_links(self) -> None:
        logger = logging.getLogger("test.telegraph_parser")
        html = """
        <html>
          <head><title>Fallback</title></head>
          <body>
            <article>
              <h1>Telegraph Demo</h1>
              <address><a href="https://t.me/source">Source</a></address>
              <time datetime="2026-07-03T09:30:00+00:00">July 3, 2026</time>
              <p><img src="/file/a.jpg"></p>
              <p><img src="https://telegra.ph/file/b.png"></p>
              <p>Jump <a href="https://t.me/channel/123">Telegram message</a></p>
              <p>Plain https://t.me/group_link</p>
            </article>
          </body>
        </html>
        """

        parsed = TelegraphService(logger).parse_html("https://telegra.ph/Demo-Page-07-03", html)

        self.assertEqual(parsed.page.title, "Telegraph Demo")
        self.assertEqual(parsed.page.published_at, "2026-07-03T09:30:00+00:00")
        self.assertEqual(parsed.page.author_name, "Source")
        self.assertEqual(parsed.page.author_url, "https://t.me/source")
        self.assertEqual(parsed.page.image_count, 2)
        self.assertEqual([image.url for image in parsed.images], [
            "https://telegra.ph/file/a.jpg",
            "https://telegra.ph/file/b.png",
        ])
        self.assertEqual(parsed.page.telegram_link_count, 2)
        self.assertEqual(parsed.telegram_links[0].normalized_url, "https://t.me/channel/123")

    def test_public_search_repository_and_service_save_results(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        logger = log_service.get_file_logger("public_search", "public_search.log")
        provider = _FakeProvider()
        service = PublicSearchService(repository, {"jisou": provider}, logger, str(log_service.logs_dir / "public_search.log"))

        report = service.search("123", "hash", "jisou", "demo", 100)

        self.assertEqual(report.total_found, 2)
        self.assertEqual(report.total_saved, 2)
        self.assertEqual(len(repository.list_results_for_task(report.task_id)), 2)
        self.assertTrue((self._temp_dir / "logs" / "public_search.log").exists())

    def test_public_search_service_saves_parsed_telegraph_page_details(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        telegraph_repository = TelegraphRepository(database)
        logger = log_service.get_file_logger("public_search", "public_search.log")
        html = """
        <article>
          <h1>Saved Telegraph</h1>
          <time datetime="2026-07-03T10:00:00+00:00"></time>
          <img src="/file/one.jpg">
          <a href="https://t.me/demo/1">Demo</a>
        </article>
        """
        service = PublicSearchService(
            repository,
            {"jisou": _TelegraphProvider()},
            logger,
            str(log_service.logs_dir / "public_search.log"),
            telegraph_service=TelegraphService(logger, fetcher=lambda _url, _timeout: html),
            telegraph_repository=telegraph_repository,
        )

        report = service.search("123", "hash", "jisou", "demo", 100)

        self.assertEqual(len(report.results), 1)
        result = report.results[0]
        self.assertEqual(result.result_type, "telegraph_page")
        self.assertEqual(result.title, "Saved Telegraph")
        self.assertIn("图片数量：1", result.summary)
        page = telegraph_repository.get_page_by_search_result_id(int(result.id))
        self.assertIsNotNone(page)
        self.assertEqual(page.image_count, 1)
        images = telegraph_repository.list_images_for_search_result(int(result.id))
        self.assertEqual(images[0].url, "https://telegra.ph/file/one.jpg")

    def test_public_search_repository_filters_results_for_advanced_forwarding(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        service = PublicSearchService(
            repository,
            {"jisou": _FakeProvider()},
            log_service.get_file_logger("public_search", "public_search.log"),
            str(log_service.logs_dir / "public_search.log"),
        )
        report = service.search("123", "hash", "jisou", "demo", 100)

        filtered = repository.list_filtered_results(
            task_id=report.task_id,
            keyword="Title 1",
            result_type="message",
            limit=100,
        )

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].title, "Title 1")
        self.assertEqual(repository.distinct_result_types(report.task_id), ["message"])

    def test_public_search_repository_deletes_results_tasks_and_telegraph_details(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        telegraph_repository = TelegraphRepository(database)
        task_id = repository.create_task("demo", "jisou", 100, "")
        results = repository.save_results(
            task_id,
            [
                SearchResult(
                    id=None,
                    task_id=None,
                    engine_name="jisou",
                    rank_no=1,
                    keyword="demo",
                    result_type="telegraph_page",
                    title="Telegraph",
                    summary="",
                    url="https://telegra.ph/Demo-07-03",
                    normalized_url="https://telegra.ph/Demo-07-03",
                ),
                SearchResult(
                    id=None,
                    task_id=None,
                    engine_name="jisou",
                    rank_no=2,
                    keyword="demo",
                    result_type="message",
                    title="Message",
                    summary="",
                    url="https://t.me/demo/2",
                    normalized_url="https://t.me/demo/2",
                ),
            ],
        )
        telegraph_repository.upsert_page_for_search_result(
            int(results[0].id),
            TelegraphPage(
                id=None,
                search_result_id=int(results[0].id),
                message_db_id=None,
                url=results[0].url,
                normalized_url=results[0].normalized_url,
                title="Telegraph",
                image_count=1,
            ),
            [
                TelegraphImage(
                    id=None,
                    page_id=None,
                    position=1,
                    url="https://telegra.ph/file/one.jpg",
                    normalized_url="https://telegra.ph/file/one.jpg",
                )
            ],
            [],
        )

        self.assertEqual(repository.delete_results_by_ids([int(results[0].id)]), 1)
        self.assertEqual(repository.get_results_by_ids([int(results[0].id)]), [])
        self.assertEqual(len(repository.list_results_for_task(task_id)), 1)
        self.assertIsNone(telegraph_repository.get_page_by_search_result_id(int(results[0].id)))

        self.assertEqual(repository.delete_tasks_by_ids([task_id]), 1)
        self.assertEqual(repository.list_results_for_task(task_id), [])
        self.assertEqual(repository.latest_tasks(), [])

    def test_public_search_repository_can_disable_duplicate_check(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        first_task = repository.create_task("demo", "jisou", 10, "public_search.log")
        second_task = repository.create_task("demo", "jisou", 10, "public_search.log")
        result = SearchResult(
            id=None,
            task_id=None,
            engine_name="jisou",
            rank_no=1,
            keyword="demo",
            result_type="message",
            title="Same",
            summary="",
            url="https://t.me/same/1",
            normalized_url="https://t.me/same/1",
        )

        repository.save_results(first_task, [result])
        saved = repository.save_results(second_task, [result], duplicate_check=False)

        self.assertFalse(saved[0].is_duplicate)

    def test_public_search_service_marks_cancelled_task(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        service = PublicSearchService(
            repository,
            {"jisou": _CancellingProvider()},
            log_service.get_file_logger("public_search", "public_search.log"),
            str(log_service.logs_dir / "public_search.log"),
        )
        token = CancellationToken()

        with self.assertRaises(OperationCancelled):
            service.search("123", "hash", "jisou", "demo", 100, cancel_token=token)

        tasks = repository.latest_tasks(limit=1)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].status, "cancelled")

    def test_forward_service_formats_card_and_saves_records(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        search_repository = PublicSearchRepository(database)
        public_search_service = PublicSearchService(
            search_repository,
            {"jisou": _FakeProvider()},
            log_service.get_file_logger("public_search", "public_search.log"),
            str(log_service.logs_dir / "public_search.log"),
        )
        search_report = public_search_service.search("123", "hash", "jisou", "demo", 100)
        result_ids = [result.id for result in search_report.results if result.id is not None]

        forward_service = ForwardService(
            search_repository=search_repository,
            forward_repository=ForwardRepository(database),
            task_repository=TaskRepository(database),
            telegram_service=_ForwardTelegramService(),
            logger=log_service.get_file_logger("forward", "forward.log"),
            log_file=str(log_service.logs_dir / "forward.log"),
        )
        progress_events = []

        report = forward_service.forward_search_result_cards(
            api_id="123",
            api_hash="hash",
            result_ids=result_ids,
            target_chat_id=-1001001,
            interval_seconds=0,
            skip_duplicates=False,
            progress_callback=progress_events.append,
        )

        self.assertEqual(report.total_count, 2)
        self.assertEqual(report.success_count, 2)
        self.assertEqual(report.failed_count, 0)
        self.assertEqual(len(progress_events), 2)
        records = ForwardRepository(database).list_records_for_task(report.task_id)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].status, "success")
        self.assertEqual(records[0].target_chat_id, -1001001)
        forwarded_results = search_repository.get_results_by_ids(result_ids)
        self.assertEqual([result.forward_status for result in forwarded_results], ["success", "success"])
        tasks = TaskRepository(database).latest_tasks("forward", limit=1)
        self.assertEqual(tasks[0].status, "completed")

    def test_forward_service_enforces_max_per_task(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        messages = [
            MessageRecord(
                id=1,
                tg_chat_id=-1001,
                message_id=1,
                sender_id=101,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="one",
                text_preview="one",
                message_type="text",
            ),
            MessageRecord(
                id=2,
                tg_chat_id=-1001,
                message_id=2,
                sender_id=102,
                sender_name="Bob",
                date="2026-07-01T10:01:00",
                text="two",
                text_preview="two",
                message_type="text",
            ),
        ]
        forward_service = ForwardService(
            search_repository=None,
            forward_repository=ForwardRepository(database),
            task_repository=TaskRepository(database),
            telegram_service=_ForwardTelegramService(),
            logger=log_service.get_file_logger("forward", "forward.log"),
            log_file=str(log_service.logs_dir / "forward.log"),
            message_repository=MessageRepository(database),
            max_per_task=1,
        )

        with self.assertRaises(ForwardServiceError) as context:
            forward_service.forward_message_records("123", "hash", messages, -1001001)

        self.assertEqual(context.exception.error_code, "FW002")
        self.assertIn("单次最多转发 1 条", str(context.exception))

    def test_forward_service_marks_task_cancelled_after_partial_progress(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        search_repository = PublicSearchRepository(database)
        public_search_service = PublicSearchService(
            search_repository,
            {"jisou": _FakeProvider()},
            log_service.get_file_logger("public_search", "public_search.log"),
            str(log_service.logs_dir / "public_search.log"),
        )
        search_report = public_search_service.search("123", "hash", "jisou", "demo", 100)
        result_ids = [result.id for result in search_report.results if result.id is not None]

        forward_service = ForwardService(
            search_repository=search_repository,
            forward_repository=ForwardRepository(database),
            task_repository=TaskRepository(database),
            telegram_service=_ForwardTelegramService(),
            logger=log_service.get_file_logger("forward", "forward.log"),
            log_file=str(log_service.logs_dir / "forward.log"),
        )
        token = CancellationToken()
        progress_events = []

        def on_progress(progress) -> None:
            progress_events.append(progress)
            if progress.done_count == 1:
                token.cancel()

        with self.assertRaises(OperationCancelled):
            forward_service.forward_search_result_cards(
                api_id="123",
                api_hash="hash",
                result_ids=result_ids,
                target_chat_id=-1001001,
                interval_seconds=0,
                skip_duplicates=False,
                progress_callback=on_progress,
                cancel_token=token,
            )

        tasks = TaskRepository(database).latest_tasks("forward", limit=1)
        self.assertEqual(tasks[0].status, "cancelled")
        self.assertEqual(len(progress_events), 1)
        records = ForwardRepository(database).list_records_for_task(tasks[0].task_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "success")

    def test_forward_service_previews_and_auto_groups_by_category(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        search_repository = PublicSearchRepository(database)
        public_search_service = PublicSearchService(
            search_repository,
            {"jisou": _FakeProvider()},
            log_service.get_file_logger("public_search", "public_search.log"),
            str(log_service.logs_dir / "public_search.log"),
        )
        search_report = public_search_service.search("123", "hash", "jisou", "demo", 100)
        result_ids = [result.id for result in search_report.results if result.id is not None]
        auto_group_service = _AutoGroupService()
        forward_service = ForwardService(
            search_repository=search_repository,
            forward_repository=ForwardRepository(database),
            task_repository=TaskRepository(database),
            telegram_service=_ForwardTelegramService(),
            logger=log_service.get_file_logger("forward", "forward.log"),
            log_file=str(log_service.logs_dir / "forward.log"),
            group_service=auto_group_service,
        )

        preview = forward_service.preview_search_result_cards(result_ids, max_cards=1)
        report = forward_service.forward_search_result_cards_auto_group(
            api_id="123",
            api_hash="hash",
            result_ids=result_ids,
            group_by="category",
            group_title_prefix="Auto",
            interval_seconds=0,
            skip_duplicates=False,
        )

        self.assertIn("Title 1", preview)
        self.assertEqual(report.total_count, 2)
        self.assertEqual(report.success_count, 2)
        self.assertEqual(len(report.target_group_titles), 1)
        self.assertTrue(report.target_group_titles[0].startswith("Auto_message_"))
        self.assertEqual(auto_group_service.categories, ["message"])
        records = ForwardRepository(database).list_records_for_task(report.task_id)
        self.assertEqual({record.target_chat_id for record in records}, {-1002001})

    def test_forward_card_format_includes_title_and_link(self) -> None:
        result = SearchResult(
            id=1,
            task_id=1,
            engine_name="jisou",
            rank_no=7,
            keyword="demo",
            result_type="channel",
            title="Demo Title",
            summary="Demo Summary",
            url="https://t.me/demo",
            normalized_url="https://t.me/demo",
        )

        card = ForwardService.format_card(result)

        self.assertIn("TGArchiveManager 搜索结果卡片", card)
        self.assertIn("标题：Demo Title", card)
        self.assertIn("链接：https://t.me/demo", card)

    def test_forward_card_format_includes_media_info(self) -> None:
        media_result = SearchResult(
            id=1,
            task_id=1,
            engine_name="telegram_native",
            rank_no=1,
            keyword="demo",
            result_type="photo",
            title="Photo",
            summary="媒体：photo；大小：1.5 KB",
            url="https://t.me/c/1001/2",
            normalized_url="telegram-native:-1001:2",
        )
        telegraph_result = SearchResult(
            id=2,
            task_id=1,
            engine_name="telegram_native",
            rank_no=2,
            keyword="demo",
            result_type="telegraph_page",
            title="Telegraph",
            summary="图片数量：3；Telegram 跳转链接：0",
            url="https://telegra.ph/demo",
            normalized_url="https://telegra.ph/demo",
        )

        self.assertIn("媒体信息：文件大小：1.5 KB", ForwardService.format_card(media_result))
        self.assertIn("媒体信息：Telegraph 图片：3 张", ForwardService.format_card(telegraph_result))

    def test_forward_service_forwards_message_records_and_marks_forwarded(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        message_repository = MessageRepository(database)
        message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=1,
                sender_id=101,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="hello",
                text_preview="hello",
                message_type="text",
            )
        )
        message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=2,
                sender_id=102,
                sender_name="Bob",
                date="2026-07-01T10:05:00",
                text="photo caption",
                text_preview="photo caption",
                message_type="media",
                has_media=True,
                media_type="photo",
                file_name="demo.jpg",
                file_size=2048,
                source_link="https://t.me/c/1001/2",
            )
        )
        messages = message_repository.list_messages(-1001, limit=10)
        forward_service = ForwardService(
            search_repository=None,
            forward_repository=ForwardRepository(database),
            task_repository=TaskRepository(database),
            telegram_service=_ForwardTelegramService(),
            logger=log_service.get_file_logger("forward", "forward.log"),
            log_file=str(log_service.logs_dir / "forward.log"),
            message_repository=message_repository,
        )

        preview = forward_service.preview_message_records(messages)
        report = forward_service.forward_message_records(
            api_id="123",
            api_hash="hash",
            messages=messages,
            target_chat_id=-1001001,
            interval_seconds=0,
        )

        self.assertIn("TGArchiveManager 聊天记录", preview)
        self.assertIn("媒体：photo，文件=demo.jpg，大小=2.0 KB，已下载=否", preview)
        self.assertIn("下载链接：tgarchive://download?chat_id=-1001&message_id=2", preview)
        self.assertIn("Telegram 原文：https://t.me/c/1001/2", preview)
        self.assertEqual(report.total_count, 2)
        self.assertEqual(report.success_count, 2)
        records = ForwardRepository(database).list_records_for_task(report.task_id)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].source_type, "message_record")
        self.assertEqual(records[0].source_chat_id, -1001)
        self.assertEqual(records[0].source_message_id, 1)
        self.assertEqual(records[0].forward_mode, "message_text")
        self.assertTrue(message_repository.get_by_chat_and_message_id(-1001, 1).is_forwarded)
        self.assertTrue(message_repository.get_by_chat_and_message_id(-1001, 2).is_forwarded)

    def test_forward_service_auto_group_for_message_records(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        message_repository = MessageRepository(database)
        message = message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1002,
                message_id=8,
                sender_id=101,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="auto group",
                text_preview="auto group",
                message_type="text",
            )
        )
        auto_group_service = _AutoGroupService()
        forward_service = ForwardService(
            search_repository=None,
            forward_repository=ForwardRepository(database),
            task_repository=TaskRepository(database),
            telegram_service=_ForwardTelegramService(),
            logger=log_service.get_file_logger("forward", "forward.log"),
            log_file=str(log_service.logs_dir / "forward.log"),
            group_service=auto_group_service,
            message_repository=message_repository,
        )

        report = forward_service.forward_message_records_auto_group(
            api_id="123",
            api_hash="hash",
            messages=[message],
            group_title="聊天记录归档",
            interval_seconds=0,
        )

        self.assertEqual(report.success_count, 1)
        self.assertEqual(report.target_group_titles, ("聊天记录归档",))
        self.assertEqual(auto_group_service.categories, ["chat_history"])
        records = ForwardRepository(database).list_records_for_task(report.task_id)
        self.assertEqual(records[0].target_chat_id, -1002001)

    def test_group_service_creates_and_registers_target_group(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        chat_repository = ChatRepository(database)
        group_repository = GroupRepository(database)
        service = GroupService(
            telegram_service=_GroupTelegramService(),
            chat_repository=chat_repository,
            group_repository=group_repository,
            logger=log_service.get_logger("group_service"),
        )

        chat = service.create_target_group("123", "hash", "Archive Target", "demo")

        self.assertEqual(chat.title, "Archive Target")
        self.assertEqual(chat_repository.get_by_tg_chat_id(chat.tg_chat_id).title, "Archive Target")
        groups = group_repository.list_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].tag, "demo")

    def test_message_repository_upserts_and_lists_messages(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = MessageRepository(database)
        repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=10,
                sender_id=1,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="hello",
                text_preview="hello",
                message_type="text",
            )
        )
        repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=10,
                sender_id=1,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="hello updated",
                text_preview="hello updated",
                message_type="text",
            )
        )

        messages = repository.list_messages(-1001)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].text, "hello updated")

    def test_message_repository_deletes_message_and_related_local_metadata(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_repository = DownloadRecordRepository(database)
        forward_repository = ForwardRepository(database)
        telegraph_repository = TelegraphRepository(database)
        message = message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=10,
                sender_id=1,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="hello https://telegra.ph/Demo-07-03",
                text_preview="hello",
                message_type="text",
                has_media=True,
                media_type="photo",
            )
        )
        file_record = file_repository.upsert_file_for_message(
            FileRecord(
                id=None,
                message_db_id=message.id,
                tg_chat_id=-1001,
                message_id=10,
                file_name="photo.jpg",
                file_ext=".jpg",
                file_size=1024,
                local_path="downloads/photo.jpg",
            )
        )
        download_repository.create_record(
            DownloadRecord(
                id=None,
                task_id="download_test",
                message_db_id=message.id,
                file_id=file_record.id,
                status="success",
                local_path="downloads/photo.jpg",
            )
        )
        forward_repository.create_record(
            ForwardRecord(
                id=None,
                task_id="forward_test",
                source_type="message_record",
                source_id=message.id,
                source_chat_id=-1001,
                source_message_id=10,
                target_chat_id=-2002,
                target_message_id=20,
                forward_mode="message_text",
                status="success",
            )
        )
        telegraph_repository.upsert_page_for_message(
            int(message.id),
            TelegraphPage(
                id=None,
                search_result_id=None,
                message_db_id=int(message.id),
                url="https://telegra.ph/Demo-07-03",
                normalized_url="https://telegra.ph/Demo-07-03",
                title="Demo",
                image_count=1,
            ),
            [
                TelegraphImage(
                    id=None,
                    page_id=None,
                    position=1,
                    url="https://telegra.ph/file/one.jpg",
                    normalized_url="https://telegra.ph/file/one.jpg",
                )
            ],
            [],
        )

        deleted = message_repository.delete_messages_by_keys([(-1001, 10)])

        self.assertEqual(deleted, 1)
        self.assertEqual(message_repository.list_messages(-1001), [])
        self.assertIsNone(file_repository.get_by_id(int(file_record.id)))
        self.assertEqual(download_repository.list_records_for_task("download_test"), [])
        self.assertEqual(forward_repository.list_records_for_task("forward_test"), [])
        self.assertIsNone(
            telegraph_repository.get_page_by_message_id(int(message.id), "https://telegra.ph/Demo-07-03")
        )

    def test_local_search_service_filters_messages(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()
        repository = MessageRepository(database)
        repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=1,
                sender_id=1,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="alpha report",
                text_preview="alpha report",
                message_type="text",
            )
        )
        repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=2,
                sender_id=2,
                sender_name="Bob",
                date="2026-07-01T11:00:00",
                text="photo report",
                text_preview="photo report",
                message_type="photo",
                has_media=True,
                file_name="photo.jpg",
                is_downloaded=True,
            )
        )
        service = LocalSearchService(repository, log_service.get_logger("local_search"))

        results = service.search(
            LocalSearchQuery(
                keyword="report",
                tg_chat_id=-1001,
                date_from="2026-07-01T00:00:00",
                date_to="2026-07-01T23:59:59",
                media_filter="downloaded",
                limit=100,
            )
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].message_id, 2)
        self.assertEqual(repository.distinct_message_types(), ["photo", "text"])

    def test_export_service_writes_csv_excel_json_and_html(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()
        repository = MessageRepository(database)
        repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=1,
                sender_id=1,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="export me",
                text_preview="export me",
                message_type="text",
                source_link="https://t.me/demo/1",
            )
        )
        search_service = LocalSearchService(repository, log_service.get_logger("local_search"))
        export_service = ExportService(
            message_repository=repository,
            search_service=search_service,
            export_root=self._temp_dir / "exports",
            logger=log_service.get_logger("export"),
        )

        report = export_service.export_messages(
            query=LocalSearchQuery(keyword="export", limit=100),
            formats=["csv", "xlsx", "json", "html"],
            base_name="stage8",
        )

        self.assertEqual(report.total_count, 1)
        self.assertEqual(len(report.files), 4)
        for path_text in report.files:
            self.assertTrue(Path(path_text).exists())
        self.assertTrue(any(path.endswith(".csv") for path in report.files))
        self.assertTrue(any(path.endswith(".xlsx") for path in report.files))
        self.assertTrue(any(path.endswith(".json") for path in report.files))
        self.assertTrue(any(path.endswith(".html") for path in report.files))

    def test_backup_service_saves_messages_and_downloads_media(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        chat_repository = ChatRepository(database)
        chat_repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1001,
                title="Backup Chat",
                username="backup_chat",
                type="group",
            )
        )
        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_record_repository = DownloadRecordRepository(database)
        download_service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_record_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        backup_service = BackupService(
            telegram_service=telegram_service,
            chat_repository=chat_repository,
            message_repository=message_repository,
            file_repository=file_repository,
            task_repository=TaskRepository(database),
            download_service=download_service,
            logger=log_service.get_file_logger("download", "download.log"),
            log_file=str(log_service.logs_dir / "download.log"),
        )
        progress_events = []

        report = backup_service.backup_chat(
            api_id="123",
            api_hash="hash",
            tg_chat_id=-1001,
            limit=100,
            incremental=True,
            download_media=True,
            retry_count=2,
            progress_callback=progress_events.append,
        )

        self.assertEqual(report.total_count, 3)
        self.assertEqual(report.saved_count, 3)
        self.assertEqual(report.downloaded_count, 2)
        self.assertEqual(report.skipped_count, 0)
        self.assertEqual(len(progress_events), 3)
        self.assertEqual(len(message_repository.list_messages(-1001)), 3)
        downloaded_message = message_repository.get_by_chat_and_message_id(-1001, 2)
        self.assertTrue(downloaded_message.is_downloaded)
        self.assertEqual(file_repository.get_by_message(-1001, 2).download_status, "success")
        self.assertEqual(file_repository.get_by_message(-1001, 3).download_status, "success")
        self.assertEqual(len(download_record_repository.list_records_for_task(report.task_id)), 2)
        self.assertEqual(chat_repository.get_by_tg_chat_id(-1001).last_backup_message_id, 3)
        self.assertEqual(telegram_service.download_calls, [2, 3])

    def test_backup_service_ignores_incremental_cursor_when_local_cache_is_incomplete(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        chat_repository = ChatRepository(database)
        chat_repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1001,
                title="Backup Chat",
                username="backup_chat",
                type="group",
                last_backup_message_id=2,
            )
        )
        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=3,
                sender_id=103,
                sender_name="Carol",
                date="2026-07-01T10:02:00",
                text="existing",
                text_preview="existing",
                message_type="text",
            )
        )
        download_record_repository = DownloadRecordRepository(database)
        download_service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=FileRepository(database),
            download_record_repository=download_record_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        backup_service = BackupService(
            telegram_service=telegram_service,
            chat_repository=chat_repository,
            message_repository=message_repository,
            file_repository=FileRepository(database),
            task_repository=TaskRepository(database),
            download_service=download_service,
            logger=log_service.get_file_logger("download", "download.log"),
            log_file=str(log_service.logs_dir / "download.log"),
        )

        report = backup_service.backup_chat(
            api_id="123",
            api_hash="hash",
            tg_chat_id=-1001,
            limit=3,
            incremental=True,
            download_media=False,
        )

        self.assertEqual(report.total_count, 3)
        self.assertEqual(message_repository.count_messages(-1001), 3)
        self.assertEqual(
            [message.message_id for message in message_repository.list_messages(-1001, limit=10)],
            [3, 2, 1],
        )

    def test_backup_service_filters_selected_message_ids(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        chat_repository = ChatRepository(database)
        chat_repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1001,
                title="Backup Chat",
                username="backup_chat",
                type="group",
            )
        )
        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_record_repository = DownloadRecordRepository(database)
        download_service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_record_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        backup_service = BackupService(
            telegram_service=telegram_service,
            chat_repository=chat_repository,
            message_repository=message_repository,
            file_repository=file_repository,
            task_repository=TaskRepository(database),
            download_service=download_service,
            logger=log_service.get_file_logger("download", "download.log"),
            log_file=str(log_service.logs_dir / "download.log"),
        )

        report = backup_service.backup_chat(
            api_id="123",
            api_hash="hash",
            tg_chat_id=-1001,
            limit=100,
            incremental=False,
            download_media=True,
            retry_count=2,
            selected_message_ids=[2],
        )

        self.assertEqual(report.total_count, 1)
        self.assertEqual(report.saved_count, 1)
        self.assertEqual(report.downloaded_count, 1)
        self.assertEqual(len(message_repository.list_messages(-1001)), 1)
        self.assertIsNone(message_repository.get_by_chat_and_message_id(-1001, 1))
        self.assertIsNotNone(message_repository.get_by_chat_and_message_id(-1001, 2))
        self.assertEqual(chat_repository.get_by_tg_chat_id(-1001).last_backup_message_id, 2)
        self.assertEqual(telegram_service.download_calls, [2])

    def test_jisou_provider_reports_human_verification(self) -> None:
        test_logger = logging.getLogger("test_jisou_provider")
        test_logger.handlers.clear()
        test_logger.addHandler(logging.NullHandler())
        test_logger.propagate = False
        provider = JisouProvider(
            telegram_service=_VerificationTelegramService(),
            bot_username="@jisou",
            parser=BotResultParser(),
            normalizer=ResultNormalizer(),
            logger=test_logger,
        )

        with self.assertRaises(SearchProviderVerificationRequired) as context:
            provider.search("123", "hash", "龙华", 100)

        self.assertEqual(context.exception.error_code, "SE005")
        self.assertEqual(context.exception.bot_username, "@jisou")
        self.assertEqual(context.exception.message_id, 1)
        self.assertEqual(context.exception.options, ["3", "5", "8"])
        self.assertEqual(context.exception.media_path, "data/verification_media/bot_verification_1.jpg")

    def test_public_search_service_returns_verification_context_and_resumes_task(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        repository = PublicSearchRepository(database)
        service = PublicSearchService(
            repository,
            {"jisou": _VerificationThenResultsProvider()},
            log_service.get_file_logger("public_search", "public_search.log"),
            str(log_service.logs_dir / "public_search.log"),
        )

        with self.assertRaises(SearchProviderVerificationRequired) as context:
            service.search("123", "hash", "jisou", "龙华", 100)

        verification = context.exception
        self.assertGreater(verification.task_id, 0)
        self.assertEqual(verification.keyword, "龙华")
        self.assertEqual(verification.engine_name, "jisou")
        self.assertEqual(verification.max_results, 100)
        self.assertEqual(repository.latest_tasks(limit=1)[0].status, "verification_required")

        report = service.submit_verification(
            api_id="123",
            api_hash="hash",
            engine_name=verification.engine_name,
            keyword=verification.keyword,
            max_results=verification.max_results,
            task_id=verification.task_id,
            message_id=verification.message_id,
            button_text="8",
        )

        self.assertEqual(report.task_id, verification.task_id)
        self.assertEqual(report.total_saved, 1)
        self.assertEqual(repository.latest_tasks(limit=1)[0].status, "completed")
        self.assertEqual(repository.list_results_for_task(report.task_id)[0].title, "验证后结果")

    def test_jisou_provider_submit_verification_parses_follow_up_results(self) -> None:
        test_logger = logging.getLogger("test_jisou_provider_submit")
        test_logger.handlers.clear()
        test_logger.addHandler(logging.NullHandler())
        test_logger.propagate = False
        provider = JisouProvider(
            telegram_service=_VerificationSuccessTelegramService(),
            bot_username="@jisou",
            parser=BotResultParser(),
            normalizer=ResultNormalizer(),
            logger=test_logger,
        )

        results = provider.submit_verification("123", "hash", "龙华", 100, 1, "8")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "验证后结果")
        self.assertEqual(results[0].url, "https://t.me/verified/1")

    def test_jisou_provider_collects_paginated_results_until_max_results(self) -> None:
        test_logger = logging.getLogger("test_jisou_provider_pagination")
        test_logger.handlers.clear()
        test_logger.addHandler(logging.NullHandler())
        test_logger.propagate = False
        telegram_service = _PaginatedTelegramService()
        provider = JisouProvider(
            telegram_service=telegram_service,
            bot_username="@jisou",
            parser=BotResultParser(),
            normalizer=ResultNormalizer(),
            logger=test_logger,
        )

        results = provider.search("123", "hash", "龙华", 3)

        self.assertEqual(len(results), 3)
        self.assertEqual([result.url for result in results], [
            "https://t.me/page1/1",
            "https://t.me/page1/2",
            "https://t.me/page2/3",
        ])
        self.assertEqual(telegram_service.clicked_buttons, ["下一页"])

    def test_telegram_native_provider_maps_joined_message_results(self) -> None:
        provider = TelegramNativeSearchProvider(
            telegram_service=_NativeSearchTelegramService(),
            logger=logging.getLogger("native_provider_test"),
        )

        results = provider.search("123", "hash", "demo", 10)

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].engine_name, "telegram_native")
        self.assertEqual(results[0].tg_chat_id, -1001001)
        self.assertEqual(results[0].tg_message_id, 10)
        self.assertEqual(results[0].normalized_url, "telegram-native:-1001001:10")
        self.assertTrue(results[0].can_forward)
        self.assertTrue(results[1].can_forward)
        self.assertEqual(results[1].forward_status, "pending")
        self.assertIn("大小：1.5 KB", results[1].summary)

    def test_telegram_native_provider_limits_results_to_selected_chats(self) -> None:
        telegram_service = _NativeSearchTelegramService()
        provider = TelegramNativeSearchProvider(
            telegram_service=telegram_service,
            logger=logging.getLogger("native_provider_scope_test"),
            target_chat_ids=[-1001002],
        )

        results = provider.search("123", "hash", "demo", 10)

        self.assertEqual(telegram_service.last_target_chat_ids, [-1001002])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tg_chat_id, -1001002)
        self.assertIn("Media Group", results[0].title)

    def test_download_service_downloads_search_result_media(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        download_repository = DownloadRecordRepository(database)
        service = DownloadService(
            telegram_service=_BackupTelegramService(self._temp_dir),
            message_repository=MessageRepository(database),
            file_repository=FileRepository(database),
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        results = [
            SearchResult(
                id=1,
                task_id=1,
                engine_name="telegram_native",
                rank_no=1,
                keyword="demo",
                result_type="photo",
                title="Photo",
                summary="",
                url="",
                normalized_url="telegram-native:-1001001:2",
                tg_chat_id=-1001001,
                tg_message_id=2,
                is_protected=False,
                can_forward=True,
            ),
            SearchResult(
                id=2,
                task_id=1,
                engine_name="telegram_native",
                rank_no=2,
                keyword="demo",
                result_type="document",
                title="Document",
                summary="",
                url="",
                normalized_url="telegram-native:-1001001:3",
                tg_chat_id=-1001001,
                tg_message_id=3,
                is_protected=False,
                can_forward=True,
            ),
        ]
        progress_events = []

        report = service.download_search_results_media(
            api_id="123",
            api_hash="hash",
            results=results,
            retry_count=1,
            progress_callback=progress_events.append,
        )

        self.assertEqual(report.success_count, 2)
        self.assertEqual(report.skipped_count, 0)
        self.assertEqual(len(progress_events), 4)
        self.assertEqual([event.status for event in progress_events], ["downloading", "success", "downloading", "success"])
        self.assertGreater(progress_events[-1].downloaded_bytes, 0)
        self.assertEqual(progress_events[-1].image_done_count, 1)
        self.assertEqual(progress_events[-1].image_total_count, 1)
        records = download_repository.list_records_for_task(report.task_id)
        self.assertEqual([record.status for record in records], ["success", "success"])

    def test_download_service_downloads_limited_telegraph_page_images(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        search_repository = PublicSearchRepository(database)
        telegraph_repository = TelegraphRepository(database)
        task_id = search_repository.create_task("demo", "jisou", 100, "")
        result = search_repository.save_results(
            task_id,
            [
                SearchResult(
                    id=None,
                    task_id=None,
                    engine_name="jisou",
                    rank_no=1,
                    keyword="demo",
                    result_type="telegraph_page",
                    title="Telegraph Saved",
                    summary="",
                    url="https://telegra.ph/Demo-Page-07-03",
                    normalized_url="https://telegra.ph/Demo-Page-07-03",
                    can_forward=True,
                )
            ],
        )[0]
        telegraph_repository.upsert_page_for_search_result(
            int(result.id),
            TelegraphPage(
                id=None,
                search_result_id=int(result.id),
                message_db_id=None,
                url=result.url,
                normalized_url=result.normalized_url,
                title="Telegraph Saved",
                image_count=2,
            ),
            [
                TelegraphImage(None, None, 1, "https://telegra.ph/file/one.jpg", "https://telegra.ph/file/one.jpg"),
                TelegraphImage(None, None, 2, "https://telegra.ph/file/two.jpg", "https://telegra.ph/file/two.jpg"),
            ],
            [],
        )
        download_repository = DownloadRecordRepository(database)
        service = _TelegraphImageDownloadService(
            telegram_service=_BackupTelegramService(self._temp_dir),
            message_repository=MessageRepository(database),
            file_repository=FileRepository(database),
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
            telegraph_repository=telegraph_repository,
            telegraph_service=TelegraphService(log_service.get_file_logger("download", "download.log")),
        )

        report = service.download_search_results_media(
            api_id="123",
            api_hash="hash",
            results=[result],
            retry_count=1,
            telegraph_image_limit=1,
        )

        self.assertEqual(report.success_count, 1)
        records = download_repository.list_records_for_task(report.task_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "success")
        self.assertTrue((self._temp_dir / "downloads" / "Telegraph Saved" / "001.jpg").exists())
        images = telegraph_repository.list_images_for_search_result(int(result.id))
        self.assertEqual([image.download_status for image in images], ["success", "pending"])

    def test_download_service_downloads_limited_backed_up_telegraph_page_images(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_repository = DownloadRecordRepository(database)
        telegraph_repository = TelegraphRepository(database)
        html = """
        <html>
          <body>
            <article>
              <h1>Backup Telegraph</h1>
              <p><img src="/file/one.jpg"></p>
              <p><img src="/file/two.jpg"></p>
            </article>
          </body>
        </html>
        """
        message = message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=8,
                sender_id=1,
                sender_name="Alice",
                date="2026-07-03T10:00:00",
                text="Read more",
                text_preview="Read more",
                message_type="text",
                external_urls="https://telegra.ph/Backup-Page-07-03",
            )
        )
        service = _TelegraphImageDownloadService(
            telegram_service=_BackupTelegramService(self._temp_dir),
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
            telegraph_repository=telegraph_repository,
            telegraph_service=TelegraphService(
                log_service.get_file_logger("download", "download.log"),
                fetcher=lambda _url, _timeout: html,
            ),
        )

        report = service.download_message_records_media(
            api_id="123",
            api_hash="hash",
            messages=[message],
            retry_count=1,
            telegraph_image_limit=1,
        )

        self.assertEqual(report.success_count, 1)
        stored_message = message_repository.get_by_chat_and_message_id(-1001, 8)
        self.assertTrue(stored_message.is_downloaded)
        self.assertTrue((self._temp_dir / "downloads" / "Backup Telegraph" / "001.jpg").exists())
        self.assertFalse((self._temp_dir / "downloads" / "Backup Telegraph" / "002.jpg").exists())
        page = telegraph_repository.get_page_by_message_id(
            int(message.id),
            TelegraphService.normalize_url("https://telegra.ph/Backup-Page-07-03"),
        )
        self.assertIsNotNone(page)
        images = telegraph_repository.list_images_for_page(int(page.id))
        self.assertEqual([image.download_status for image in images], ["success", "pending"])
        records = download_repository.list_records_for_task(report.task_id)
        self.assertEqual([record.status for record in records], ["success"])

    def test_download_service_downloads_selected_local_message_media(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_repository = DownloadRecordRepository(database)
        service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        messages = [
            message_repository.upsert_message(
                MessageRecord(
                    id=None,
                    tg_chat_id=-1001,
                    message_id=1,
                    sender_id=1,
                    sender_name="Alice",
                    date="2026-07-01T10:00:00",
                    text="plain text",
                    text_preview="plain text",
                    message_type="text",
                )
            ),
            message_repository.upsert_message(
                MessageRecord(
                    id=None,
                    tg_chat_id=-1001,
                    message_id=2,
                    sender_id=2,
                    sender_name="Bob",
                    date="2026-07-01T10:01:00",
                    text="photo",
                    text_preview="photo",
                    message_type="photo",
                    has_media=True,
                    media_type="photo",
                    media_id="photo2",
                    file_name="photo2.jpg",
                )
            ),
            message_repository.upsert_message(
                MessageRecord(
                    id=None,
                    tg_chat_id=-1001,
                    message_id=3,
                    sender_id=3,
                    sender_name="Cara",
                    date="2026-07-01T10:02:00",
                    text="document",
                    text_preview="document",
                    message_type="document",
                    has_media=True,
                    media_type="document",
                    media_id="doc3",
                    file_name="doc3.pdf",
                )
            ),
        ]
        progress_events = []

        report = service.download_message_records_media(
            api_id="123",
            api_hash="hash",
            messages=messages,
            retry_count=1,
            progress_callback=progress_events.append,
        )

        self.assertEqual(report.success_count, 2)
        self.assertEqual(report.skipped_count, 1)
        self.assertEqual(len(progress_events), 5)
        self.assertEqual([event.status for event in progress_events], ["skipped", "downloading", "success", "downloading", "success"])
        self.assertGreater(progress_events[-1].downloaded_bytes, 0)
        self.assertEqual(progress_events[-1].image_done_count, 1)
        self.assertEqual(progress_events[-1].image_total_count, 1)
        records = download_repository.list_records_for_task(report.task_id)
        self.assertEqual([record.status for record in records], ["skipped", "success", "success"])
        self.assertEqual(telegram_service.download_calls, [2, 3])
        self.assertTrue(message_repository.get_by_chat_and_message_id(-1001, 2).is_downloaded)
        self.assertTrue(message_repository.get_by_chat_and_message_id(-1001, 3).is_downloaded)

    def test_download_service_uses_forwarded_download_link(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_repository = DownloadRecordRepository(database)
        service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        forwarded_text = "\n".join(
            [
                "TGArchiveManager 聊天记录",
                "来源：chat_id=-1001 message_id=2",
                "媒体：photo，文件=photo2.jpg，已下载=否",
                "下载链接：tgarchive://download?chat_id=-1001&message_id=2",
            ]
        )
        proxy_message = message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1002001,
                message_id=50,
                sender_id=1,
                sender_name="Archive Bot",
                date="2026-07-01T11:00:00",
                text=forwarded_text,
                text_preview=forwarded_text,
                message_type="text",
            )
        )

        report = service.download_message_records_media(
            api_id="123",
            api_hash="hash",
            messages=[proxy_message],
            retry_count=1,
        )

        self.assertEqual(report.success_count, 1)
        self.assertEqual(report.skipped_count, 0)
        self.assertEqual(telegram_service.download_calls, [2])
        self.assertTrue(message_repository.get_by_chat_and_message_id(-1002001, 50).is_downloaded)
        file_record = file_repository.get_by_message(-1001, 2)
        self.assertIsNotNone(file_record)
        self.assertEqual(file_record.download_status, "success")
        records = download_repository.list_records_for_task(report.task_id)
        self.assertEqual(records[0].status, "success")

    def test_download_service_skips_disabled_image_media(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_repository = DownloadRecordRepository(database)
        service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
            download_options={"download_images": False},
        )
        message = message_repository.upsert_message(
            MessageRecord(
                id=None,
                tg_chat_id=-1001,
                message_id=2,
                sender_id=102,
                sender_name="Bob",
                date="2026-07-01T10:01:00",
                text="photo",
                text_preview="photo",
                message_type="photo",
                has_media=True,
                media_type="photo",
                media_id="photo2",
                file_name="photo2.jpg",
                file_size=10,
            )
        )

        report = service.download_message_records_media("123", "hash", [message], retry_count=1)

        self.assertEqual(report.success_count, 0)
        self.assertEqual(report.skipped_count, 1)
        self.assertEqual(telegram_service.download_calls, [])
        records = download_repository.list_records_for_task(report.task_id)
        self.assertEqual(records[0].status, "skipped")
        self.assertIn("禁用图片下载", records[0].error_message)

    def test_download_service_cancels_selected_messages_after_partial_progress(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        telegram_service = _BackupTelegramService(self._temp_dir)
        message_repository = MessageRepository(database)
        file_repository = FileRepository(database)
        download_repository = DownloadRecordRepository(database)
        service = DownloadService(
            telegram_service=telegram_service,
            message_repository=message_repository,
            file_repository=file_repository,
            download_record_repository=download_repository,
            download_root=self._temp_dir / "downloads",
            logger=log_service.get_file_logger("download", "download.log"),
        )
        messages = [
            message_repository.upsert_message(
                MessageRecord(
                    id=None,
                    tg_chat_id=-1001,
                    message_id=2,
                    sender_id=102,
                    sender_name="Bob",
                    date="2026-07-01T10:01:00",
                    text="photo 1",
                    text_preview="photo 1",
                    message_type="photo",
                    has_media=True,
                    media_type="photo",
                    media_id="photo2",
                    file_name="photo2.jpg",
                    file_size=10,
                )
            ),
            message_repository.upsert_message(
                MessageRecord(
                    id=None,
                    tg_chat_id=-1001,
                    message_id=3,
                    sender_id=103,
                    sender_name="Carol",
                    date="2026-07-01T10:02:00",
                    text="photo 2",
                    text_preview="photo 2",
                    message_type="photo",
                    has_media=True,
                    media_type="photo",
                    media_id="photo3",
                    file_name="photo3.jpg",
                    file_size=10,
                )
            ),
        ]
        token = CancellationToken()
        progress_events = []

        def on_progress(progress) -> None:
            progress_events.append(progress)
            if progress.done_count == 1:
                token.cancel()

        with self.assertRaises(OperationCancelled):
            service.download_message_records_media(
                api_id="123",
                api_hash="hash",
                messages=messages,
                retry_count=1,
                progress_callback=on_progress,
                cancel_token=token,
        )

        self.assertEqual(telegram_service.download_calls, [2])
        self.assertGreaterEqual(len(progress_events), 1)
        self.assertEqual(progress_events[-1].done_count, 1)
        records = download_repository.list_records_for_task(progress_events[-1].task_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "success")

    def test_backup_page_formats_message_type_and_media_size(self) -> None:
        text_message = MessageRecord(
            id=1,
            tg_chat_id=-1001,
            message_id=1,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:00:00",
            text="hello",
            text_preview="hello",
            message_type="text",
        )
        link_message = MessageRecord(
            id=2,
            tg_chat_id=-1001,
            message_id=2,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:01:00",
            text="https://example.com/report",
            text_preview="https://example.com/report",
            message_type="text",
        )
        photo_message = MessageRecord(
            id=3,
            tg_chat_id=-1001,
            message_id=3,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:02:00",
            text="photo",
            text_preview="photo",
            message_type="photo",
            has_media=True,
            media_type="photo",
            file_size=1536,
        )
        video_message = MessageRecord(
            id=4,
            tg_chat_id=-1001,
            message_id=4,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:03:00",
            text="video",
            text_preview="video",
            message_type="video",
            has_media=True,
            media_type="video",
            file_size=2 * 1024 * 1024,
        )
        document_message = MessageRecord(
            id=6,
            tg_chat_id=-1001,
            message_id=6,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:05:00",
            text="document",
            text_preview="document",
            message_type="document",
            has_media=True,
            media_type="document",
            file_size=4096,
        )
        telegraph_message = MessageRecord(
            id=5,
            tg_chat_id=-1001,
            message_id=5,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:04:00",
            text="hidden link",
            text_preview="hidden link",
            message_type="text",
            external_urls="https://telegra.ph/Backup-Page-07-03",
        )

        self.assertEqual(BackupPage._display_message_type(text_message), "文字")
        self.assertEqual(BackupPage._display_media_size(text_message), "-")
        self.assertEqual(BackupPage._display_message_type(link_message), "链接")
        self.assertEqual(BackupPage._display_media_size(link_message), "-")
        self.assertIn("Telegraph", BackupPage._display_message_type(telegraph_message))
        self.assertEqual(BackupPage._display_media_size(telegraph_message), "图片数未知")
        self.assertEqual(BackupPage._display_message_type(photo_message), "图片")
        self.assertEqual(BackupPage._display_media_size(photo_message), "1.5 KB")
        self.assertEqual(BackupPage._display_message_type(video_message), "视频")
        self.assertEqual(BackupPage._display_media_size(video_message), "2.0 MB")
        self.assertEqual(BackupPage._display_message_type(document_message), "文件")
        self.assertEqual(BackupPage._display_media_size(document_message), "4.0 KB")

    def test_backup_page_displays_telegraph_image_count_from_metadata(self) -> None:
        config_service = ConfigService(self._temp_dir)
        config = config_service.load()
        log_service = LogService(self._temp_dir, config)
        log_service.configure()
        database = DatabaseManager(self._temp_dir, config, log_service.get_logger("database"))
        database.initialize()

        telegraph_repository = TelegraphRepository(database)
        message = MessageRecord(
            id=10,
            tg_chat_id=-1001,
            message_id=10,
            sender_id=1,
            sender_name="Alice",
            date="2026-07-01T10:10:00",
            text="https://telegra.ph/Backup-Page-07-03",
            text_preview="https://telegra.ph/Backup-Page-07-03",
            message_type="telegraph_page",
            external_urls="https://telegra.ph/Backup-Page-07-03",
        )
        telegraph_repository.upsert_page_for_message(
            int(message.id),
            TelegraphPage(
                id=None,
                search_result_id=None,
                message_db_id=int(message.id),
                url="https://telegra.ph/Backup-Page-07-03",
                normalized_url=TelegraphService.normalize_url("https://telegra.ph/Backup-Page-07-03"),
                title="Backup Telegraph",
                image_count=3,
            ),
            [],
            [],
        )
        page = BackupPage.__new__(BackupPage)
        page._telegraph_repository = telegraph_repository

        self.assertEqual(page._display_media_size_for_row(message), "图片 3 张")


class _FakeProvider(BaseSearchProvider):
    @property
    def engine_name(self) -> str:
        return "jisou"

    def search(self, api_id: str, api_hash: str, keyword: str, max_results: int) -> list[SearchResult]:
        parsed = [
            ParsedBotResult("Title 1", "Summary 1", "https://t.me/demo/1"),
            ParsedBotResult("Title 2", "Summary 2", "https://t.me/demo/2"),
        ]
        return ResultNormalizer().normalize(keyword, self.engine_name, parsed, max_results)


class _CancellingProvider(BaseSearchProvider):
    @property
    def engine_name(self) -> str:
        return "jisou"

    def search(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        cancel_token: CancellationToken | None = None,
    ) -> list[SearchResult]:
        if cancel_token is not None:
            cancel_token.cancel()
        check_cancelled(cancel_token)
        return []


class _TelegraphProvider(BaseSearchProvider):
    @property
    def engine_name(self) -> str:
        return "jisou"

    def search(self, api_id: str, api_hash: str, keyword: str, max_results: int) -> list[SearchResult]:
        parsed = [
            ParsedBotResult("Telegraph result", "Summary", "https://telegra.ph/Demo-Page-07-03"),
        ]
        return ResultNormalizer().normalize(keyword, self.engine_name, parsed, max_results)


class _NativeSearchTelegramService:
    def __init__(self):
        self.last_target_chat_ids = None

    def search_joined_messages(self, api_id, api_hash, keyword, max_results, target_chat_ids=None, cancel_token=None):
        check_cancelled(cancel_token)
        self.last_target_chat_ids = None if target_chat_ids is None else [int(chat_id) for chat_id in target_chat_ids]
        messages = [
            TelegramArchivedMessage(
                tg_chat_id=-1001001,
                message_id=10,
                sender_id=101,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="demo text",
                text_preview="demo text",
                message_type="text",
                source_link="https://t.me/c/1001/10",
                chat_title="Demo Channel",
            ),
            TelegramArchivedMessage(
                tg_chat_id=-1001002,
                message_id=11,
                sender_id=102,
                sender_name="Bob",
                date="2026-07-01T10:01:00",
                text="media demo",
                text_preview="media demo",
                message_type="photo",
                has_media=True,
                media_type="photo",
                file_size=1536,
                source_link="https://t.me/c/1002/11",
                chat_title="Media Group",
            ),
        ]
        if target_chat_ids is not None:
            target_ids = {int(chat_id) for chat_id in target_chat_ids}
            messages = [message for message in messages if message.tg_chat_id in target_ids]
        return messages[:max_results]


class _VerificationThenResultsProvider(BaseSearchProvider):
    @property
    def engine_name(self) -> str:
        return "jisou"

    def search(self, api_id: str, api_hash: str, keyword: str, max_results: int) -> list[SearchResult]:
        raise SearchProviderVerificationRequired(
            message="搜索 Bot 要求人机验证，请在下方选择验证结果并提交。",
            bot_username="@jisou",
            message_id=1,
            prompt="请选择计算结果",
            options=["3", "5", "8"],
        )

    def submit_verification(
        self,
        api_id: str,
        api_hash: str,
        keyword: str,
        max_results: int,
        message_id: int,
        button_text: str,
    ) -> list[SearchResult]:
        if button_text != "8":
            raise SearchProviderVerificationRequired(
                message="Bot 仍要求人机验证，请重新选择验证结果并提交。",
                bot_username="@jisou",
                message_id=message_id,
                prompt="请选择计算结果",
                options=["3", "5", "8"],
            )
        parsed = [ParsedBotResult("验证后结果", "Summary", "https://t.me/verified/1")]
        return ResultNormalizer().normalize(keyword, self.engine_name, parsed, max_results)


class _VerificationTelegramService:
    def query_search_bot(self, api_id, api_hash, bot_username, keyword, response_limit, timeout_seconds):
        return [
            SimpleNamespace(
                message_id=1,
                text="@user 您必须完成人机验证才能继续使用 请选择计算结果👇👇👇",
                button_urls=[],
                button_texts=["3", "5", "8"],
                text_links=[],
                media_path="data/verification_media/bot_verification_1.jpg",
            )
        ]


class _VerificationSuccessTelegramService:
    def click_bot_button_and_collect_responses(
        self,
        api_id,
        api_hash,
        bot_username,
        message_id,
        button_text,
        response_limit,
        timeout_seconds,
    ):
        return [
            SimpleNamespace(
                message_id=2,
                text="验证后结果\nhttps://t.me/verified/1\nSummary",
                button_urls=[],
                button_texts=[],
                text_links=[],
            )
        ]


class _PaginatedTelegramService:
    def __init__(self):
        self.clicked_buttons = []

    def query_search_bot(self, api_id, api_hash, bot_username, keyword, response_limit, timeout_seconds):
        return [
            SimpleNamespace(
                message_id=10,
                text="第一页\nhttps://t.me/page1/1\nhttps://t.me/page1/2",
                button_urls=[],
                button_texts=["下一页"],
                text_links=[],
            )
        ]

    def click_bot_button_and_collect_responses(
        self,
        api_id,
        api_hash,
        bot_username,
        message_id,
        button_text,
        response_limit,
        timeout_seconds,
    ):
        self.clicked_buttons.append(button_text)
        return [
            SimpleNamespace(
                message_id=10,
                text="第二页\nhttps://t.me/page2/3\nhttps://t.me/page2/4",
                button_urls=[],
                button_texts=[],
                text_links=[],
            )
        ]


class _ForwardTelegramService:
    def send_text_messages(
        self,
        api_id,
        api_hash,
        target_chat_id,
        messages,
        interval_seconds,
        progress_callback=None,
        cancel_token=None,
    ):
        results = []
        total = len(messages)
        for index, message in enumerate(messages, start=1):
            check_cancelled(cancel_token)
            result = TelegramSendResult(
                source_id=message.source_id,
                status="success",
                target_message_id=1000 + message.source_id,
            )
            results.append(result)
            if progress_callback is not None:
                progress_callback(result, index, total)
            check_cancelled(cancel_token)
        return results


class _GroupTelegramService:
    def create_group(self, api_id, api_hash, title):
        return Chat(
            id=None,
            tg_chat_id=-100999,
            title=str(title),
            username="",
            type="group",
            is_created_by_tool=True,
        )


class _AutoGroupService:
    def __init__(self):
        self.categories = []

    def create_target_group(self, api_id, api_hash, title, category=""):
        self.categories.append(category)
        return Chat(
            id=None,
            tg_chat_id=-1002001 - len(self.categories) + 1,
            title=str(title),
            username="",
            type="group",
            tag=category,
            is_created_by_tool=True,
        )


class _BackupTelegramService:
    def __init__(self, root: Path):
        self._root = Path(root)
        self.download_calls = []

    def fetch_chat_messages(
        self,
        api_id,
        api_hash,
        tg_chat_id,
        limit,
        min_message_id,
        date_from="",
        date_to="",
        cancel_token=None,
    ):
        check_cancelled(cancel_token)
        messages = [
            TelegramArchivedMessage(
                tg_chat_id=tg_chat_id,
                message_id=1,
                sender_id=101,
                sender_name="Alice",
                date="2026-07-01T10:00:00",
                text="plain text",
                text_preview="plain text",
                message_type="text",
            ),
            TelegramArchivedMessage(
                tg_chat_id=tg_chat_id,
                message_id=2,
                sender_id=102,
                sender_name="Bob",
                date="2026-07-01T10:01:00",
                text="photo",
                text_preview="photo",
                message_type="photo",
                has_media=True,
                media_type="photo",
                media_id="photo2",
                file_name="photo2.jpg",
                file_size=10,
            ),
            TelegramArchivedMessage(
                tg_chat_id=tg_chat_id,
                message_id=3,
                sender_id=103,
                sender_name="Carol",
                date="2026-07-01T10:02:00",
                text="document",
                text_preview="document",
                message_type="document",
                has_media=True,
                media_type="document",
                media_id="doc3",
                file_name="doc3.pdf",
                file_size=20,
            ),
        ]
        return [message for message in messages if message.message_id > int(min_message_id)]

    def download_archived_message_media(
        self,
        api_id,
        api_hash,
        tg_chat_id,
        message_id,
        download_dir,
        progress_callback=None,
        cancel_token=None,
    ):
        check_cancelled(cancel_token)
        self.download_calls.append(int(message_id))
        target_dir = Path(download_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{message_id}.jpg"
        path.write_bytes(b"media")
        if progress_callback is not None:
            progress_callback(path.stat().st_size, path.stat().st_size)
        check_cancelled(cancel_token)
        return TelegramMediaDownloadResult(
            tg_chat_id=int(tg_chat_id),
            message_id=int(message_id),
            status="success",
            local_path=str(path),
            file_name=path.name,
            file_size=path.stat().st_size,
        )


class _TelegraphImageDownloadService(DownloadService):
    def _fetch_binary_url(self, url: str, progress_callback=None, cancel_token=None) -> tuple[bytes, str]:
        check_cancelled(cancel_token)
        if progress_callback is not None:
            progress_callback(5, 5)
        check_cancelled(cancel_token)
        return b"image", "image/jpeg"


if __name__ == "__main__":
    unittest.main()
