"""Request body size limit, as raw ASGI middleware.

It sits just inside the error boundary and ahead of everything else the router does, so no
dependency (CSRF reading a form, a route parsing an upload) ever sees more than
``MAX_REQUEST_BYTES`` (default 10 MB plus 64 KB of form overhead):

* A declared ``Content-Length`` over the limit is answered with the custom 413 page BEFORE a byte
  of the body is read. A malformed ``Content-Length`` is a 400.
* A body with no (or a false) ``Content-Length``, such as a chunked upload, is counted as it
  streams. The moment the total passes the limit, reading stops and the 413 page is sent; the
  rest of the body is never pulled in. If the application had already started its response, the
  error is re-raised instead (a second response cannot be sent).

It applies to every HTTP request, anonymous ones too: the login form is bounded like any other.
The response carries ``Connection: close`` so the server does not wait for the unread body.

``path_limits`` gives a path (exact match) a limit of its own. It is for the one route that
legitimately takes a bigger body from a logged-in user (the screenshot upload); every other path,
anonymous ones included, keeps ``max_bytes``. A route with a bigger limit must not read the body
before its cheap gates (login, Origin, CSRF header, rate limit).
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.web.errors import error_response, new_reference


class BodyTooLarge(StarletteHTTPException):
    """Raised from ``receive`` once a streamed body passes the limit.

    An HTTP 413 exception on purpose: FastAPI turns any OTHER exception raised while it parses a
    form body into a 400, but lets an ``HTTPException`` through to the 413 error page.
    """

    def __init__(self) -> None:
        super().__init__(413, detail="request body too large", headers={"Connection": "close"})


class RequestBodyLimitMiddleware:
    def __init__(
        self, app: ASGIApp, max_bytes: int, path_limits: Mapping[str, int] | None = None
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.path_limits.get(scope["path"], self.max_bytes)
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            if not (declared.isascii() and declared.isdigit()):
                await error_response(400, new_reference())(scope, receive, send)
                return
            if int(declared) > limit:
                await self._too_large(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge
            return message

        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except BodyTooLarge:
            if started:
                raise
            await self._too_large(scope, receive, send)

    @staticmethod
    async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
        response = error_response(413, new_reference(), {"Connection": "close"})
        await response(scope, receive, send)
