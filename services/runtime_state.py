"""In-memory runtime state shared by UI pages."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RuntimeState:
    """Transient state for the current GUI process.

    The Telegram API hash is intentionally kept in memory only. It is not
    written to YAML by this state object.
    """

    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    current_account: str = ""

    def update_credentials(self, api_id: str, api_hash: str, phone: str = "") -> None:
        self.api_id = str(api_id).strip()
        self.api_hash = str(api_hash).strip()
        if phone:
            self.phone = str(phone).strip()

    def update_account(self, account: str, phone: str = "") -> None:
        self.current_account = str(account).strip()
        if phone:
            self.phone = str(phone).strip()
