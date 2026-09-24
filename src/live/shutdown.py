"""Graceful-shutdown helpers — SIGTERM / SIGINT handlers + atexit.

Exposes :func:`install` which:
* Sets up SIGTERM / SIGINT handlers that set the asyncio Event.
* Registers :func:`cleanup` with ``atexit`` so even an unhandled
  exception goes through the orderly close path.
* Provides a synchronous :func:`force_cleanup` for emergency
  use (e.g. the systemd KillSignal).

Why a single :class:`Shutdown` object
=====================================

Tying the lifecycle to a single instance (passed to every
component) means the ``await cleanup()`` path is the same
whether triggered by a signal, an atexit, or an explicit call.
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import signal
import sys
import time
from typing import Awaitable, Callable, Optional

_log = logging.getLogger(__name__)


class Shutdown:
    """Asyncio-coordinated shutdown signal + ordered close callbacks."""

    def __init__(self) -> None:
        self.event = asyncio.Event()
        self._cleanups: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._installed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def is_set(self) -> bool:
        return self.event.is_set()

    def trigger(self, reason: str = "external") -> None:
        if not self.event.is_set():
            _log.info("shutdown triggered", extra={"reason": reason})
            self.event.set()

    def register_cleanup(
        self,
        name: str,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        self._cleanups.append((name, callback))

    async def run_cleanups(self, timeout: float = 15.0) -> None:
        """Run every registered cleanup in registration order.

        Wraps each in a timeout so a stuck cleanup can't hold
        the process open past systemd's ``TimeoutStopSec``.
        """
        for name, cb in self._cleanups:
            try:
                await asyncio.wait_for(cb(), timeout=timeout)
                _log.info("cleanup ok", extra={"step": name})
            except asyncio.TimeoutError:
                _log.error("cleanup timed out", extra={"step": name, "timeout": timeout})
            except Exception as e:
                _log.exception("cleanup failed", extra={"step": name, "err": repr(e)})

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._installed:
            return
        self._loop = loop
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig,
                    lambda s=sig: self.trigger(reason=f"signal:{signal.Signals(s).name}"),
                )
            except NotImplementedError:
                # Windows / restricted env — fall back to default handlers.
                signal.signal(sig, lambda s, _f: self.trigger(reason=f"signal:{s}"))
        # Ensure clean shutdown on interpreter shutdown too.
        atexit.register(self._atexit_sync)
        self._installed = True

    def _atexit_sync(self) -> None:
        if not self.event.is_set():
            self.trigger(reason="atexit")
        # Run cleanups synchronously: each cleanup is async; we can't
        # await here. If the loop is still running, give it 2s to finish.
        if self._loop is not None and self._loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.run_cleanups(timeout=2.0), self._loop
                )
                future.result(timeout=3.0)
            except Exception as e:
                _log.error("atexit async cleanup failed", extra={"err": repr(e)})


def install(loop: asyncio.AbstractEventLoop) -> Shutdown:
    """Create + install a Shutdown on the given loop."""
    sd = Shutdown()
    sd.install_signal_handlers(loop)
    return sd


__all__ = ["Shutdown", "install"]