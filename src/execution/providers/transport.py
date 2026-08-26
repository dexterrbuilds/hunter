"""Reusable HTTP transport for transaction providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

HTTP_SERVER_ERROR_MINIMUM = 500


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """Provider HTTP response without credential-bearing request data."""

    status: int
    payload: dict[str, Any]
    headers: dict[str, str]
    connection_reused: bool | None = None
    session_generation: int | None = None
    session_created_for_request: bool | None = None


class JsonRpcTransport(Protocol):
    """Injectable transport used by offline adapter tests."""

    async def post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5_000,
    ) -> TransportResponse: ...

    async def warm(self, endpoint: str, *, timeout_ms: int = 2_000) -> bool: ...

    async def close(self) -> None: ...


class AioHttpJsonRpcTransport:
    """TLS-verifying keepalive transport shared across provider submissions."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._session_generation = 0

    async def _get_session(self) -> tuple[aiohttp.ClientSession, bool]:
        created = False
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(connector=connector)
            self._session_generation += 1
            created = True
        return self._session, created

    async def post_json(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        timeout_ms: int = 5_000,
    ) -> TransportResponse:
        session, session_created = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1_000)
        async with session.post(
            endpoint,
            json=payload,
            headers={"Content-Type": "application/json", **(headers or {})},
            timeout=timeout,
        ) as response:
            raw_body = await response.text()
            try:
                body = json.loads(raw_body)
            except json.JSONDecodeError:
                body = {"error": {"message": raw_body or "non-JSON response"}}
            payload_dict = body if isinstance(body, dict) else {"result": body}
            return TransportResponse(
                response.status,
                payload_dict,
                dict(response.headers),
                connection_reused=not session_created,
                session_generation=self._session_generation,
                session_created_for_request=session_created,
            )

    async def warm(self, endpoint: str, *, timeout_ms: int = 2_000) -> bool:
        """Warm DNS/TCP/TLS state using a provider-documented ping endpoint."""
        session, _session_created = await self._get_session()
        timeout = aiohttp.ClientTimeout(total=timeout_ms / 1_000)
        async with session.get(endpoint, timeout=timeout) as response:
            await response.read()
            return response.status < HTTP_SERVER_ERROR_MINIMUM

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
