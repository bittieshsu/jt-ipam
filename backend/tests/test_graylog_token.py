"""公開端點（Graylog DSV、機櫃嵌入圖）存取權杖比對的安全回歸測試（不需 DB / 不需真 server）。

這些是公開、未登入的端點，token 是唯一守門，因此比對必須：
- 常數時間（hmac.compare_digest）以免 timing side-channel 洩漏 token
- 對任意輸入（含非 ASCII / NUL）安全，不可丟例外 → 500
- expected 為空一律拒絕
- 過期的權杖拒絕（2026-10-09 起權杖會到期）
"""
from datetime import UTC, datetime, timedelta

from app.services.system_config import public_token_problem


def _ok(supplied, expected, exp=None) -> bool:  # noqa: ANN001
    return public_token_problem({"token": expected, "token_expires_at": exp}, supplied) is None


def test_correct_token_matches():
    assert _ok("s3cr3t-token", "s3cr3t-token") is True


def test_wrong_token_rejected():
    assert _ok("s3cr3t-token", "s3cr3t-tokeX") is False
    assert _ok("", "s3cr3t-token") is False
    assert _ok("short", "s3cr3t-token") is False


def test_empty_expected_always_rejected():
    # token 未設定（空）時，任何輸入都不得放行
    assert _ok("anything", "") is False
    assert _ok("anything", None) is False
    assert _ok("", "") is False


def test_malicious_non_ascii_token_does_not_raise():
    # 惡意 token 含非 ASCII / NUL 不可讓 hmac.compare_digest 丟 TypeError → 500
    for bad in ["\x00\xff", "é\x00è", "🔥", "\udcff"]:
        assert _ok(bad, "s3cr3t-token") is False


def test_expired_token_is_rejected():
    past = datetime.now(UTC) - timedelta(seconds=1)
    assert public_token_problem({"token": "t", "token_expires_at": past}, "t") == "expired"
    future = datetime.now(UTC) + timedelta(days=1)
    assert public_token_problem({"token": "t", "token_expires_at": future}, "t") is None


def test_uses_constant_time_compare():
    # 確認實作走 hmac.compare_digest（常數時間），而非 str ==
    import inspect

    src = inspect.getsource(public_token_problem)
    assert "compare_digest" in src
