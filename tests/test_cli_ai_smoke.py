"""``python -m app ai-smoke``: one tiny call proves the configured key works in production.

The model client is always a fake here: no test reaches the network.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import cli
from app.ai.chat_client import ChatModelClient, ChatRequest
from app.ai.client import AiUnavailable
from app.ai.fake import FakeChatClient, fake_text

KEY = "sk-ant-api03-TOPSECRETKEYVALUE"
ANSWER = "the model said something private"


@pytest.fixture(autouse=True)
def key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ENV", "RENDER", "AI_ENABLED", "AI_CHAT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)


def use_client(monkeypatch: pytest.MonkeyPatch, client: ChatModelClient) -> list[str]:
    built: list[str] = []

    def build(key: str, *, timeout_s: float, max_retries: int) -> ChatModelClient:
        built.append(key)
        return client

    monkeypatch.setattr(cli, "AnthropicChatClient", build)
    return built


def test_ok_prints_model_and_token_counts_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeChatClient([fake_text(ANSWER, input_tokens=41, output_tokens=7)])
    built = use_client(monkeypatch, fake)
    assert cli.main(["ai-smoke"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "ai-smoke: ok model=claude-haiku-5-5 input_tokens=41 output_tokens=7"
    assert out.err == ""
    assert built == [KEY]
    for text in (out.out, out.err):
        assert KEY not in text
        assert ANSWER not in text


def test_one_small_synthetic_call_with_no_tenant_data(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeChatClient([fake_text("ok")])
    use_client(monkeypatch, fake)
    assert cli.main(["ai-smoke"]) == 0
    (request,) = fake.requests
    assert isinstance(request, ChatRequest)
    assert request.model == "claude-haiku-5-5"
    assert request.messages == [{"role": "user", "content": "ping"}]
    assert request.tool_choice == {"type": "none"}
    assert request.max_tokens <= 256
    assert len(request.tools) == 1 and request.tools[0]["name"] == "noop"


def test_it_uses_the_configured_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_CHAT_MODEL", "claude-sonnet-5-5")
    fake = FakeChatClient([fake_text("ok")])
    use_client(monkeypatch, fake)
    assert cli.main(["ai-smoke"]) == 0
    assert fake.requests[0].model == "claude-sonnet-5-5"


@pytest.mark.parametrize("kind", ["auth", "timeout", "connection", "rate_limit", "server"])
def test_a_failed_call_prints_failed_and_the_kind_only(
    kind: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_client(monkeypatch, FakeChatClient([AiUnavailable(kind)]))
    assert cli.main(["ai-smoke"]) == 1
    out = capsys.readouterr()
    assert f"ai-smoke: failed kind={kind} model=claude-haiku-5-5" in out.err
    assert out.out == ""
    assert KEY not in out.err


def test_no_key_fails_without_building_a_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    built = use_client(monkeypatch, FakeChatClient([]))
    assert cli.main(["ai-smoke"]) == 1
    assert built == []
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_ai_switched_off_fails_without_a_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AI_ENABLED", "false")
    built = use_client(monkeypatch, FakeChatClient([]))
    assert cli.main(["ai-smoke"]) == 1
    assert built == []
    assert "AI_ENABLED=false" in capsys.readouterr().err


def test_production_refuses_a_foreign_base_url_and_never_echoes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw-secret@db.internal/x")
    monkeypatch.setenv("ALLOWED_HOSTS", "example.org")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example/v1")
    built = use_client(monkeypatch, FakeChatClient([]))
    assert cli.main(["ai-smoke"]) == 1
    err = capsys.readouterr().err
    assert built == []
    assert "ANTHROPIC_BASE_URL must not be set in production" in err
    assert "evil.example" not in err
    assert "pw-secret" not in err


def test_it_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:////nonexistent/dir/none.db")
    use_client(monkeypatch, FakeChatClient([fake_text("ok")]))
    assert cli.main(["ai-smoke"]) == 0


def test_the_real_client_is_built_with_the_chat_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the construction is checked: the spy replaces the SDK, nothing is sent."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class Spy:
        def __init__(self, key: str, **kwargs: Any) -> None:
            calls.append((key, kwargs))

        def complete(self, request: ChatRequest) -> Any:
            return fake_text("ok")

    monkeypatch.setattr(cli, "AnthropicChatClient", Spy)
    monkeypatch.setenv("ANTHROPIC_API_KEY", f"  {KEY}  ")
    assert cli.main(["ai-smoke"]) == 0
    assert calls == [(KEY, {"timeout_s": 20.0, "max_retries": 0})]
