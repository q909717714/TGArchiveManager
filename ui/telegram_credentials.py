"""Shared helpers for Telegram API credentials used by UI pages."""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtWidgets import QMessageBox, QWidget

from services.runtime_state import RuntimeState


def telegram_credentials_or_warn(parent: QWidget, runtime_state: RuntimeState) -> Optional[Tuple[str, str]]:
    """Return shared Telegram API credentials or show a user-facing warning."""
    api_id = str(runtime_state.api_id).strip()
    api_hash = str(runtime_state.api_hash).strip()
    if api_id and api_hash:
        return api_id, api_hash

    QMessageBox.warning(
        parent,
        "缺少 Telegram API 配置",
        "请先在“账号登录”页面填写并保存 API ID 和 API Hash。",
    )
    return None
