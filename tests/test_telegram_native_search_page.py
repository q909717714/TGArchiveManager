"""Tests for Telegram native search scope grouping."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from database.db import DatabaseManager  # noqa: E402
from database.models import Chat  # noqa: E402
from database.repositories import ChatRepository  # noqa: E402
from services.config_service import ConfigService  # noqa: E402
from services.log_service import LogService  # noqa: E402
from services.runtime_state import RuntimeState  # noqa: E402
from ui.telegram_native_search_page import TelegramNativeSearchPage  # noqa: E402


class TelegramNativeSearchPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._temp_dir = Path(tempfile.mkdtemp(prefix="tg_native_page_test_"))
        source_config = Path(__file__).resolve().parents[1] / "config" / "config.yaml.example"
        target_config_dir = self._temp_dir / "config"
        target_config_dir.mkdir(parents=True)
        shutil.copyfile(source_config, target_config_dir / "config.yaml.example")

        self._config_service = ConfigService(self._temp_dir)
        config = self._config_service.load()
        self._log_service = LogService(self._temp_dir, config)
        self._log_service.configure()
        self._database = DatabaseManager(self._temp_dir, config, self._log_service.get_logger("database"))
        self._database.initialize()

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

    def test_scope_tree_group_can_collapse_and_select_all_channels(self) -> None:
        repository = ChatRepository(self._database)
        repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1001,
                title="Alpha Channel",
                username="alpha_channel",
                type="channel",
                telegram_folder_names="资料频道",
            )
        )
        repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1002,
                title="Beta Group",
                username="beta_group",
                type="group",
                telegram_folder_names="资料频道",
            )
        )
        repository.upsert_chat(
            Chat(
                id=None,
                tg_chat_id=-1003,
                title="News Channel",
                username="news_channel",
                type="channel",
                telegram_folder_names="公告",
            )
        )

        page = TelegramNativeSearchPage(
            project_root=self._temp_dir,
            config_service=self._config_service,
            log_service=self._log_service,
            database=self._database,
            runtime_state=RuntimeState(),
        )
        self.addCleanup(page.deleteLater)

        group_item = self._find_scope_group(page, "资料频道")
        self.assertIsNotNone(group_item)
        self.assertTrue(group_item.isExpanded())

        group_item.setExpanded(False)
        self.assertFalse(group_item.isExpanded())

        page._clear_scope_chats()
        group_item = self._find_scope_group(page, "资料频道")
        self.assertIsNotNone(group_item)
        self.assertEqual(page._selected_scope_chat_ids, set())

        group_item.setCheckState(0, Qt.Checked)
        QApplication.processEvents()

        self.assertEqual(page._selected_scope_chat_ids, {-1001, -1002})
        self.assertEqual(group_item.child(0).checkState(0), Qt.Checked)
        self.assertEqual(group_item.child(1).checkState(0), Qt.Checked)

        group_item.setCheckState(0, Qt.Unchecked)
        QApplication.processEvents()

        self.assertEqual(page._selected_scope_chat_ids, set())
        self.assertEqual(group_item.child(0).checkState(0), Qt.Unchecked)
        self.assertEqual(group_item.child(1).checkState(0), Qt.Unchecked)

    @staticmethod
    def _find_scope_group(page: TelegramNativeSearchPage, group_name: str):
        for index in range(page._channel_tree.topLevelItemCount()):
            item = page._channel_tree.topLevelItem(index)
            if item.text(0).startswith(group_name):
                return item
        return None


if __name__ == "__main__":
    unittest.main()
