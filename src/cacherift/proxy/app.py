"""Read-only reverse proxy: forwards bytes untouched and records what went by."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    MutableMapping,
)
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from cacherift.core.freeze import freeze_json
from cacherift.core.models import CaptureRecord, DialectName, EndpointIdentity
from cacherift.io.redact import redact_headers, scrub_secrets, secret_fragments
from cacherift.proxy.recorder import CaptureRecorder
from cacherift.proxy.sse import SSEAccumulator

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

# RFC 7230 section 6.1: headers that apply to a single transport connection, never forwarded.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Hop-by-hop plus what this proxy recomputes or owns on the upstream leg. Accept-encoding is
# pinned to identity so the bytes forwarded to the client are the bytes parsed for events.
_NOT_FORWARDED_UPSTREAM = _HOP_BY_HOP | {"expect", "host", "content-length", "accept-encoding"}
_PROXY_HEADER_PREFIX = "x-cacherift-"
_SSE_CONTENT_TYPE = "text/event-stream"
# Dialects whose streams carry no terminal frame: for them the end of the stream is the end.
_EOF_TERMINATED = frozenset({"gemini_generate_content"})
_TIMEOUT = httpx.Timeout(None, connect=30.0)
_UNREACHABLE_TYPE = "cacherift_upstream_unreachable"
_PROVIDER_BY_HOST = {
    "api.openai.com": "openai",
    "api.anthropic.com": "anthropic",
    "api.deepseek.com": "deepseek",
    "generativelanguage.googleapis.com": "gemini",
}
_GEMINI_MODEL = re.compile(r"/models/([^:/]+):(?:stream)?[gG]enerateContent$")


def detect_dialect(path: str, upstream_host: str) -> str | None:
    """Dialect named by the request path, or None when the path is not a known model endpoint."""
    if path.endswith("/responses"):
        return "openai_responses"
    if path.endswith("/messages"):
        return "anthropic_messages"
    if path.endswith("/chat/completions"):
        return "deepseek_chat" if upstream_host == "api.deepseek.com" else "openai_chat"
    if _GEMINI_MODEL.search(path):
        return "gemini_generate_content"
    return None


def detect_provider(upstream_host: str) -> str:
    """Provider named by the upstream host; an unrecognized host stays ``unknown``."""
    return _PROVIDER_BY_HOST.get(upstream_host, "unknown")


def extract_model(parsed_body: Any, path: str) -> str:
    """Model from the request body, else from a Gemini style path, else ``unknown``."""
    if isinstance(parsed_body, Mapping):
        model = parsed_body.get("model")
        if isinstance(model, str) and model:
            return model
    match = _GEMINI_MODEL.search(path)
    return match.group(1) if match else "unknown"


def _decode_headers(raw: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name, value in raw:
        key = name.decode("latin-1").lower()
        text = value.decode("latin-1")
        headers[key] = f"{headers[key]}, {text}" if key in headers else text
    return headers


def _forwarded_response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    return [
        (name, value)
        for name, value in response.headers.raw
        if name.decode("latin-1").lower() not in _HOP_BY_HOP
    ]


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _response_id(response_body: Any) -> str | None:
    if not isinstance(response_body, Mapping):
        return None
    candidate = response_body.get("id")
    if isinstance(candidate, str):
        return candidate
    events = response_body.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, Mapping):
                continue
            for nested_key in ("response", "message"):
                nested = event.get(nested_key)
                if isinstance(nested, Mapping) and isinstance(nested.get("id"), str):
                    return str(nested["id"])
    return None


async def _read_body(receive: Receive) -> tuple[bytes, bool]:
    """The request body, and whether all of it arrived before the client went away."""
    chunks = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return bytes(chunks), False
        chunks += message.get("body", b"")
        if not message.get("more_body", False):
            return bytes(chunks), True


async def _await_upstream(
    pending: Awaitable[httpx.Response], disconnect: asyncio.Future[Message]
) -> httpx.Response | None:
    """Wait for the upstream response, or give up as soon as the client is gone (None)."""
    task = asyncio.ensure_future(pending)
    watchers: list[asyncio.Future[Any]] = [task, disconnect]
    await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
    if disconnect.done():
        disconnect.exception()  # an unexpected receive() failure must not vanish
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return None
    return task.result()


async def _iter_chunks(response: httpx.Response) -> AsyncGenerator[bytes]:
    if response.is_stream_consumed:
        # A transport may hand back an already-buffered response; there is nothing left to stream.
        if response.content:
            yield response.content
        return
    async for chunk in response.aiter_raw():
        yield chunk


async def _pump(
    response: httpx.Response,
    disconnect: asyncio.Future[Message],
    send: Send,
    take: Callable[[bytes], None],
) -> tuple[str, Exception | None]:
    """Forward upstream chunks until the stream ends, the client leaves, or the upstream dies.

    ``disconnect`` is the owner's pending ``receive()`` call; it finishes when the client goes.
    """
    chunks = _iter_chunks(response)
    try:
        while True:
            following = asyncio.ensure_future(chunks.__anext__())
            watchers: list[asyncio.Future[Any]] = [following, disconnect]
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                disconnect.exception()  # an unexpected receive() failure must not vanish
                following.cancel()
                await asyncio.gather(following, return_exceptions=True)
                return "client_disconnected", None
            try:
                chunk = following.result()
            except StopAsyncIteration:
                return "completed", None
            except httpx.HTTPError as exc:
                return "upstream_error", exc
            take(chunk)
            try:
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            except Exception:
                return "client_disconnected", None
    finally:
        await chunks.aclose()


class ProxyApp:
    """ASGI application forwarding every request to one upstream and recording model traffic."""

    def __init__(
        self,
        *,
        upstream: str,
        recorder: CaptureRecorder,
        transport: httpx.AsyncBaseTransport | None = None,
        dialect: str = "auto",
        provider: str | None = None,
        pricing_table_id: str | None = None,
    ) -> None:
        self.upstream = upstream.rstrip("/")
        self.recorder = recorder
        self._dialect = dialect
        self._provider = provider
        self._pricing_table_id = pricing_table_id
        self._host = urlsplit(self.upstream).hostname or ""
        self._client = httpx.AsyncClient(transport=transport, timeout=_TIMEOUT)

    async def aclose(self) -> None:
        """Release the upstream client and the capture file; safe to call more than once."""
        await self._client.aclose()
        self.recorder.close()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            raise RuntimeError(
                f"cacherift proxy does not handle ASGI scope type {scope['type']!r}"
            )
        await self._proxy(scope, receive, send)

    async def _lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _proxy(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = str(scope["method"])
        path = str(scope.get("path", ""))
        query = bytes(scope.get("query_string", b"") or b"")
        target = f"{self.upstream}{path}" + (f"?{query.decode('latin-1')}" if query else "")
        client_headers = _decode_headers(scope.get("headers", []))
        body, uploaded = await _read_body(receive)
        if not uploaded:
            return  # the client left mid-upload: there is no whole request to forward
        started = time.time()

        parsed = _parse_json(body) if body else None
        dialect = self._dialect if self._dialect != "auto" else detect_dialect(path, self._host)
        # The query string is forwarded but never recorded: Gemini style `?key=` lives there.
        recordable = isinstance(parsed, Mapping) and dialect is not None
        record_id = self.recorder.next_record_id() if recordable else ""

        async def record(
            *, status: int | None, response_body: Any, complete: bool
        ) -> None:
            if not recordable:
                return
            await self.recorder.write(
                self._capture(
                    record_id,
                    started,
                    path,
                    client_headers,
                    body,
                    parsed,
                    status=status,
                    response_body=response_body,
                    complete=complete,
                )
            )

        forwarded = {
            name: value
            for name, value in client_headers.items()
            if name not in _NOT_FORWARDED_UPSTREAM
            and not name.startswith(_PROXY_HEADER_PREFIX)
        }
        forwarded["accept-encoding"] = "identity"

        # Watching receive() from here on is what lets the server notice a closed client socket,
        # even while the upstream is still working on its response headers.
        disconnect = asyncio.ensure_future(receive())
        try:
            try:
                request = self._client.build_request(
                    method, target, content=body or None, headers=forwarded
                )
                response = await _await_upstream(
                    self._client.send(request, stream=True), disconnect
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                await _send_unreachable(send, exc)
                await record(status=None, response_body=None, complete=False)
                return
            if response is None:
                # The client gave up before the upstream answered; nothing reached the client.
                await record(status=None, response_body=None, complete=False)
                return

            raw = bytearray()
            accumulator = (
                SSEAccumulator(require_terminal_frame=dialect not in _EOF_TERMINATED)
                if response.headers.get("content-type", "").startswith(_SSE_CONTENT_TYPE)
                else None
            )

            def take(chunk: bytes) -> None:
                if accumulator is not None:
                    accumulator.feed(chunk)
                else:
                    raw.extend(chunk)

            outcome = "not_started"
            upstream_error: Exception | None = None
            try:
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": _forwarded_response_headers(response),
                    }
                )
                outcome, upstream_error = await _pump(response, disconnect, send, take)
            finally:
                await response.aclose()

            if accumulator is not None:
                accumulator.finish()
                response_body: Any = {"events": accumulator.events}
                finished = accumulator.complete
            else:
                response_body = _parse_json(bytes(raw))
                finished = isinstance(response_body, Mapping)
            secrets = secret_fragments(client_headers)
            if response_body is not None:
                response_body = scrub_secrets(response_body, secrets)

            if outcome == "completed":
                # Finish the client's response before touching the capture: capturing is an
                # observer and must never be why a delivered response gets truncated.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            await record(
                status=response.status_code,
                response_body=response_body,
                # Complete only when a whole, successful model response arrived.
                complete=outcome == "completed" and response.is_success and finished,
            )
        finally:
            disconnect.cancel()
            await asyncio.gather(disconnect, return_exceptions=True)

        if outcome == "upstream_error":
            # Sending the final empty body would fake a complete stream. Returning without it
            # makes the server drop the client connection, so the truncation is visible to it.
            sys.stderr.write(
                f"cacherift proxy: upstream stream failed mid-response "
                f"({type(upstream_error).__name__}); client connection aborted, "
                f"{record_id} recorded as incomplete\n"
            )
            return
        if outcome == "client_disconnected":
            sys.stderr.write(
                f"cacherift proxy: client disconnected mid-response; upstream stream closed, "
                f"{record_id} recorded as incomplete\n"
            )

    def _capture(
        self,
        record_id: str,
        started: float,
        path: str,
        client_headers: dict[str, str],
        body: bytes,
        parsed: Any,
        *,
        status: int | None,
        response_body: Any,
        complete: bool,
    ) -> CaptureRecord:
        dialect = self._dialect if self._dialect != "auto" else detect_dialect(path, self._host)
        return CaptureRecord(
            record_id=record_id,
            timestamp=started,
            identity=EndpointIdentity(
                dialect=cast(DialectName, dialect),
                provider=self._provider or detect_provider(self._host),
                model=extract_model(parsed, path),
                endpoint_path=path,
                pricing_table_id=self._pricing_table_id,
            ),
            # Headers as the client sent them, not as forwarded upstream.
            request_headers=freeze_json(redact_headers(client_headers)),
            captured_body_bytes=body,
            parsed_body=freeze_json(parsed),
            response_id=_response_id(response_body),
            response_body=freeze_json(response_body) if response_body is not None else None,
            response_complete=complete,
            # Only an explicit opt-in header groups requests; lineage stays otherwise natural.
            client_session_id=client_headers.get("x-cacherift-session-id"),
        )


async def _send_unreachable(send: Send, exc: Exception) -> None:
    payload = json.dumps(
        {"error": {"type": _UNREACHABLE_TYPE, "message": type(exc).__name__}}
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 502,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload, "more_body": False})
