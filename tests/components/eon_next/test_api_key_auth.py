"""API-key authentication and secret redaction in the E.ON Next API client."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.eon_next.eonnext import (
    EonNext,
    EonNextApiError,
    EonNextAuthError,
)
from custom_components.eon_next.redact import (
    REDACTED,
    TO_REDACT,
    redact_secrets,
    redacted_error_messages,
)

API_KEY = "sk_live_AbCdEf0123456789xyz"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"


def _token_response(token: str = "fresh-jwt", refresh: str = "fresh-refresh") -> dict:
    now = 1_700_000_000
    return {
        "data": {
            "obtainKrakenToken": {
                "token": token,
                "refreshToken": refresh,
                "refreshExpiresIn": 10**10,
                "payload": {"iat": now, "exp": 10**10},
            }
        }
    }


def _valid_refresh_but_expired_access(api: EonNext) -> None:
    """A refresh token that looks valid locally, with no usable access token."""
    api.auth = {
        "issued": 1_000_000_000,
        "token": {"token": None, "expires": None},
        "refresh": {"token": "saved-refresh", "expires": 9_999_999_999},
    }


# --- login_with_api_key ---


@pytest.mark.asyncio
async def test_api_key_login_success_stores_tokens() -> None:
    api = EonNext()
    seen: list[str] = []
    api.set_token_update_callback(seen.append)
    api._graphql_post = AsyncMock(return_value=_token_response())  # type: ignore[method-assign]

    assert await api.login_with_api_key(API_KEY, initialise=False) is True

    assert api.api_key == API_KEY
    assert api.auth["token"]["token"] == "fresh-jwt"
    assert api.auth["refresh"]["token"] == "fresh-refresh"
    assert seen == ["fresh-refresh"]
    args = api._graphql_post.await_args.args
    assert args[0] == "apiKeyAuthentication"
    assert "obtainKrakenToken" in args[1]
    assert args[2] == {"input": {"APIKey": API_KEY}}
    # Unauthenticated call: the key is the credential.
    assert args[3] is False


@pytest.mark.asyncio
async def test_api_key_login_rejected_returns_false_and_logs_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = EonNext()
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "data": {"obtainKrakenToken": None},
            "errors": [{"message": f"Invalid data. APIKey: {API_KEY}"}],
        }
    )

    with caplog.at_level(logging.DEBUG, logger="custom_components.eon_next"):
        assert await api.login_with_api_key(API_KEY, initialise=False) is False

    assert api.auth["token"]["token"] is None
    assert API_KEY not in caplog.text
    assert "authentication failed" in caplog.text


@pytest.mark.asyncio
async def test_api_key_login_auth_error_returns_false() -> None:
    api = EonNext()
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        side_effect=EonNextAuthError("rejected")
    )
    assert await api.login_with_api_key(API_KEY, initialise=False) is False


@pytest.mark.asyncio
async def test_api_key_login_without_key_makes_no_request() -> None:
    api = EonNext()
    api._graphql_post = AsyncMock()  # type: ignore[method-assign]
    assert await api.login_with_api_key(initialise=False) is False
    api._graphql_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_login_is_gone() -> None:
    assert not hasattr(EonNext(), "login_with_username_and_password")


# --- token renewal order: access token -> refresh token -> API key ---


@pytest.mark.asyncio
async def test_rejected_refresh_token_falls_back_to_api_key() -> None:
    """A locally-valid refresh token rejected by E.ON re-logs in with the key."""
    api = EonNext()
    api.api_key = API_KEY
    _valid_refresh_but_expired_access(api)
    calls: list[str] = []

    async def _fake_post(operation: str, *_a: Any) -> dict:
        calls.append(operation)
        if operation == "refreshToken":
            return {"errors": [{"message": "Invalid data."}]}
        return _token_response()

    api._graphql_post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    token = await api._EonNext__auth_token()  # type: ignore[attr-defined]

    assert token == "fresh-jwt"
    assert calls == ["refreshToken", "apiKeyAuthentication"]


@pytest.mark.asyncio
async def test_refresh_token_401_falls_back_to_api_key() -> None:
    """A 401/403 on the refresh mutation (EonNextAuthError) also falls back."""
    api = EonNext()
    api.api_key = API_KEY
    _valid_refresh_but_expired_access(api)
    calls: list[str] = []

    async def _fake_post(operation: str, *_a: Any) -> dict:
        calls.append(operation)
        if operation == "refreshToken":
            raise EonNextAuthError("Authentication rejected by API")
        return _token_response()

    api._graphql_post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    assert await api._force_token_refresh() is True
    assert calls == ["refreshToken", "apiKeyAuthentication"]
    assert api.auth["token"]["token"] == "fresh-jwt"


@pytest.mark.asyncio
async def test_valid_refresh_token_does_not_use_api_key() -> None:
    api = EonNext()
    api.api_key = API_KEY
    _valid_refresh_but_expired_access(api)
    calls: list[str] = []

    async def _fake_post(operation: str, *_a: Any) -> dict:
        calls.append(operation)
        return _token_response()

    api._graphql_post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    await api._EonNext__auth_token()  # type: ignore[attr-defined]
    assert calls == ["refreshToken"]


@pytest.mark.asyncio
async def test_no_refresh_token_uses_api_key() -> None:
    api = EonNext()
    api.api_key = API_KEY
    calls: list[str] = []

    async def _fake_post(operation: str, *_a: Any) -> dict:
        calls.append(operation)
        return _token_response()

    api._graphql_post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    assert await api._EonNext__auth_token() == "fresh-jwt"  # type: ignore[attr-defined]
    assert calls == ["apiKeyAuthentication"]


@pytest.mark.asyncio
async def test_transient_refresh_error_does_not_fall_back() -> None:
    """A transient API error during refresh propagates; it must not re-login."""
    api = EonNext()
    api.api_key = API_KEY
    _valid_refresh_but_expired_access(api)
    calls: list[str] = []

    async def _fake_post(operation: str, *_a: Any) -> dict:
        calls.append(operation)
        raise EonNextApiError("server unavailable")

    api._graphql_post = AsyncMock(side_effect=_fake_post)  # type: ignore[method-assign]

    with pytest.raises(EonNextApiError, match="server unavailable"):
        await api._EonNext__auth_token()  # type: ignore[attr-defined]
    assert calls == ["refreshToken"]


@pytest.mark.asyncio
async def test_rejected_refresh_and_no_key_raises_auth_error() -> None:
    """Without a key, a rejected refresh token needs user action (re-auth)."""
    api = EonNext()
    _valid_refresh_but_expired_access(api)
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        return_value={"errors": [{"message": "Invalid data."}]}
    )

    with pytest.raises(EonNextAuthError):
        await api._EonNext__auth_token()  # type: ignore[attr-defined]
    assert api._graphql_post.await_count == 1


@pytest.mark.asyncio
async def test_account_numbers() -> None:
    api = EonNext()
    api.auth["token"] = {"token": "jwt", "expires": 10**10}
    api._graphql_post = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "data": {
                "viewer": {
                    "accounts": [
                        {"number": "A-2", "balance": 1},
                        {"number": "A-1", "balance": 2},
                    ]
                }
            }
        }
    )
    assert await api.async_get_account_numbers() == ["A-2", "A-1"]


# --- redaction ---


@pytest.mark.parametrize(
    "raw",
    [
        f"Invalid value {API_KEY}",
        f'{{"APIKey": "{API_KEY}"}}',
        f"{{'input': {{'APIKey': '{API_KEY}'}}}}",
        f"api_key={API_KEY}",
        "refreshToken: 3c2b1a-not-a-jwt-refresh",
        'refresh_token="opaque-refresh-value"',
        "token=opaque-token-value",
        f"authorization: JWT {JWT}",
        f"Bearer {JWT}",
        f"jwt {JWT} expired",
        "access_token=opaque-token-value",
        "x-api-key: opaque-token-value",
        "secret_key=opaque-refresh-value",
        "liveSecretKey: 'opaque-token-value'",
    ],
)
def test_redact_secrets_masks_keys_and_tokens(raw: str) -> None:
    out = redact_secrets(raw)
    for secret in (
        API_KEY,
        JWT,
        "3c2b1a-not-a-jwt-refresh",
        "opaque-refresh-value",
        "opaque-token-value",
        "sk_live_",
    ):
        assert secret not in out
    assert REDACTED in out


def test_redact_secrets_masks_email_and_keeps_plain_text() -> None:
    assert "user@example.com" not in redact_secrets("login user@example.com failed")
    assert redact_secrets("Invalid data.") == "Invalid data."
    assert redact_secrets(EonNextApiError("status 503")) == "status 503"


def test_redacted_error_messages() -> None:
    assert redacted_error_messages(None) == "Unknown error"
    assert redacted_error_messages([]) == "Unknown error"
    assert redacted_error_messages([{"message": f"bad {API_KEY}"}]) == [
        f"bad {REDACTED}"
    ]


def test_to_redact_covers_entry_and_payload_keys() -> None:
    for key in ("api_key", "APIKey", "refresh_token", "refreshToken", "token", "password"):
        assert key in TO_REDACT


@pytest.mark.asyncio
async def test_graphql_error_logs_are_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GraphQL errors echoing the key back are logged without it."""

    class _Resp:
        status = 200

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def json(self, *_a: Any, **_k: Any) -> Any:
            return {"errors": [{"message": f"Field 'APIKey' value '{API_KEY}' bad"}]}

    class _Session:
        closed = False

        def post(self, *_a: Any, **_k: Any) -> _Resp:
            return _Resp()

    api = EonNext()
    api._get_session = AsyncMock(return_value=_Session())  # type: ignore[method-assign]

    with caplog.at_level(logging.DEBUG, logger="custom_components.eon_next"):
        assert await api.login_with_api_key(API_KEY, initialise=False) is False

    assert API_KEY not in caplog.text
    assert "sk_live_" not in caplog.text


@pytest.mark.asyncio
async def test_non_json_graphql_body_is_api_error() -> None:
    """An HTML error page must raise EonNextApiError, not a raw ValueError."""
    import json as _json

    class _Resp:
        status = 502

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def json(self, *_a: Any, **_k: Any) -> Any:
            raise _json.JSONDecodeError("Expecting value", "<html>", 0)

        async def text(self) -> str:
            return "<html>Bad gateway</html>"

    class _Session:
        closed = False

        def post(self, *_a: Any, **_k: Any) -> _Resp:
            return _Resp()

    api = EonNext()
    api._get_session = AsyncMock(return_value=_Session())  # type: ignore[method-assign]

    with pytest.raises(EonNextApiError):
        await api.login_with_api_key(API_KEY, initialise=False)
