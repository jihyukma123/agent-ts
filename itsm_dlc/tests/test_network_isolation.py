from __future__ import annotations

import socket

import httpx
import pytest

from app.release_assurance.evidence import network_fence_result
from tests.conftest import NetworkBlocker, UnexpectedNetworkError


@pytest.mark.anyio
async def test_fresh_httpx_client_is_blocked_before_dns_or_socket_io(
    network_default_deny: NetworkBlocker,
):
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(UnexpectedNetworkError, match="socket.getaddrinfo"):
            await client.get("http://unregistered.invalid/should-never-resolve")
    assert network_default_deny.attempts == ["socket.getaddrinfo"]


def test_raw_socket_bypass_is_blocked_before_connect_io(
    network_default_deny: NetworkBlocker,
):
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(UnexpectedNetworkError, match="socket.socket.connect"):
            candidate.connect(("127.0.0.1", 9))
    finally:
        candidate.close()
    assert network_default_deny.attempts == ["socket.socket.connect"]


def test_release_evidence_requires_zero_unexpected_and_one_blocked_bypass():
    evidence = network_fence_result(unexpected_attempts=0, deliberate_bypass_blocked=1)
    assert evidence.passed is True
    assert network_fence_result(unexpected_attempts=1, deliberate_bypass_blocked=1).passed is False
    assert network_fence_result(unexpected_attempts=0, deliberate_bypass_blocked=0).passed is False
