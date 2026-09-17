"""FreeRDP 引擎的接線。

這裡守的是「換了引擎之後畫面是不是還送得出去」與「缺套件時講不講得清楚」——
兩者壞掉都不會報錯，只會讓使用者看到一片空白或一句沒有內容的「連線失敗」。
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest
from app.services.rdp_freerdp import (
    REQUIRED_BINARIES,
    REQUIRED_MODULES,
    FreeRdpConnection,
    VideoTile,
    availability,
)


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


def test_display_number_is_not_read_from_a_pipe():
    """不可以用 `-displayfd` 從管線讀顯示編號。

    uvicorn 預設跑在 uvloop 上，而 `connect_read_pipe` 在那裡讀到的是立即 EOF ——
    我們拿到空字串、Xvfb 卻還活著，錯誤只剩「啟動失敗（結束碼 None）」。
    用標準 asyncio 跑獨立腳本完全測不出來（2026-09-17 正式環境）。

    改走 X 自己的仲裁：鎖檔 + 等 socket 出現，純檔案檢查，與事件迴圈無關。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    # 只看實際呼叫：註解裡解釋「為什麼不用」是應該留著的
    code = "\n".join(ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#"))
    assert '"-displayfd"' not in code, "又用回管線讀編號了 —— 那在 uvloop 下會壞"
    assert "connect_read_pipe(" not in code
    assert "os.pipe()" not in code
    assert "_X11_LOCK_PREFIX" in src, "沒有用鎖檔挑顯示編號"


def test_a_taken_display_number_moves_on_to_the_next():
    """編號被佔走時要換下一個，不可以直接失敗。

    同一台機器上可能同時有好幾條 RDP 連線（也可能有別的 X 伺服器）。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert "for num in range(_DISPLAY_MIN, _DISPLAY_MAX)" in src
    assert "await self._kill_xvfb()" in src, "試失敗的那個 Xvfb 沒有收掉 → 會留下孤兒"


def test_children_are_cleaned_up_without_forking_in_a_thread():
    """子行程不可以用 `preexec_fn` 收尾，孤兒交給 systemd 的 cgroup。

    正常收線走 `terminate()`；後端被 SIGKILL 帶走時（OOM、`systemctl kill`、部署腳本
    出手）那段不會執行，Xvfb／xfreerdp／ffmpeg 會變成孤兒繼續佔記憶體 —— 它們不吵不鬧，
    直到機器被一堆看不出來歷的 Xvfb 吃光。

    這件事**曾經**用 `preexec_fn` 設 `PR_SET_PDEATHSIG`，但在多執行緒行程裡 fork 慢得
    離譜：同一台機器單執行緒建立連線 2.85 秒，有 8 個工作執行緒時 14.76 秒 —— uvicorn
    的 worker 本來就有執行緒，直接衝破連線逾時（2026-09-17 正式環境「連線逾時」）。
    改由 systemd 的 `KillMode=mixed` 對 cgroup 內剩餘行程送 SIGKILL，同一件事零成本。

    所以這裡守兩件事：原始碼不可以又把 `preexec_fn` 加回來，單元檔要有 `KillMode=mixed`。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "backend" / "app" / "services" / "rdp_freerdp.py").read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert "preexec_fn" not in code, "又用回 preexec_fn 了 —— 那會讓連線在有負載時逾時"

    unit = (root / "deploy" / "systemd" / "jt-ipam-backend.service").read_text()
    assert "KillMode=mixed" in unit, "沒有 KillMode=mixed，後端被 SIGKILL 後會留下孤兒 Xvfb"


def test_terminate_kills_in_the_right_order():
    """先收畫面擷取與 RDP，最後才收 Xvfb。

    反過來的話，xfreerdp 與 ffmpeg 會對著一個不存在的顯示噴一堆錯誤才死。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert "for proc in (self._grab, self._rdp, self._xvfb):" in src


def test_the_x11_socket_directory_is_created():
    """`/tmp/.X11-unix` 不存在時要自己建。

    X 的 unix socket 路徑是協定寫死的，而 **Xvfb 以非 root 身分不會自己建那個目錄**
    （`_XSERVTransmkdir: ERROR: euid != 0`），然後安靜地失敗、連顯示編號都不回報。

    開發機上看不到這個問題，因為那個目錄早就在了。正式環境的 systemd 單元帶
    `PrivateTmp=yes` —— 服務拿到的是全新的空 /tmp，於是第一次連線就失敗
    （2026-09-17 實際發生）。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert "_ensure_x11_socket_dir()" in src, "沒有在啟動 Xvfb 前確保 socket 目錄存在"
    assert "makedirs(_X11_SOCKET_DIR" in src
    # 既有目錄的權限不該被我們動：沒有 PrivateTmp 的機器上那是共用的
    assert "os.chmod(_X11_SOCKET_DIR" not in src, "不要改既有 X11 socket 目錄的權限"


def test_xvfb_failures_carry_its_own_words():
    """Xvfb 起不來時要把它自己說的話帶出來。

    只說「顯示編號看不懂」會讓人去查我們的解析，真正的原因在 Xvfb 的 stderr 裡。

    ⚠️ stderr 要寫**檔案**不是管線：失敗時管線可能已經關掉或還沒有東西可讀，
    於是原因就消失了（2026-09-17 正式環境上只剩一句「啟動失敗」，查不下去）。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert "找不到可用的虛擬顯示" in src
    assert "stderr=self._xvfb_err" in src, "Xvfb 的 stderr 沒有導進檔案"
    assert "_xvfb_stderr_tail" in src, "失敗時沒有把 Xvfb 說的話讀回來"


def test_xvfb_stderr_tail_filters_the_keysym_noise():
    """xkbcomp 的一長串 keysym 警告會把真正的那一行淹掉。"""
    from app.services.rdp_freerdp import FreeRdpConnection

    conn = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                             domain=None, width=800, height=600)

    class _Fake:
        name = ""
        def flush(self): pass

    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False,
                                     encoding="utf-8") as fh:
        fh.write("The XKEYBOARD keymap compiler (xkbcomp) reports:\n")
        for i in range(40):
            fh.write(f"> Warning: Could not resolve keysym XF86Thing{i}\n")
        fh.write("_XSERVTransmkdir: ERROR: euid != 0, directory /tmp/.X11-unix will not be created.\n")
        path = fh.name
    _Fake.name = path
    conn._xvfb_err = _Fake()
    tail = conn._xvfb_stderr_tail()
    import os
    os.unlink(path)
    assert "_XSERVTransmkdir" in tail, "真正的錯誤被噪音擠掉了"
    assert "Could not resolve keysym" not in tail


def test_freerdp_errors_are_not_run_through_the_aardwolf_classifier():
    """FreeRDP 的錯誤不可以套 aardwolf 的失敗分類。

    那個分類器是照 aardwolf 的樣態寫的。把「虛擬顯示起不來」丟進去，會得到
    「連線/認證失敗（帳號、密碼、網域或 NLA 設定）」—— 指著完全無關的方向，
    而使用者會照著去查密碼（2026-09-17 正式環境實際發生）。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "api" / "v1" / "endpoints" / "rdp_console.py").read_text()
    i = src.index("if err is not None:")
    block = src[i:i + 1600]
    assert "isinstance(err, FreeRdpError)" in block, "沒有先辨認 FreeRDP 的錯誤"
    assert block.index("isinstance(err, FreeRdpError)") < block.index("_classify_connect_error"), \
        "FreeRDP 的錯誤仍會落進 aardwolf 的分類器"


def test_freerdp_gets_its_own_home():
    """xfreerdp 要用這條連線專用的家目錄。

    FreeRDP 會往 `$HOME/.config/freerdp` 寫東西。家目錄不可寫時它不直說，而是在
    後面回 `ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED`（「安全層協商失敗」）——
    指向完全無關的方向。給它自己的臨時目錄，就不必管服務的 HOME 是什麼。
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text()
    assert 'HOME=self._home' in src, "沒有給 xfreerdp 自己的家目錄"
    assert "mkdtemp" in src and "rmtree" in src, "臨時家目錄沒有建立或沒有清掉"


async def test_the_temp_home_is_removed_on_terminate():
    """臨時家目錄要跟著 session 消失，不可以累積。"""
    import os

    from app.services.rdp_freerdp import FreeRdpConnection

    conn = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                             domain=None, width=800, height=600)
    import tempfile
    conn._home = tempfile.mkdtemp(prefix="jtipam-rdp-test-")
    path = conn._home
    assert os.path.isdir(path)
    await conn.terminate()
    assert not os.path.isdir(path), "臨時家目錄沒有被清掉"


def test_odd_widths_are_rounded_down():
    """RDP 的桌面寬度必須是偶數。

    奇數寬度會讓連線在 post_connect 階段失敗，而 FreeRDP 回的是
    `ERRCONNECT_CONNECT_TRANSPORT_FAILED` —— 讀起來像「連不到目標」，
    但其實 TCP 早就通了。瀏覽器視窗寬度剛好是奇數的人會**每次都連不上**，
    而錯誤訊息指向網路（2026-09-17 正式環境：1525 寬）。

    實測：1525x979 與 1525x978 都失敗，1524x979 與 1524x978 都成功 ——
    所以是寬度，不是高度。
    """
    from app.services.rdp_freerdp import FreeRdpConnection

    conn = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                             domain=None, width=1525, height=979)
    assert conn._width == 1524, "奇數寬度沒有被對齊"
    assert conn._width % 2 == 0
    # 已經是偶數的不要動
    same = FreeRdpConnection(host="192.0.2.1", port=3389, username="u", password="p",
                             domain=None, width=1280, height=800)
    assert (same._width, same._height) == (1280, 800)


def test_post_connect_failures_are_not_called_a_network_problem():
    """`post_connect` 階段的失敗不可以說成「連不到目標的 3389」。

    那個階段代表 TCP 與認證都過了，卡在畫面參數協商。說成網路問題會讓人去 ping、
    查防火牆 —— 全都是對的，全都沒用。
    """
    from app.services.rdp_freerdp import _explain

    msg = _explain("ERRCONNECT_CONNECT_TRANSPORT_FAILED [0x0002000D] freerdp_post_connect failed")
    assert "畫面參數" in msg
    assert "連不到" not in msg
    # 純粹的傳輸失敗（沒到 post_connect）仍然要說連不到
    assert "連不到" in _explain("ERRCONNECT_CONNECT_TRANSPORT_FAILED [0x0002000D]")


def test_the_captured_frame_carries_no_cursor():
    """畫面裡不可以烘進游標 —— 瀏覽器自己會畫一個，兩個疊在一起就是「游標偏移」。

    ffmpeg 畫的是 **X 的**游標（被控端的游標形狀是另一回事），位置雖然正確，但使用者
    看到的是自己的箭頭加上畫面裡的那一個。aardwolf 引擎沒有這個現象，換引擎後才冒出來。
    順帶：滑鼠在空白處移動時不再產生任何更新，閒置流量歸零。
    """
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "services" / "rdp_freerdp.py").read_text("utf-8")
    assert '"-draw_mouse", "0"' in src
    assert '"-draw_mouse", "1"' not in src


def _record_xtest(monkeypatch):
    """攔下 xtest.fake_input，記下每一次注入。"""
    from Xlib.ext import xtest
    calls: list[tuple] = []
    monkeypatch.setattr(xtest, "fake_input",
                        lambda disp, kind, detail=0, **kw: calls.append((kind, detail, kw)))
    return calls


@pytest.mark.parametrize(("name", "pressed", "wheel", "expect"), [
    ("MOUSEBUTTON_LEFT", True, 0, [("press", 1)]),
    ("MOUSEBUTTON_LEFT", False, 0, [("release", 1)]),
    ("MOUSEBUTTON_RIGHT", True, 0, [("press", 3)]),
    ("MOUSEBUTTON_RIGHT", False, 0, [("release", 3)]),
    ("MOUSEBUTTON_MIDDLE", True, 0, [("press", 2)]),
    ("MOUSEBUTTON_HOVER", False, 0, []),
    ("MOUSEBUTTON_WHEEL_UP", False, 120, [("press", 4), ("release", 4)]),
    ("MOUSEBUTTON_WHEEL_UP", False, 0x100 | 120, [("press", 5), ("release", 5)]),
    ("MOUSEBUTTON_WHEEL_UP", False, 240, [("press", 4), ("release", 4)] * 2),
])
async def test_every_button_and_the_wheel_reach_x(monkeypatch, name, pressed, wheel, expect):
    from Xlib import X
    calls = _record_xtest(monkeypatch)
    conn = FreeRdpConnection(host="h", port=3389, username="u", password="p",
                             domain=None, width=800, height=600)
    conn._disp = SimpleNamespace(sync=lambda: None)

    class _B:
        def __init__(self, n: str) -> None: self.name = n

    await conn.send_mouse(_B(name), 123, 45, pressed, wheel)
    kind = {X.ButtonPress: "press", X.ButtonRelease: "release"}
    assert calls[0][0] == X.MotionNotify, "每次都要先把指標移到座標上"
    assert calls[0][2] == {"x": 123, "y": 45}
    assert [(kind[k], d) for k, d, _ in calls[1:]] == expect


async def test_mouse_coordinates_are_clamped_to_the_framebuffer():
    """寬度被捨成偶數後，前端仍可能送來最後那一欄 —— 不可以送出畫面外的座標。"""
    conn = FreeRdpConnection(host="h", port=3389, username="u", password="p",
                             domain=None, width=1525, height=801)
    seen: list[tuple[int, int]] = []
    conn._disp = SimpleNamespace(sync=lambda: None)
    from Xlib.ext import xtest
    orig = xtest.fake_input
    try:
        xtest.fake_input = lambda d, k, detail=0, **kw: seen.append((kw.get("x"), kw.get("y")))

        class _B:
            name = "MOUSEBUTTON_HOVER"

        await conn.send_mouse(_B(), 1524, 800, False)
        await conn.send_mouse(_B(), -5, -5, False)
    finally:
        xtest.fake_input = orig
    assert seen == [(1523, 799), (0, 0)]


def test_the_engine_reports_the_size_it_actually_got():
    """捨成偶數之後的尺寸要講出來，前端才畫得對。

    前端把 canvas 開成它「要求」的寬度，但 framebuffer 是捨過的。差 1 px 會讓最右邊
    那一欄永遠是黑的，而且 `mapXY` 用錯的比例換算 —— 邊緣愈靠右愈偏。
    """
    conn = FreeRdpConnection(host="h", port=3389, username="u", password="p",
                             domain=None, width=1525, height=801)
    assert conn.framebuffer_size == (1524, 800)
    even = FreeRdpConnection(host="h", port=3389, username="u", password="p",
                             domain=None, width=1366, height=830)
    assert even.framebuffer_size == (1366, 830)
