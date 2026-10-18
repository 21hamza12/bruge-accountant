"""Rendering helper for full HTML pages."""

from __future__ import annotations

from typing import Any

from starlette.responses import HTMLResponse

from app.web.templating import render


def page(
    template: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context: Any,
) -> HTMLResponse:
    return HTMLResponse(render(template, **context), status_code=status_code, headers=headers)
