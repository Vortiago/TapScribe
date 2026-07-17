"""Unit tests for the e2e RecorderServer harness itself (no browser needed)."""

from __future__ import annotations

import socket

import pytest
from fastapi import FastAPI

from .harness import RecorderServer


def test_recorder_server_retries_past_a_port_taken_on_ipv6() -> None:
    """Regression for the ::1 port-collision flake.

    free_port() reserves a port on IPv4 only, but uvicorn binds the dual-stack
    `localhost` name, so a port free on 127.0.0.1 can be in use on ::1. uvicorn
    surfaces that bind failure as SystemExit(STARTUP_FAILURE), not OSError, so
    the previous `except OSError` never captured it: start() fell through to a
    ready-timeout and raised instead of retrying. This deterministically
    reproduces the collision by occupying the chosen port on ::1, then asserts
    the server still comes up on a fresh port.
    """
    server = RecorderServer(FastAPI())
    taken = server.port

    blocker = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        try:
            blocker.bind(("::1", taken))
        except OSError:
            pytest.skip("no ::1 IPv6 loopback available to reproduce the collision")
        blocker.listen(1)

        # Before the fix this raises (SystemExit uncaught -> no retry -> timeout).
        server.start(ready_timeout=5.0)
        try:
            # Success is only possible on a port where ::1 is also free, i.e. a
            # port the retry picked after the initial collision.
            assert server.port != taken
        finally:
            server.stop()
    finally:
        blocker.close()
