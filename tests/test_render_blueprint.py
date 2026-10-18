"""Static checks on the deploy files (render.yaml, Dockerfile, .dockerignore, CI).

Docker and Render are not reachable from the test run, so these pin the properties that matter
(region, health check, no plaintext secrets, non-root, signal handling). The image itself is
built and probed by the ``docker`` job in CI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SECRET_NAME = re.compile(r"password|secret|token|dsn|database_url|api_key|private", re.IGNORECASE)
CREDENTIAL_URL = re.compile(r"[a-z+]+://[^/\s:@]+:[^@\s]+@", re.IGNORECASE)


@pytest.fixture(scope="module")
def blueprint() -> dict[str, Any]:
    loaded = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.fixture(scope="module")
def web(blueprint: dict[str, Any]) -> dict[str, Any]:
    (service,) = blueprint["services"]
    assert isinstance(service, dict)
    return service


@pytest.fixture(scope="module")
def env_vars(web: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in web["envVars"]}


def test_web_service_basics(web: dict[str, Any]) -> None:
    assert web["type"] == "web"
    assert web["runtime"] == "docker"
    assert web["region"] == "frankfurt"
    assert web["plan"] == "starter"
    assert web["healthCheckPath"] == "/healthz"
    assert web["preDeployCommand"] == "python -m app init-db"


def test_database_is_private_and_in_the_same_region(blueprint: dict[str, Any]) -> None:
    (db,) = blueprint["databases"]
    assert db["region"] == "frankfurt"
    assert db["plan"] == "basic-256mb"
    assert db["ipAllowList"] == []


def test_production_environment_flags(env_vars: dict[str, dict[str, Any]]) -> None:
    assert env_vars["ENV"]["value"] == "production"
    assert env_vars["COOKIE_SECURE"]["value"] == "true"
    assert "ALLOWED_HOSTS" in env_vars
    assert "*" not in str(env_vars["ALLOWED_HOSTS"]["value"])
    assert "CORS_ALLOWED_ORIGINS" not in env_vars or "*" not in str(
        env_vars["CORS_ALLOWED_ORIGINS"].get("value", "")
    )


def test_secrets_are_not_in_the_file(env_vars: dict[str, dict[str, Any]]) -> None:
    assert env_vars["DATABASE_URL"] == {"key": "DATABASE_URL", "sync": False}
    assert env_vars["SENTRY_DSN"] == {"key": "SENTRY_DSN", "sync": False}
    assert env_vars["MIGRATION_DATABASE_URL"]["fromDatabase"]["property"] == "connectionString"


def test_no_secret_appears_in_plaintext(env_vars: dict[str, dict[str, Any]]) -> None:
    for key, item in env_vars.items():
        if SECRET_NAME.search(key):
            assert "value" not in item, f"{key} must come from the dashboard or fromDatabase"
        assert not CREDENTIAL_URL.search(str(item.get("value", ""))), key
    raw = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert not CREDENTIAL_URL.search(raw)


def test_migration_database_url_points_at_the_declared_database(
    blueprint: dict[str, Any], env_vars: dict[str, dict[str, Any]]
) -> None:
    (db,) = blueprint["databases"]
    assert env_vars["MIGRATION_DATABASE_URL"]["fromDatabase"]["name"] == db["name"]


def test_allowed_hosts_placeholder_is_loud_and_documented(
    env_vars: dict[str, dict[str, Any]],
) -> None:
    value = str(env_vars["ALLOWED_HOSTS"]["value"])
    assert "replace-me" in value.lower()
    assert "ALLOWED_HOSTS" in (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")


# --- Dockerfile -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_dockerfile_base_and_uv(dockerfile: str) -> None:
    assert re.search(r"^FROM python:3\.12-slim\b", dockerfile, re.MULTILINE)
    assert "ghcr.io/astral-sh/uv:" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile


def test_dockerfile_runs_as_non_root(dockerfile: str) -> None:
    users = re.findall(r"^USER\s+(\S+)", dockerfile, re.MULTILINE)
    assert users
    assert users[-1] not in {"root", "0"}


def test_dockerfile_cmd_is_shell_form_with_exec_and_the_required_flags(dockerfile: str) -> None:
    cmds = re.findall(r"^CMD\s+(.+)$", dockerfile, re.MULTILINE)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert not cmd.startswith("["), "shell form is needed so $PORT expands"
    assert cmd.startswith("exec uvicorn --factory app.web.main:create_app"), (
        "exec lets SIGTERM reach uvicorn; --factory avoids import-time side effects"
    )
    for fragment in (
        "--host 0.0.0.0",
        "--port ${PORT:-8000}",
        "--no-server-header",
        "--workers 1",
    ):
        assert fragment in cmd


def test_dockerfile_does_not_use_uvicorn_proxy_headers(dockerfile: str) -> None:
    # Uvicorn would trust only 127.0.0.1; app.web.proxy counts TRUSTED_PROXY_HOPS instead.
    assert "--proxy-headers" not in dockerfile
    assert "--forwarded-allow-ips" not in dockerfile


def test_trusted_proxy_hops_is_one_on_render_and_marked_for_confirmation(
    env_vars: dict[str, dict[str, Any]],
) -> None:
    assert env_vars["TRUSTED_PROXY_HOPS"]["value"] == "1"
    raw = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert re.search(r"TRUSTED_PROXY_HOPS\s+value: \"1\".*confirm", raw)


def test_dockerfile_copies_no_tests_or_venv(dockerfile: str) -> None:
    copies = re.findall(r"^COPY\s+(?:--\S+\s+)*(.+)$", dockerfile, re.MULTILINE)
    sources = " ".join(copies)
    assert "tests" not in sources
    assert not re.search(r"(^|\s)\.(\s|$)", sources), "COPY . would bypass the allowlist"
    assert "COPY . " not in dockerfile


def test_dockerfile_installs_nothing_extra(dockerfile: str) -> None:
    assert "apt-get install" not in dockerfile
    assert "curl | sh" not in dockerfile


def test_dockerignore_excludes_tests_venv_and_secrets() -> None:
    lines = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in (".git", ".venv", "tests", "docs", "__pycache__", ".env", "*.db"):
        assert required in lines, required


# --- CI ---------------------------------------------------------------------------------------


def test_ci_has_a_docker_job_that_probes_healthz() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = ci["jobs"]["docker"]
    assert "postgres" in job["services"]
    steps = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "docker build" in steps
    assert "init-db" in steps
    assert "/healthz" in steps
    assert "timeout" in steps.lower() or "deadline" in steps.lower()
    assert job.get("timeout-minutes")


def test_migration_0001_is_documented_as_frozen() -> None:
    text = (ROOT / "migrations" / "versions" / "0001_initial_schema.py").read_text(encoding="utf-8")
    assert "frozen" in text.lower()
    assert "never been deployed" not in text


def test_render_yaml_does_not_claim_the_owner_url_is_confined_to_pre_deploy() -> None:
    # Render env vars apply to the running service too, not only to preDeployCommand.
    raw = (ROOT / "render.yaml").read_text(encoding="utf-8")
    assert "used only by preDeployCommand" not in raw
    assert "running service" in raw


def test_runbook_documents_the_owner_url_and_the_no_real_data_rule() -> None:
    runbook = " ".join((ROOT / "docs" / "runbook.md").read_text(encoding="utf-8").split())
    assert "until pr 2 creates the `bruge_app` role" in runbook.lower()
    assert "`DATABASE_URL` is the owner URL" in runbook
    assert "no real agency data" in runbook.lower()
    assert "decision 2" in runbook.lower()


def test_only_the_cli_reads_the_owner_url() -> None:
    hits = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*.py")
        if "migration_database_url" in path.read_text(encoding="utf-8").lower()
    ]
    assert sorted(hits) == ["app/cli.py", "app/settings.py"]
