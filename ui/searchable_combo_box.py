"""Reusable fuzzy-search combo box for PySide6 pages."""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QEvent, QSignalBlocker, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QComboBox


class _FuzzyFilterModel(QSortFilterProxyModel):
    """Filter combo-box rows by case-insensitive substring or subsequence match."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter_text = ""

    def set_filter_text(self, text: str) -> None:
        filter_text = _normalize(text)
        if hasattr(self, "beginFilterChange") and hasattr(self, "endFilterChange"):
            self.beginFilterChange()
            self._filter_text = filter_text
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        elif hasattr(self, "invalidateRowsFilter"):
            self._filter_text = filter_text
            self.invalidateRowsFilter()
        else:
            self._filter_text = filter_text
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802 - Qt override
        if not self._filter_text:
            return True
        source_model = self.sourceModel()
        if source_model is None:
            return False
        index = source_model.index(source_row, 0, source_parent)
        display_text = str(index.data(Qt.DisplayRole) or "")
        user_data = index.data(Qt.UserRole)
        haystack = _normalize(f"{display_text} {user_data if user_data is not None else ''}")
        return _matches_filter(self._filter_text, haystack)


class SearchableComboBox(QComboBox):
    """QComboBox with fuzzy filtering inside the drop-down popup.

    The widget keeps an unfiltered source model internally. Typing in the line edit
    filters the popup candidates only; choosing an item restores the full model so
    existing code can keep using ``currentData()``, ``findData()`` and ``addItem()``.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source_model = QStandardItemModel(self)
        self._filter_model = _FuzzyFilterModel(self)
        self._filter_model.setSourceModel(self._source_model)
        self._filter_text = ""
        self._pre_search_text = ""
        self._pre_search_data: Any = None

        super().setModel(self._filter_model)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self.setCompleter(None)

        line_edit = self.lineEdit()
        if line_edit is not None:
            line_edit.setClearButtonEnabled(True)
            line_edit.setPlaceholderText("输入关键字过滤")
            line_edit.textEdited.connect(self._on_text_edited)
            line_edit.returnPressed.connect(self._commit_first_filtered)
            line_edit.editingFinished.connect(self._on_editing_finished)
            line_edit.installEventFilter(self)

        self.activated.connect(self._on_activated)

    def addItem(self, *args) -> None:  # noqa: N802 - Qt API compatibility
        icon, text, user_data = self._parse_add_item_args(args)
        item = QStandardItem(icon, text) if icon is not None else QStandardItem(text)
        item.setData(user_data, Qt.UserRole)
        self._source_model.appendRow(item)

    def addItems(self, texts: Iterable[str]) -> None:  # noqa: N802 - Qt API compatibility
        for text in texts:
            self.addItem(str(text))

    def insertItem(self, index: int, *args) -> None:  # noqa: N802 - Qt API compatibility
        icon, text, user_data = self._parse_add_item_args(args)
        item = QStandardItem(icon, text) if icon is not None else QStandardItem(text)
        item.setData(user_data, Qt.UserRole)
        self._source_model.insertRow(max(0, int(index)), item)

    def insertItems(self, index: int, texts: Iterable[str]) -> None:  # noqa: N802 - Qt API compatibility
        row = max(0, int(index))
        for offset, text in enumerate(texts):
            self.insertItem(row + offset, str(text))

    def clear(self) -> None:
        self._filter_text = ""
        self._pre_search_text = ""
        self._pre_search_data = None
        self._filter_model.set_filter_text("")
        self._source_model.clear()
        super().setCurrentIndex(-1)
        if self.lineEdit() is not None:
            self.lineEdit().clear()

    def showPopup(self) -> None:  # noqa: N802 - Qt override
        if not self._filter_text:
            self._filter_model.set_filter_text("")
        super().showPopup()
        if not self._filter_text:
            QTimer.singleShot(0, self._select_all_text)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if watched is self.lineEdit() and event.type() == QEvent.FocusIn and not self._filter_text:
            QTimer.singleShot(0, self._select_all_text)
        return super().eventFilter(watched, event)

    def _on_text_edited(self, text: str) -> None:
        if not self._filter_text:
            self._pre_search_text = super().currentText()
            self._pre_search_data = super().currentData()

        self._filter_text = str(text)
        with QSignalBlocker(self):
            self._filter_model.set_filter_text(self._filter_text)
            if self._filter_model.rowCount() == 0:
                super().setCurrentIndex(-1)
            super().setEditText(self._filter_text)

        if not self.view().isVisible():
            self.showPopup()

    def _on_activated(self, index: int) -> None:
        if index < 0:
            return
        self._commit_selection(self.itemText(index), self.itemData(index), emit_signal=False)

    def _on_editing_finished(self) -> None:
        if self._filter_text:
            self._commit_first_filtered()

    def _commit_first_filtered(self) -> None:
        if not self._filter_text:
            return
        if self._filter_model.rowCount() <= 0:
            self._restore_pre_search_selection()
            return

        index = self.currentIndex()
        if index < 0 or index >= self._filter_model.rowCount():
            index = 0
        self._commit_selection(self.itemText(index), self.itemData(index), emit_signal=True)

    def _commit_selection(self, text: str, data: Any, emit_signal: bool) -> None:
        if emit_signal:
            self._apply_committed_selection(text, data)
        else:
            with QSignalBlocker(self):
                self._apply_committed_selection(text, data)
        self._filter_text = ""
        self._pre_search_text = text
        self._pre_search_data = data

    def _apply_committed_selection(self, text: str, data: Any) -> None:
        self._filter_model.set_filter_text("")
        index = self._find_unfiltered_index(text, data)
        if index >= 0:
            super().setCurrentIndex(index)
            super().setEditText(self.itemText(index))
        else:
            super().setCurrentIndex(-1)
            super().setEditText("")

    def _restore_pre_search_selection(self) -> None:
        with QSignalBlocker(self):
            self._apply_committed_selection(self._pre_search_text, self._pre_search_data)
        self._filter_text = ""

    def _find_unfiltered_index(self, text: str, data: Any) -> int:
        if data is not None:
            index = super().findData(data)
            if index >= 0:
                return index
        if text:
            return super().findText(text, Qt.MatchExactly)
        return -1

    def _select_all_text(self) -> None:
        line_edit = self.lineEdit()
        if line_edit is not None and not self._filter_text:
            line_edit.selectAll()

    @staticmethod
    def _parse_add_item_args(args: tuple[Any, ...]) -> tuple[QIcon | None, str, Any]:
        if len(args) == 1:
            return None, str(args[0]), None
        if len(args) == 2:
            if isinstance(args[0], QIcon):
                return args[0], str(args[1]), None
            return None, str(args[0]), args[1]
        if len(args) == 3 and isinstance(args[0], QIcon):
            return args[0], str(args[1]), args[2]
        raise TypeError("addItem expects text, text/data, or icon/text/data arguments")


def _normalize(value: object) -> str:
    return str(value or "").strip().casefold()


def _matches_filter(needle: str, haystack: str) -> bool:
    tokens = [token for token in needle.split() if token]
    if not tokens:
        return True
    return all(token in haystack or _is_subsequence(token, haystack) for token in tokens)


def _is_subsequence(needle: str, haystack: str) -> bool:
    position = 0
    for char in needle:
        position = haystack.find(char, position)
        if position < 0:
            return False
        position += 1
    return True
