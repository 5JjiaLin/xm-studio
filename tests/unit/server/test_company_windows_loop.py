"""The pinned Windows runtime must detach reset sockets before graceful stop."""

import os
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Proactor transport")


def transport_with_reset(protocol_error=None):
    from scripts.company_windows_loop import ResetSafeSocketTransport

    transport = object.__new__(ResetSafeSocketTransport)
    transport._called_connection_lost = False
    transport._protocol = Mock()
    transport._protocol.connection_lost.side_effect = protocol_error
    transport._sock = Mock()
    transport._sock.fileno.return_value = 1
    transport._sock.shutdown.side_effect = ConnectionResetError(10054, "client reset")
    transport._server = Mock()
    return transport


def test_reset_closes_socket_and_detaches_exactly_once():
    transport = transport_with_reset()
    sock, server, protocol = transport._sock, transport._server, transport._protocol
    transport._call_connection_lost(None)
    transport._call_connection_lost(None)
    sock.close.assert_called_once()
    server._detach.assert_called_once()
    protocol.connection_lost.assert_called_once_with(None)


def test_unrelated_protocol_failure_is_preserved():
    transport = transport_with_reset(ValueError("bad protocol"))
    transport._sock.shutdown.side_effect = None
    with pytest.raises(ValueError, match="bad protocol"):
        transport._call_connection_lost(None)
    assert transport._called_connection_lost
