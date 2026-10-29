import os
import sys

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, SRC_DIR)

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import http_auth


TOKEN = "test-token-0123456789abcdef"


def build_app():
    async def ok(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    return http_auth.BearerTokenMiddleware(app, TOKEN)


def test_missing_token_is_rejected():
    client = TestClient(build_app())
    response = client.get("/")
    assert response.status_code == 401, response.status_code
    assert response.headers["www-authenticate"] == 'Bearer realm="oss-mcp"', response.headers
    assert response.json()["error"] == "unauthorized", response.text


def test_wrong_token_is_rejected():
    client = TestClient(build_app())
    response = client.get("/", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401, response.status_code


def test_valid_token_is_accepted():
    client = TestClient(build_app())
    response = client.get("/", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200, response.status_code
    assert response.text == "ok", response.text


def test_token_validation_rejects_weak_values():
    for value in ("", "short", "has whitespace 0123456789", None, 1234567890123456):
        try:
            http_auth.validate_bearer_token(value)
        except ValueError:
            continue
        raise AssertionError(f"should reject token: {value!r}")

    assert http_auth.validate_bearer_token(f"  {TOKEN}  ") == TOKEN


def test_local_host_without_token_is_allowed():
    assert http_auth.prepare_http_auth("127.0.0.1", None) is None
    assert http_auth.prepare_http_auth("localhost", "") is None
    assert http_auth.prepare_http_auth("::1", None) is None


def test_external_host_requires_token():
    try:
        http_auth.prepare_http_auth("0.0.0.0", None)
    except ValueError as exc:
        assert "Bearer Token" in str(exc), exc
    else:
        raise AssertionError("external host without token must be rejected")

    assert http_auth.prepare_http_auth("0.0.0.0", TOKEN) == TOKEN
    assert http_auth.prepare_http_auth("127.0.0.1", TOKEN) == TOKEN


def test_token_precedence():
    cli_token = "cli-token-0123456789abcdef"
    env_token = "env-token-0123456789abcdef"
    config_token = "config-token-0123456789abcdef"
    args = ("0.0.0.0", cli_token, env_token, config_token)

    assert http_auth.resolve_http_auth(*args) == cli_token
    assert http_auth.resolve_http_auth("0.0.0.0", None, env_token, config_token) == env_token
    assert http_auth.resolve_http_auth("0.0.0.0", "", "", config_token) == config_token


def test_non_ascii_token_is_rejected():
    for value in ("令牌" * 8, "token-令牌-0123456789"):
        try:
            http_auth.validate_bearer_token(value)
        except ValueError:
            continue
        raise AssertionError(f"should reject non-ASCII token: {value!r}")


test_missing_token_is_rejected()
test_wrong_token_is_rejected()
test_valid_token_is_accepted()
test_token_validation_rejects_weak_values()
test_local_host_without_token_is_allowed()
test_external_host_requires_token()
test_token_precedence()
test_non_ascii_token_is_rejected()
print("HTTP AUTH TESTS PASSED")
