"""FreeRDP 引擎的接線。

這裡守的是「換了引擎之後畫面是不是還送得出去」與「缺套件時講不講得清楚」——
兩者壞掉都不會報錯，只會讓使用者看到一片空白或一句沒有內容的「連線失敗」。
"""

from __future__ import annotations

from app.services.rdp_freerdp import REQUIRED_BINARIES, REQUIRED_MODULES, VideoTile, availability


def test_video_tile_has_the_fields_the_bridge_sends():
    """`_bridge` 直接讀 `.x/.y/.width/.height/.data` —— 欄位名改了畫面就不會出現。"""
    t = VideoTile(x=1, y=2, width=3, height=4, data=b"png")
    assert (t.x, t.y, t.width, t.height, t.data) == (1, 2, 3, 4, b"png")


def test_bridge_accepts_a_tile_without_a_type_field():
    """FreeRDP 的 tile 沒有 aardwolf 的 `type` 欄位。

    原本的判斷是 `data.type == RDPDATATYPE.VIDEO`，FreeRDP 的畫面會全部被丟掉，
    而且一聲不響 —— 使用者只會看到永遠空白的畫面。
    """
    from app.api.v1.endpoints.rdp_console import _is_video

    assert _is_video(VideoTile(x=0, y=0, width=8, height=8, data=b"x")) is True


def test_bridge_still_rejects_non_video():
    from app.api.v1.endpoints.rdp_console import _is_video

    class _Empty:
        data = b""

    assert _is_video(_Empty()) is False
    assert _is_video(None) is False


def test_availability_names_what_is_missing():
    """缺東西時要講得出缺哪一個套件，設定頁才說得出「怎麼裝」。"""
    av = availability()
    assert set(av) == {"ok", "missing_packages", "missing_modules"}
    if not av["ok"]:
        assert av["missing_packages"] or av["missing_modules"]
    # 套件名是要印給人照著 apt install 的，不能是空字串
    assert all(REQUIRED_BINARIES.values())
    assert all(REQUIRED_MODULES.values())


def test_engine_available_reports_per_engine(monkeypatch):
    """『RDP 能不能用』是逐引擎的問題。

    以前只看 aardwolf 這個全域旗標；只裝了 FreeRDP 的機器會被整個關掉功能。
    """
    from app.api.v1.endpoints import rdp_console

    monkeypatch.setattr(rdp_console, "RDP_AVAILABLE", False)
    ok, missing = rdp_console.engine_available("aardwolf")
    assert ok is False and "aardwolf" in missing

    monkeypatch.setattr("app.services.rdp_freerdp.availability",
                        lambda: {"ok": True, "missing_packages": [], "missing_modules": []})
    assert rdp_console.engine_available("freerdp") == (True, "")


def test_freerdp_password_never_reaches_argv():
    """密碼只能走 stdin。

    `/p:<password>` 會讓本機**任何**使用者從 `ps` 讀到目標主機的密碼。
    這是靜態檢查：原始碼裡不該出現把密碼接進參數列的寫法。
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "rdp_freerdp.py"
    text = src.read_text()
    assert "/from-stdin:force" in text, "沒有走 stdin 的密碼輸入"
    assert 'f"/p:' not in text and '"/p:"' not in text, "密碼被放進 argv 了"


def test_terminate_is_safe_before_connect():
    """連線還沒建立就被收掉（使用者馬上關掉分頁）不可以炸。"""
    import asyncio

    from app.services.rdp_freerdp import FreeRdpConnection

    conn = FreeRdpConnection(host="192.0.2.1", port=3389, username="u",
                             password="p", domain=None, width=800, height=600)
    asyncio.run(conn.terminate())
    asyncio.run(conn.terminate())      # 第二次也要安靜地過


def test_clipboard_is_off_unless_the_admin_enabled_it():
    """FreeRDP 的剪貼簿重導**預設是開的** —— 這個引擎必須顯式關掉它。

    管理者把「控制端貼上」關掉，是明確關閉了一條資料通道。換引擎時把它默默打開，
    等於用一個「效能／相容性」的選擇改變了資安姿態，而且畫面上看不出來。
    """
    from app.services.rdp_freerdp import FreeRdpConnection

    off = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                            domain=None, width=800, height=600, clip_enabled=False)
    on = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                           domain=None, width=800, height=600, clip_enabled=True)
    assert off._clip_enabled is False
    assert on._clip_enabled is True
    # 預設值就是關的：呼叫端忘了傳也不會意外打開
    dflt = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                             domain=None, width=800, height=600)
    assert dflt._clip_enabled is False


async def test_pasting_is_refused_when_disabled():
    """沒開就不該默默吞掉 —— 要講出原因，否則使用者會以為貼上成功了。"""
    import pytest

    from app.services.rdp_freerdp import FreeRdpConnection, FreeRdpError

    conn = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                             domain=None, width=800, height=600, clip_enabled=False)
    conn._display = ":99"
    with pytest.raises(FreeRdpError):
        await conn.set_current_clipboard_text("hello")


def test_the_engine_does_not_need_aardwolf_for_mouse_buttons():
    """滑鼠按鍵不可以綁在 aardwolf 的列舉上。

    否則「用 FreeRDP 引擎」仍然得先裝 aardwolf，兩個引擎等於沒有真的分開 ——
    而 FreeRDP 存在的理由，正是給 aardwolf 連不上的環境用。
    """
    from app.api.v1.endpoints.rdp_console import _mouse_button

    for b, name in ((0, "MOUSEBUTTON_LEFT"), (1, "MOUSEBUTTON_RIGHT"), (2, "MOUSEBUTTON_MIDDLE")):
        btn = _mouse_button(b)
        assert btn.name == name
        assert type(btn).__module__.startswith("app."), "按鍵型別來自 aardwolf"


def test_children_are_set_to_die_with_the_backend():
    """三個子行程都要掛上 PDEATHSIG。

    正常收線走 `terminate()`；但後端被 SIGKILL 帶走時（OOM、`systemctl kill`、
    部署腳本出手）那段不會執行，Xvfb／xfreerdp／ffmpeg 會變成孤兒繼續佔記憶體。
    它們不吵不鬧，所以沒人會發現，直到機器被一堆看不出來歷的 Xvfb 吃光。

    實測（2026-09-17）：父行程 SIGKILL 後三個子行程都消失、零殘留。
    這裡守的是「三個都掛上了」—— 少掛一個不會有任何徵兆。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert src.count("preexec_fn=_die_with_parent") == 3, "有子行程沒有掛上 PDEATHSIG"
    assert "PR_SET_PDEATHSIG" in src


def test_terminate_kills_in_the_right_order():
    """先收畫面擷取與 RDP，最後才收 Xvfb。

    反過來的話，xfreerdp 與 ffmpeg 會對著一個不存在的顯示噴一堆錯誤才死。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert "for proc in (self._grab, self._rdp, self._xvfb):" in src
