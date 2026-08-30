"""IP 的「建議建立／關聯裝置」：只給建議，按了才動手。

由來：DHCP 的筆電會散在十幾個 IP 上（同一個主機名稱、不同位址）。每一筆都手動建裝置、
手動關聯是純粹的苦工 —— 但要不要建立仍然是人決定，系統只負責把答案端到面前。

這裡守的是三件容易做錯的事：
1. **建議不可以有副作用**：GET 過之後資料庫不能有任何改變。
2. **對到多台就不猜**：同名裝置有兩台時不給建議（與 `ip_device_link` 的規則一致）。
3. **可見性要推進 SQL**：「還有幾筆同名 IP」不可以算進使用者看不到的資料。
"""

from __future__ import annotations

import inspect

from app.api.v1.endpoints import addresses


def _src(name: str) -> str:
    fn = getattr(addresses, name)
    return inspect.getsource(fn)


def test_suggestion_endpoint_exists_and_is_read_only():
    src = _src("device_suggestion")
    assert 'required="read"' in src or '"read"' in src, "建議端點沒有做讀取權限檢查"
    for writer in ("session.add(", "session.commit(", "append_audit(", "log_change("):
        assert writer not in src, (
            f"建議端點裡出現 {writer} —— 這支是「只給建議」，不可以有副作用"
        )


def test_suggestion_refuses_to_guess_when_several_devices_match():
    """同名裝置有多台時不給建議。猜錯的關聯比沒有關聯更難發現。"""
    src = _src("device_suggestion")
    assert "limit(2)" in src, "沒有多取一筆來判斷「是不是不只一台」"
    assert "len(row) == 1" in src, "沒有『只有一台才採用』的判斷"


def test_sibling_count_is_scoped_to_what_the_user_can_see():
    src = _src("_sibling_ip_ids")
    assert "visible_ids(" in src, "同名 IP 的計數沒有套可見性"
    assert "IPAddress.subnet_id.in_(vis)" in src, "可見性沒有推進 SQL（先取再過濾會算錯）"
    assert "device_id.is_(None)" in src, "把已經有裝置的 IP 也算進去了"


def test_apply_requires_write_and_admin_for_creation():
    src = _src("apply_device_suggestion")
    assert '"write"' in src, "套用端點沒有要求寫入權限"
    assert "user.is_admin" in src, (
        "建立裝置沒有限定管理員 —— POST /devices 是 require_admin，這條捷徑不能比它寬鬆"
    )


def test_apply_never_overwrites_an_existing_link():
    """既有關聯永不覆寫 —— 與 ip_device_link 的第一條規則一致。"""
    src = _src("apply_device_suggestion")
    assert "obj.device_id is None" in src, "沒有檢查『這個 IP 已經有裝置』"
    assert "sib.device_id is not None" in src, "批次關聯時沒有跳過已經有裝置的 IP"


def test_apply_writes_history_and_audit():
    src = _src("apply_device_suggestion")
    assert "log_change(" in src, "沒有寫 IP 異動記錄，事後查不出誰把它接上去的"
    assert "append_audit(" in src, "沒有寫稽核記錄"


def test_apply_is_either_or():
    """關聯既有／建立新的必須二擇一，兩個都給或都不給要擋下來。"""
    src = _src("apply_device_suggestion")
    assert "bool(payload.device_id) == bool(payload.create_name)" in src, (
        "沒有擋下『兩個都填』或『兩個都沒填』"
    )
