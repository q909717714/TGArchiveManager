"""Tests for the reusable searchable combo box."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.searchable_combo_box import SearchableComboBox  # noqa: E402


class SearchableComboBoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_filters_candidates_and_restores_full_list_after_commit(self) -> None:
        combo = SearchableComboBox()
        combo.addItem("Alpha Channel", 1)
        combo.addItem("Beta Group", 2)
        combo.addItem("Archive News", 3)

        self.assertEqual(combo.count(), 3)
        self.assertEqual(combo.currentData(), 1)

        combo._on_text_edited("group")
        self.assertEqual(combo.count(), 1)

        combo._commit_first_filtered()
        self.assertEqual(combo.count(), 3)
        self.assertEqual(combo.currentData(), 2)

    def test_no_match_keeps_previous_selection(self) -> None:
        combo = SearchableComboBox()
        combo.addItem("Alpha Channel", 1)
        combo.addItem("Beta Group", 2)
        combo.setCurrentIndex(1)

        combo._on_text_edited("missing")
        self.assertEqual(combo.count(), 0)

        combo._commit_first_filtered()
        self.assertEqual(combo.count(), 2)
        self.assertEqual(combo.currentData(), 2)


if __name__ == "__main__":
    unittest.main()
