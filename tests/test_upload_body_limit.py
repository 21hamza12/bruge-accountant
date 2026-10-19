"""Step 26: the request body limit covers multipart uploads to /uploads.

The limit itself is app/web/bodylimit.py (PR 3). These tests pin that an upload-sized multipart
body is cut off before any route or dependency sees it, whether the size is declared or streamed.
They put a spy in front of the router, so they hold whether or not the real route exists.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.responses import PlainTextResponse

from app.parsing.amadeus import MAX_FILE_BYTES
from app.settings import Settings
from tests.web_support import ORIGIN, FakeClock, WebDb, make_client

BOUNDARY = "testboundary7d4a"
MB = 1024 * 1024


def multipart(payload_bytes: int) -> tuple[bytes, dict[str, str]]:
    head = (
        f"--{BOUNDARY}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.xlsx"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    tail = f"\r\n--{BOUNDARY}--\r\n".encode()
    body = head + b"PK\x03\x04" + b"0" * (payload_bytes - 4) + tail
    return body, {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}", "Origin": ORIGIN}


@pytest.fixture
def spy_client(web_db: WebDb) -> Iterator[tuple[TestClient, list[int]]]:
    factory = make_client(web_db, FakeClock())
    app: FastAPI = factory.app  # type: ignore[attr-defined]
    reached: list[int] = []

    async def spy(file: UploadFile = File()) -> PlainTextResponse:  # noqa: B008
        reached.append(len(await file.read(MAX_FILE_BYTES + 1)))
        return PlainTextResponse("reached")

    # In front of every real route, so the spy answers /uploads whatever PR 4 registers later.
    app.router.routes.insert(0, APIRoute("/uploads", spy, methods=["POST"]))
    with factory() as client:
        yield client, reached


def test_the_default_limit_leaves_room_for_a_full_size_upload() -> None:
    assert Settings().max_request_bytes > MAX_FILE_BYTES


def test_an_eleven_megabyte_multipart_post_gets_the_413_page_and_the_route_never_runs(
    spy_client: tuple[TestClient, list[int]],
) -> None:
    client, reached = spy_client
    body, headers = multipart(11 * MB)
    response = client.post("/uploads", content=body, headers=headers)
    assert response.status_code == 413
    assert "Fichier trop volumineux" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["connection"] == "close"
    assert "content-security-policy" in response.headers
    assert reached == []


def test_a_streamed_eleven_megabyte_multipart_post_is_cut_off_too(
    spy_client: tuple[TestClient, list[int]],
) -> None:
    client, reached = spy_client
    body, headers = multipart(11 * MB)

    def chunks() -> Iterator[bytes]:  # no Content-Length: a chunked body
        for start in range(0, len(body), 256 * 1024):
            yield body[start : start + 256 * 1024]

    response = client.post("/uploads", content=chunks(), headers=headers)
    assert response.status_code == 413
    assert reached == []


def test_a_maximum_size_file_still_gets_through(spy_client: tuple[TestClient, list[int]]) -> None:
    client, reached = spy_client
    body, headers = multipart(MAX_FILE_BYTES)
    response = client.post("/uploads", content=body, headers=headers)
    assert response.status_code == 200
    assert reached == [MAX_FILE_BYTES]


def test_the_handler_never_reads_more_than_the_file_limit_plus_one(
    spy_client: tuple[TestClient, list[int]],
) -> None:
    # The spy mirrors the real handler's read; a body that slips under the request limit but over
    # the file limit (10 MB + a few KB of file, inside the 64 KB overhead) reads exactly +1.
    client, reached = spy_client
    body, headers = multipart(MAX_FILE_BYTES + 1000)
    response = client.post("/uploads", content=body, headers=headers)
    assert response.status_code == 200
    assert reached == [MAX_FILE_BYTES + 1]
