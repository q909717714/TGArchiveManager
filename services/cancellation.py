"""Cooperative cancellation primitives for long-running service calls."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from utils.error_codes import OP001


class OperationCancelled(RuntimeError):
    """Raised when a user-requested cancellation reaches a safe checkpoint."""

    error_code = OP001

    def __init__(self, message: str = "任务已取消"):
        super().__init__(message)


class CancellationToken:
    """Thread-safe cancellation flag shared between UI workers and services."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cooperative cancellation."""
        self._event.set()

    def is_cancelled(self) -> bool:
        """Return whether cancellation has been requested."""
        return self._event.is_set()

    def throw_if_cancelled(self) -> None:
        """Raise OperationCancelled when cancellation has been requested."""
        if self.is_cancelled():
            raise OperationCancelled()


def check_cancelled(cancel_token: Optional[CancellationToken]) -> None:
    """Raise OperationCancelled if the optional cancellation token is set."""
    if cancel_token is not None:
        cancel_token.throw_if_cancelled()


def sleep_with_cancel(cancel_token: Optional[CancellationToken], seconds: float, step_seconds: float = 0.2) -> None:
    """Sleep in short chunks so synchronous code can respond to cancellation."""
    remaining = max(0.0, float(seconds or 0))
    step = max(0.05, float(step_seconds or 0.2))
    while remaining > 0:
        check_cancelled(cancel_token)
        wait_time = min(step, remaining)
        time.sleep(wait_time)
        remaining -= wait_time
    check_cancelled(cancel_token)


async def async_sleep_with_cancel(
    cancel_token: Optional[CancellationToken],
    seconds: float,
    step_seconds: float = 0.2,
) -> None:
    """Async sleep in short chunks so Telethon loops can respond to cancellation."""
    remaining = max(0.0, float(seconds or 0))
    step = max(0.05, float(step_seconds or 0.2))
    while remaining > 0:
        check_cancelled(cancel_token)
        wait_time = min(step, remaining)
        await asyncio.sleep(wait_time)
        remaining -= wait_time
    check_cancelled(cancel_token)
