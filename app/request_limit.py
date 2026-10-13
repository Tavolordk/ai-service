"""Protege endpoints inline ANTES de que FastAPI construya el objeto JSON en RAM."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable


class InlineBodyLimit:
    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") not in {
            "/api/v1/intelligence/chat", "/api/v1/intelligence/chat/stream",
        }:
            await self.app(scope, receive, send)
            return

        async def reject():
            payload = json.dumps({"detail": f"JSON inline excede {self.max_bytes} bytes; use POST /api/v1/cases?caseId=... y luego /api/v1/cases/{{id}}/chat"}).encode("utf-8")
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(payload)).encode("ascii"))]})
            await send({"type": "http.response.body", "body": payload})

        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = 0
        if length > self.max_bytes:
            await reject()
            return
        read = 0

        async def bounded_receive():
            nonlocal read
            msg = await receive()
            if msg.get("type") == "http.request":
                read += len(msg.get("body", b""))
                if read > self.max_bytes:
                    raise _BodyTooLarge()
            return msg

        try:
            await self.app(scope, bounded_receive, send)
        except _BodyTooLarge:
            # Respuesta aún no iniciada cuando FastAPI lee cuerpo de solicitud.
            await reject()


class _BodyTooLarge(Exception):
    pass
