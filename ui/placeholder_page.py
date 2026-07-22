"""Reusable placeholder pages for features implemented in later stages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class PlaceholderPage(QWidget):
    """Simple non-interactive page for a planned module."""

    def __init__(self, title: str, stage_note: str, parent=None):
        super().__init__(parent)

        title_label = QLabel(title)
        title_label.setObjectName("pageTitle")

        note_label = QLabel(stage_note)
        note_label.setObjectName("pageNote")
        note_label.setWordWrap(True)
        note_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(title_label)
        layout.addWidget(note_label)
        layout.addStretch(1)
