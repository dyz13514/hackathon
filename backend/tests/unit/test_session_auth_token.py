"""会话令牌编解码的单元测试（R23.12，tasks.md 1.5）。

这些是纯函数（无 I/O、无框架），因此可以把注意力全放在**伪造尝试**上：篡改载荷、
篡改签名、换密钥、换口令、过期、格式垃圾。每一条对应一种真实的攻击或运维事故。

签名密钥绑定口令指纹这一条尤其要有用例：它是「口令泄漏后一步失效全部会话」这个
运维答案的全部依据（`api/deps.py` 模块 docstring 第 3 条）。
"""

from __future__ import annotations

import base64
import json

import pytest
from pydantic import SecretStr

from app.api.deps import (
    CLOCK_SKEW_SECONDS,
    SESSION_SUBJECT,
    SESSION_TTL_SECONDS,
    SessionTokenError,
    issue_token,
    verify_shared_password,
    verify_token,
)
from app.settings import Settings

NOW = 1_740_000_000  # 固定时间戳，避免测试依赖真实时钟


def _payload_of(token: str) -> dict[str, object]:
    encoded = token.split(".")[0]
    padding = "=" * (-len(encoded) % 4)
    decoded = json.loads(base64.urlsafe_b64decode(encoded + padding))
    assert isinstance(decoded, dict)
    return decoded


def _retamper(token: str, payload: dict[str, object]) -> str:
    """替换载荷但保留原签名——最典型的伪造形态。"""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{encoded}.{token.split('.')[1]}"


def _rotated(
    base: Settings, *, password: str | None = None, secret_key: str | None = None
) -> Settings:
    """换掉口令或密钥后的配置。`model_copy` 保留其余字段，避免重读环境变量。"""
    update: dict[str, SecretStr] = {}
    if password is not None:
        update["session_shared_password"] = SecretStr(password)
    if secret_key is not None:
        update["session_secret_key"] = SecretStr(secret_key)
    return base.model_copy(update=update)


def test_issued_token_round_trips(settings: Settings) -> None:
    token, expires_at = issue_token(settings, now=NOW)
    claims = verify_token(settings, token, now=NOW)

    assert claims.subject == SESSION_SUBJECT
    assert int(claims.issued_at.timestamp()) == NOW
    assert int(claims.expires_at.timestamp()) == NOW + SESSION_TTL_SECONDS
    assert claims.expires_at == expires_at


def test_token_never_contains_the_password_or_the_secret_key(settings: Settings) -> None:
    """令牌是签名串，不是加密串：任何写进载荷的东西都是明文可读的。"""
    token, _ = issue_token(settings, now=NOW)

    assert settings.session_shared_password.get_secret_value() not in token
    assert settings.session_secret_key.get_secret_value() not in token
    assert set(_payload_of(token)) == {"v", "sub", "iat", "exp"}


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        (None, "MISSING"),
        ("", "MISSING"),
        ("no-separator", "MALFORMED"),
        ("a.b.c", "MALFORMED"),
        (".sig", "MALFORMED"),
        ("payload.", "MALFORMED"),
        ("!!!.!!!", "BAD_SIGNATURE"),
    ],
)
def test_garbage_tokens_are_rejected(
    settings: Settings, token: str | None, reason: str
) -> None:
    with pytest.raises(SessionTokenError) as raised:
        verify_token(settings, token, now=NOW)
    assert raised.value.reason == reason


def test_tampered_payload_is_rejected_before_being_interpreted(
    settings: Settings,
) -> None:
    """把有效期改到十年后，但签名还是原来的——必须报签名不符，而不是接受。"""
    token, _ = issue_token(settings, now=NOW)
    payload = _payload_of(token)
    payload["exp"] = NOW + 10 * 365 * 24 * 3600

    with pytest.raises(SessionTokenError) as raised:
        verify_token(settings, _retamper(token, payload), now=NOW)
    assert raised.value.reason == "BAD_SIGNATURE"


def test_tampered_signature_is_rejected(settings: Settings) -> None:
    encoded, signature = issue_token(settings, now=NOW)[0].split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]

    with pytest.raises(SessionTokenError) as raised:
        verify_token(settings, f"{encoded}.{flipped}", now=NOW)
    assert raised.value.reason == "BAD_SIGNATURE"


def test_expired_token_is_rejected_at_the_boundary(settings: Settings) -> None:
    token, _ = issue_token(settings, now=NOW)

    # 到期前一秒仍有效，到期当秒即失效——边界写死，避免「差一秒」的实现漂移。
    assert verify_token(settings, token, now=NOW + SESSION_TTL_SECONDS - 1)
    with pytest.raises(SessionTokenError) as raised:
        verify_token(settings, token, now=NOW + SESSION_TTL_SECONDS)
    assert raised.value.reason == "EXPIRED"


def test_token_from_the_future_beyond_clock_skew_is_rejected(settings: Settings) -> None:
    token, _ = issue_token(settings, now=NOW + 10 * CLOCK_SKEW_SECONDS)

    with pytest.raises(SessionTokenError) as raised:
        verify_token(settings, token, now=NOW)
    assert raised.value.reason == "NOT_YET_VALID"


def test_small_clock_skew_is_tolerated(settings: Settings) -> None:
    """部署机比浏览器机快几十秒，不该让刚签发的令牌失效。"""
    token, _ = issue_token(settings, now=NOW + CLOCK_SKEW_SECONDS)
    assert verify_token(settings, token, now=NOW).subject == SESSION_SUBJECT


def test_rotating_the_secret_key_invalidates_existing_tokens(settings: Settings) -> None:
    token, _ = issue_token(settings, now=NOW)
    rotated = _rotated(settings, secret_key="another-secret-key-that-is-long-enough-32")

    with pytest.raises(SessionTokenError) as raised:
        verify_token(rotated, token, now=NOW)
    assert raised.value.reason == "BAD_SIGNATURE"


def test_rotating_the_shared_password_invalidates_existing_tokens(
    settings: Settings,
) -> None:
    """这是「口令泄漏了怎么办」的全部答案：改环境变量重启，旧令牌立刻不认。"""
    token, _ = issue_token(settings, now=NOW)
    rotated = _rotated(settings, password="a-brand-new-password")

    with pytest.raises(SessionTokenError) as raised:
        verify_token(rotated, token, now=NOW)
    assert raised.value.reason == "BAD_SIGNATURE"


def test_shared_password_comparison(settings: Settings) -> None:
    correct = settings.session_shared_password.get_secret_value()

    assert verify_shared_password(settings, correct) is True
    assert verify_shared_password(settings, correct + "x") is False
    assert verify_shared_password(settings, correct[:-1]) is False
    assert verify_shared_password(settings, "") is False
    assert verify_shared_password(settings, correct.upper()) is False
