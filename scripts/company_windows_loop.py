"""Windows company host event loop, preserving subprocess support for Agents.

CPython 3.12's Proactor transport can skip socket close and Server._detach
when socket.shutdown raises WinError 10054. Server.wait_closed then never
finishes, even after all requests have drained. Complete that cleanup only
for this reset case; do not bypass pending application work or lifespans.
"""

from __future__ import annotations

import asyncio
import logging
import os

if os.name == "nt":
    from asyncio.proactor_events import _ProactorSocketTransport

    class ResetSafeSocketTransport(_ProactorSocketTransport):
        def _call_connection_lost(self, exc):
            try:
                super()._call_connection_lost(exc)
            except ConnectionResetError:
                # If cleanup completed, the exception came from the protocol
                # itself. Preserve it instead of hiding an application error.
                if self._called_connection_lost or self._sock is None:
                    raise
                try:
                    self._sock.close()
                finally:
                    self._sock = None
                    if self._server is not None:
                        self._server._detach()
                        self._server = None
                    self._called_connection_lost = True
                logging.getLogger(__name__).info("Closed a reset Windows socket during connection cleanup")

    class CompanyProactorEventLoop(asyncio.ProactorEventLoop):
        def _make_socket_transport(self, sock, protocol, waiter=None, extra=None, server=None):
            return ResetSafeSocketTransport(self, sock, protocol, waiter, extra, server)


def run(coroutine):
    if os.name == "nt":
        with asyncio.Runner(loop_factory=CompanyProactorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)
