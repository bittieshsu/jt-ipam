"""FreeRDP 連線引擎：以 FreeRDP 取代 aardwolf 連到 RDP 目標。

為什麼需要它
------------
aardwolf 走的 `asyauth` 0.0.23 在 NTLM 認證時不送 MIC（原始碼裡是一行 TODO）。
MS-NLMP 3.1.5.1.2 說：伺服器的 CHALLENGE 帶了 `MsvAvTimestamp` 時，用戶端就該回 MIC；
FreeRDP 的伺服器端會**強制**檢查這一點，而 gnome-remote-desktop 用的正是它。
實測（2026-09-17，Ubuntu 24 + GNOME 遠端登入）：同一台主機、同一組帳密，
FreeRDP 認證成功，aardwolf 回 `STATUS_LOGON_FAILURE (0xc000006d)`。

怎麼做
------
FreeRDP 沒有可用的 Python 繫結，所以用它的 X11 用戶端：開一個只有這個 session 看得到的
虛擬顯示（Xvfb），讓 `xfreerdp` 畫在上面，我們再從那個顯示把畫面抓下來、把輸入打進去。

對外介面刻意與 aardwolf 的連線物件**一模一樣**（`ext_out_queue` / `send_mouse` /
`send_key_scancode` / `send_key_char` / `terminate`），所以 `rdp_console._bridge()`
與整個前端都不用改 —— 換引擎對上層是透明的。

資安
----
- 密碼走 `/from-stdin:force`，**不進 argv** —— `/p:` 會讓本機任何使用者從 `ps` 讀到。
- Xvfb 用 `-nolisten tcp`，顯示只存在於本機 unix socket。
- 行程與 WS session 同生共死；`terminate()` 一定要把兩個子行程收掉，否則會留下孤兒
  Xvfb 一直佔著記憶體。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("jt-ipam.rdp.freerdp")

# 需要的外部程式。少任何一個就不能用這個引擎 —— 由 `availability()` 回報，
# 讓設定頁講得出「缺什麼、怎麼裝」，而不是讓使用者連到一半才看到錯誤。
REQUIRED_BINARIES: dict[str, str] = {
    "xfreerdp": "freerdp2-x11",
    "Xvfb": "xvfb",
    # 抓畫面。不是「順便用用看」——`XGetImage` 經 python-xlib 要 334 ms/張（1280x800，
    # 純 Python 解析 4 MB 像素），上限 2.8 fps，互動主控台不能用。ffmpeg 的 x11grab
    # 內部走 MIT-SHM，同一台機器量到 47 fps，而且 `-draw_mouse` 會把遠端游標畫進來。
    "ffmpeg": "ffmpeg",
}
# 純 Python 相依（抓畫面與打鍵盤滑鼠）
REQUIRED_MODULES: dict[str, str] = {
    "Xlib": "python-xlib",
    "PIL": "pillow",
}

# 缺套件時印給人照著貼的指令。這串由後端算，前端不要自己維護一份 —— 兩份會不一致。
FREERDP_APT_HINT = "sudo apt-get install -y freerdp2-x11 xvfb xclip ffmpeg"

_CONNECT_TIMEOUT = 25.0        # 秒；與 aardwolf 那條路一致
_CAPTURE_FPS = 15              # 交給 ffmpeg 的取樣率；畫面沒變時我們仍然不送
_XVFB_READY_TIMEOUT = 10.0


def _die_with_parent() -> None:
    """讓子行程跟著後端一起死。

    Xvfb／xfreerdp／ffmpeg 都是後端的子行程，正常收線走 `terminate()`。但後端若是被
    SIGKILL 帶走（OOM、`systemctl kill`、部署腳本出手），`terminate()` 沒有機會跑，
    這三個行程會變成孤兒繼續佔著記憶體 —— 而且因為它們不吵不鬧，沒有人會發現，
    直到那台機器的記憶體被一堆看不出來歷的 Xvfb 吃光。

    `PR_SET_PDEATHSIG` 讓核心在父行程消失時直接送 SIGKILL 給它們。只有 Linux 有；
    其他平台就維持原本的行為（本專案只部署在 Linux）。
    """
    with contextlib.suppress(Exception):
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL)      # PR_SET_PDEATHSIG = 1


@dataclass(slots=True)
class VideoTile:
    """一塊畫面更新。欄位名與 aardwolf 的 video data 相同，`_bridge` 才不用分辨來源。"""

    x: int
    y: int
    width: int
    height: int
    data: bytes          # PNG


def availability() -> dict[str, Any]:
    """這台機器能不能用 FreeRDP 引擎，缺什麼。

    回傳 `{ok, missing_packages, missing_modules, install_hint}`。設定頁直接顯示這個 ——
    「選項在那裡但按了才發現不能用」比沒有這個選項更糟。
    """
    missing_pkgs = sorted({pkg for exe, pkg in REQUIRED_BINARIES.items()
                           if shutil.which(exe) is None})
    missing_mods: list[str] = []
    for mod, pkg in REQUIRED_MODULES.items():
        try:
            __import__(mod)
        except Exception:
            missing_mods.append(pkg)
    return {
        "ok": not missing_pkgs and not missing_mods,
        "missing_packages": missing_pkgs,
        "missing_modules": sorted(missing_mods),
    }


class FreeRdpError(Exception):
    """連線失敗；訊息帶得住底層原文，不要只說「連線失敗」。"""


# ── 輸入 ────────────────────────────────────────────────────────────────────
#
# 上層送進來的是 RDP 的掃描碼（PC Set-1）與 unicode 字元，但我們能操作的是 X。
# 中間這張表把掃描碼翻成 X 的 keysym 名稱，再由 X 自己查出對應的 keycode ——
# 直接假設「X keycode = 掃描碼 + 8」在擴充鍵（方向鍵、Delete…）上會錯，
# 而那些正是主控台最常按的鍵。
_SCANCODE_KEYSYM: dict[int, str] = {
    0x1C: "Return", 0x0E: "BackSpace", 0x0F: "Tab", 0x01: "Escape", 0x39: "space",
    0x53: "Delete", 0x47: "Home", 0x4F: "End", 0x49: "Prior", 0x51: "Next",
    0x52: "Insert", 0x48: "Up", 0x50: "Down", 0x4B: "Left", 0x4D: "Right",
    0x1D: "Control_L", 0x2A: "Shift_L", 0x38: "Alt_L", 0x5B: "Super_L",
    0x3B: "F1", 0x3C: "F2", 0x3D: "F3", 0x3E: "F4", 0x3F: "F5", 0x40: "F6",
    0x41: "F7", 0x42: "F8", 0x43: "F9", 0x44: "F10", 0x57: "F11", 0x58: "F12",
    # 字母與數字列（與 rdp_console._CODE_SCANCODES 同一組掃描碼）
    0x1E: "a", 0x30: "b", 0x2E: "c", 0x20: "d", 0x12: "e", 0x21: "f", 0x22: "g",
    0x23: "h", 0x17: "i", 0x24: "j", 0x25: "k", 0x26: "l", 0x32: "m", 0x31: "n",
    0x18: "o", 0x19: "p", 0x10: "q", 0x13: "r", 0x1F: "s", 0x14: "t", 0x16: "u",
    0x2F: "v", 0x11: "w", 0x2D: "x", 0x15: "y", 0x2C: "z",
    0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5",
    0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
    0x0C: "minus", 0x0D: "equal", 0x1A: "bracketleft", 0x1B: "bracketright",
    0x2B: "backslash", 0x27: "semicolon", 0x28: "apostrophe", 0x29: "grave",
    0x33: "comma", 0x34: "period", 0x35: "slash",
}

# 上層的滑鼠按鍵值（aardwolf 的 MOUSEBUTTON）→ X 的按鈕編號。
# 這裡刻意比對名稱而不是 enum 本身：FreeRDP 這條路不該為了一個常數而 import aardwolf。
_XBUTTON_BY_NAME: dict[str, int] = {
    "MOUSEBUTTON_LEFT": 1, "MOUSEBUTTON_MIDDLE": 2, "MOUSEBUTTON_RIGHT": 3,
}


class _InputMixin:
    """滑鼠與鍵盤注入。拆成 mixin 只是為了讓上面的連線邏輯讀起來不被沖散。"""

    _disp: Any
    _root: Any
    _display: str | None
    _spare_keycode: int | None
    _char_down: dict[str, tuple[int, bool, bool]]
    _width: int
    _height: int

    async def _x(self, fn: Any, *a: Any) -> None:
        """Xlib 是同步的，一律丟到執行緒，別卡住 event loop。"""
        if self._disp is None:
            return
        await asyncio.to_thread(fn, *a)

    async def send_mouse(self, button: Any, x: int, y: int, pressed: bool,
                         wheel_data: int = 0) -> None:
        name = getattr(button, "name", str(button))
        x = max(0, min(int(x), self._width - 1))
        y = max(0, min(int(y), self._height - 1))

        def _do() -> None:
            from Xlib import X
            from Xlib.ext import xtest
            xtest.fake_input(self._disp, X.MotionNotify, x=x, y=y)
            if name == "MOUSEBUTTON_HOVER":
                pass
            elif name == "MOUSEBUTTON_WHEEL_UP":
                # 上層把方向塞進 steps 的 0x100 位（WHEEL_NEGATIVE），量值是 120 的倍數
                down = bool(int(wheel_data) & 0x100)
                btn = 5 if down else 4
                clicks = max(1, (int(wheel_data) & 0xFF) // 120) or 1
                for _ in range(clicks):
                    xtest.fake_input(self._disp, X.ButtonPress, btn)
                    xtest.fake_input(self._disp, X.ButtonRelease, btn)
            else:
                btn = _XBUTTON_BY_NAME.get(name, 1)
                xtest.fake_input(
                    self._disp, X.ButtonPress if pressed else X.ButtonRelease, btn)
            self._disp.sync()

        await self._x(_do)

    async def send_key_scancode(self, scancode: int, pressed: bool,
                                _extended: bool = False) -> None:
        keysym_name = _SCANCODE_KEYSYM.get(int(scancode))
        if keysym_name is None:
            logger.debug("freerdp: 不認得的掃描碼 0x%02x，略過", scancode)
            return

        def _do() -> None:
            from Xlib import XK, X
            from Xlib.ext import xtest
            keysym = XK.string_to_keysym(keysym_name)
            keycode = self._disp.keysym_to_keycode(keysym)
            if not keycode:
                return
            xtest.fake_input(self._disp, X.KeyPress if pressed else X.KeyRelease, keycode)
            self._disp.sync()

        await self._x(_do)

    async def send_key_char(self, ch: str, pressed: bool) -> None:
        """打出一個字元。

        優先用**目前鍵盤配置上已經有的那顆鍵**（必要時補 Shift）。原本的做法是把字元
        暫時重綁到一個沒人用的 keycode 再敲 —— 那在這裡行不通：X 要先把 MappingNotify
        送到客戶端、客戶端處理完，新的對應才算數，而我們在幾微秒後就敲下去了，
        結果是「一聲不響、什麼都沒打出來」。重綁只留給配置上真的沒有的字元。
        """
        if len(ch) != 1:
            return
        cp = ord(ch)
        keysym = cp if cp < 0x100 else 0x01000000 + cp

        if pressed:
            plan = await asyncio.to_thread(self._plan_char, keysym)
            if plan is None:
                return
            self._char_down[ch] = plan
        else:
            plan = self._char_down.pop(ch, None)
            if plan is None:
                return
        keycode, needs_shift, remapped = plan

        def _do() -> None:
            from Xlib import XK, X
            from Xlib.ext import xtest
            shift_kc = self._disp.keysym_to_keycode(XK.string_to_keysym("Shift_L"))
            if pressed:
                if needs_shift and shift_kc:
                    xtest.fake_input(self._disp, X.KeyPress, shift_kc)
                xtest.fake_input(self._disp, X.KeyPress, keycode)
            else:
                xtest.fake_input(self._disp, X.KeyRelease, keycode)
                if needs_shift and shift_kc:
                    xtest.fake_input(self._disp, X.KeyRelease, shift_kc)
                if remapped:
                    # 用完把借來的那顆還原，免得累積一堆奇怪的對應
                    self._disp.change_keyboard_mapping(keycode, [[X.NoSymbol, X.NoSymbol]])
            self._disp.sync()

        await self._x(_do)

    def _plan_char(self, keysym: int) -> tuple[int, bool, bool] | None:
        """決定這個字元要敲哪一顆鍵、要不要按 Shift、是不是借來的。

        回傳 `(keycode, needs_shift, remapped)`；找不到就回 None。
        """
        pairs = self._disp.keysym_to_keycodes(keysym)
        for keycode, index in pairs:
            if keycode:
                return keycode, bool(index), False
        # 配置上沒有這個字元 → 借一顆沒人用的 keycode，並給客戶端時間處理 MappingNotify
        if self._spare_keycode is None:
            return None
        self._disp.change_keyboard_mapping(self._spare_keycode, [[keysym, keysym]])
        self._disp.sync()
        time.sleep(0.03)
        return self._spare_keycode, False, True

    async def set_current_clipboard_text(self, text: str) -> None:
        """控制端貼上。

        FreeRDP 的剪貼簿重導走的是 X selection：把文字放進那個顯示的 CLIPBOARD，
        xfreerdp 就會把它同步到被控端。這裡用 `xclip` 之外的做法會牽進整套
        selection owner 事件迴圈，先以外部工具處理；沒裝 xclip 就當作不支援。

        單向性是**結構上**成立的，不是靠設定：我們只會往那個 X 顯示寫，從來不讀。
        被控端的剪貼簿即使被 xfreerdp 同步過來，也只停在那個虛擬顯示裡，隨 session
        一起消滅，不會回到控制端。
        """
        if not self._clip_enabled:
            raise FreeRdpError("這個站台沒有開啟「控制端貼上」")
        if not text or self._display is None:
            return
        if shutil.which("xclip") is None:
            raise FreeRdpError("這台沒有安裝 xclip，FreeRDP 引擎無法轉送剪貼簿")
        proc = await asyncio.create_subprocess_exec(
            "xclip", "-selection", "clipboard",
            env=dict(os.environ, DISPLAY=self._display or ""),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate(text.encode())


class FreeRdpConnection(_InputMixin):
    """一條 FreeRDP 連線（含它專屬的 Xvfb）。

    介面與 aardwolf 的連線物件相同，供 `rdp_console._bridge()` 直接使用。
    """

    def __init__(self, *, host: str, port: int, username: str, password: str,
                 domain: str | None, width: int, height: int,
                 clip_enabled: bool = False) -> None:
        self._host, self._port = host, port
        self._clip_enabled = clip_enabled
        self._username, self._password = username, password
        self._domain = domain or None
        self._width, self._height = width, height

        self.ext_out_queue: asyncio.Queue[VideoTile | None] = asyncio.Queue(maxsize=8)
        self._xvfb: asyncio.subprocess.Process | None = None
        self._rdp: asyncio.subprocess.Process | None = None
        self._grab: asyncio.subprocess.Process | None = None
        self._display: str | None = None
        self._disp: Any = None              # Xlib display
        self._root: Any = None
        self._grab_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        # xfreerdp 為什麼結束的（有值代表是它先走的，不是我們收掉它）
        self.exit_reason: str | None = None
        self._spare_keycode: int | None = None
        # 目前按住的字元 → (keycode, 要不要 Shift, 是不是借來的)。放開時要還原同一顆，
        # 中間若重綁會變成放開別的鍵。
        self._char_down: dict[str, tuple[int, bool, bool]] = {}
        self._closed = False
        self._frames = 0          # 已送出的畫面張數（給效能量測與日誌用）

    # ── 生命週期 ────────────────────────────────────────────────────────────

    async def connect(self) -> tuple[Any, Exception | None]:
        """回傳 `(result, error)` —— 與 aardwolf 的 `connect()` 同形狀。"""
        try:
            await self._start_xvfb()
            await self._start_xfreerdp()
            await self._open_x_display()
            await self._start_capture()
            self._grab_task = asyncio.create_task(self._grab_loop())
            self._watch_task = asyncio.create_task(self._watch_child())
            return None, None
        except Exception as exc:          # 一律轉成 (None, err) 交給呼叫端處理
            await self.terminate()
            return None, exc

    async def _start_xvfb(self) -> None:
        """開一個只給這條連線用的虛擬顯示。

        用 `-displayfd` 讓 Xvfb 自己挑一個沒被佔用的編號再告訴我們 —— 自己掃
        `/tmp/.X11-unix` 再挑，兩條連線同時開就會搶到同一個號碼。
        """
        r_fd, w_fd = os.pipe()
        try:
            self._xvfb = await asyncio.create_subprocess_exec(
                "Xvfb", "-displayfd", str(w_fd),
                "-screen", "0", f"{self._width}x{self._height}x24",
                "-nolisten", "tcp", "-noreset",
                pass_fds=(w_fd,), preexec_fn=_die_with_parent,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
        finally:
            os.close(w_fd)

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(r_fd, "rb"))
        try:
            async with asyncio.timeout(_XVFB_READY_TIMEOUT):
                line = await reader.readline()
        except TimeoutError as exc:
            raise FreeRdpError("Xvfb 沒有在時限內就緒") from exc
        num = line.decode().strip()
        if not num.isdigit():
            raise FreeRdpError(f"Xvfb 回報的顯示編號看不懂：{num!r}")
        self._display = f":{num}"
        logger.info("freerdp: Xvfb 就緒 display=%s size=%dx%d",
                    self._display, self._width, self._height)

    async def _start_xfreerdp(self) -> None:
        """起 xfreerdp。密碼走 stdin，不進 argv。"""
        args = [
            "xfreerdp",
            f"/v:{self._host}:{self._port}",
            f"/u:{self._username}",
            f"/size:{self._width}x{self._height}",
            # ⚠️ 不要指定 /bpp：gnome-remote-desktop 收到明確的色深要求就會在能力交換階段
            # 回 DEACTIVATE_ALL（FreeRDP 報 `expected PDU_TYPE_DEMAND_ACTIVE 0001, got 0006`），
            # 連線直接斷。讓伺服器自己決定色深就沒事 —— 我們抓畫面時本來就會轉成 RGB。
            "/cert:ignore",          # 目標憑證多半是自簽；信任由網路層與跳板決定
            "/from-stdin:force",     # ⚠️ 密碼只能走這裡
            "-wallpaper", "-themes", "-menu-anims", "-decorations",
            "+auto-reconnect",
            # ⚠️ FreeRDP 的剪貼簿重導**預設是開的**。管理者關掉「控制端貼上」時，
            # 換到這個引擎不可以把它默默打開 —— 那是把一個被明確關閉的資料通道
            # 重新接上。deny by default，與 aardwolf 那條路（沒開就不掛 cliprdr）一致。
            "+clipboard" if self._clip_enabled else "-clipboard",
            "/log-level:WARN",
        ]
        if self._domain:
            args.insert(3, f"/d:{self._domain}")

        env = dict(os.environ, DISPLAY=self._display or "")
        self._rdp = await asyncio.create_subprocess_exec(
            *args, env=env, preexec_fn=_die_with_parent,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self._rdp.stdin is not None
        self._rdp.stdin.write(self._password.encode() + b"\n")
        with contextlib.suppress(Exception):
            await self._rdp.stdin.drain()
        self._rdp.stdin.close()
        # 密碼只在這個物件上留到這裡為止
        self._password = ""

        await self._await_connected()

    async def _await_connected(self) -> None:
        """等 xfreerdp 真的連上。

        判準是「視窗出現在那個顯示上」而不是「行程還活著」—— 行程在認證失敗後
        還會存活一小段時間，只看行程會把失敗說成成功（升級腳本踩過同一種錯，
        見 `scripts/jt-ipam.sh` 的 wait_until_serving）。
        """
        assert self._rdp is not None
        deadline = time.monotonic() + _CONNECT_TIMEOUT
        stderr_buf = b""
        while time.monotonic() < deadline:
            if self._rdp.returncode is not None:
                if self._rdp.stderr is not None:
                    with contextlib.suppress(Exception):
                        stderr_buf = await self._rdp.stderr.read(4096)
                raise FreeRdpError(_explain(stderr_buf.decode("utf-8", "replace")))
            if await self._has_window():
                logger.info("freerdp: 已連上 %s:%s", self._host, self._port)
                return
            await asyncio.sleep(0.25)
        raise FreeRdpError("FreeRDP 在時限內沒有畫出畫面（可能卡在認證或協定協商）")

    async def _has_window(self) -> bool:
        """那個顯示上有沒有已經對映的視窗。"""
        def _check() -> bool:
            try:
                from Xlib import display as xdisplay
                d = xdisplay.Display(self._display)
                try:
                    root = d.screen().root
                    kids = root.query_tree().children
                    for w in kids:
                        attrs = w.get_attributes()
                        if attrs.map_state == 2:      # IsViewable
                            return True
                    return False
                finally:
                    d.close()
            except Exception:
                return False
        return await asyncio.to_thread(_check)

    async def _open_x_display(self) -> None:
        def _open() -> tuple[Any, Any, int | None]:
            from Xlib import display as xdisplay
            d = xdisplay.Display(self._display)
            root = d.screen().root
            # 留一個沒被使用的 keycode 給「打出任意 unicode 字元」用（見 send_key_char）
            spare = None
            mn, mx = d.display.info.min_keycode, d.display.info.max_keycode
            mapping = d.get_keyboard_mapping(mn, mx - mn + 1)
            for i, syms in enumerate(mapping):
                kc = mn + i
                # 跳過最小值那一顆：它在多數實作上被當成保留，借來用不一定生效
                if kc > mn and not any(syms):
                    spare = kc
                    break
            return d, root, spare
        self._disp, self._root, self._spare_keycode = await asyncio.to_thread(_open)
        if self._spare_keycode is None:
            logger.warning("freerdp: 找不到備用 keycode，非 ASCII 字元可能打不出來")

    async def _start_capture(self) -> None:
        """把畫面交給 ffmpeg 抓，我們只負責從管線讀原始像素。

        `-draw_mouse 1`：遠端游標是 X 端畫的，`XGetImage` 抓不到它 —— 少了這個參數，
        使用者在畫面上看不到自己的游標在哪裡。
        """
        size = self._width * self._height * 3
        self._grab = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error",
            "-f", "x11grab", "-draw_mouse", "1",
            "-video_size", f"{self._width}x{self._height}",
            "-framerate", str(_CAPTURE_FPS),
            "-i", self._display or "",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            preexec_fn=_die_with_parent,
            # 預設的資料流上限是 64 KB，一張 1280x800 是 3 MB —— 不放大會一直卡住
            limit=size + (1 << 20),
        )
        logger.info("freerdp: 畫面擷取已啟動 %dx%d @%d fps",
                    self._width, self._height, _CAPTURE_FPS)

    async def _watch_child(self) -> None:
        """xfreerdp 自己結束時要出聲。

        它死掉之後畫面就停在最後一張 —— 不會有錯誤、不會斷線，使用者只看到一個
        不再更新的桌面，還以為是網路慢。最常見的原因是被控端把工作階段換掉了
        （GNOME 從登入畫面交接到使用者工作階段就會這樣）。
        """
        if self._rdp is None:
            return
        rc = await self._rdp.wait()
        if self._closed:
            return
        detail = ""
        if self._rdp.stderr is not None:
            with contextlib.suppress(Exception):
                detail = (await self._rdp.stderr.read(2048)).decode("utf-8", "replace")
        self.exit_reason = _explain(detail) if detail.strip() else f"FreeRDP 結束（代碼 {rc}）"
        logger.info("freerdp: 子行程結束 rc=%s reason=%s", rc, self.exit_reason)
        # 讓串流那一端知道該收了；`_bridge` 會因此結束，WS 才會關掉而不是無聲凍結
        with contextlib.suppress(Exception):
            self.ext_out_queue.put_nowait(None)

    async def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._watch_task,):
            if task is not None:
                task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await task
        self._watch_task = None
        if self._grab_task is not None:
            self._grab_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._grab_task
        with contextlib.suppress(Exception):
            await self.ext_out_queue.put(None)
        if self._disp is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._disp.close)
            self._disp = None
        # 先收 RDP 再收 Xvfb（反過來會讓 xfreerdp 對著不存在的顯示噴一堆錯）
        for proc in (self._grab, self._rdp, self._xvfb):
            if proc is None or proc.returncode is not None:
                continue
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                async with asyncio.timeout(5):
                    await proc.wait()
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        self._grab = self._rdp = self._xvfb = None

    # ── 畫面 ────────────────────────────────────────────────────────────────

    async def _grab_loop(self) -> None:
        """從 ffmpeg 讀畫面 → 只把「變了的那一塊」送出去。

        比對整張交給 Pillow（C 實作）做：純 Python 逐像素在 1280x800 上一秒跑不完一張。
        沒變就不送 —— 閒置的桌面不該持續佔頻寬。
        """
        from PIL import Image, ImageChops

        if self._grab is None or self._grab.stdout is None:
            return
        size = self._width * self._height * 3
        prev: Any = None
        while not self._closed:
            try:
                raw = await self._grab.stdout.readexactly(size)
            except asyncio.IncompleteReadError:
                logger.info("freerdp: 畫面擷取結束（ffmpeg 收掉了管線）")
                break
            except Exception as exc:
                logger.info("freerdp: 讀畫面失敗，結束串流：%r", exc)
                break
            cur = Image.frombytes("RGB", (self._width, self._height), raw)
            box = (0, 0, self._width, self._height) if prev is None else \
                ImageChops.difference(prev, cur).getbbox()
            prev = cur
            if box is not None:
                await self._emit(cur.crop(box), box)
                self._frames += 1

    async def _emit(self, tile: Any, box: tuple[int, int, int, int]) -> None:
        import io

        def _encode() -> bytes:
            buf = io.BytesIO()
            tile.save(buf, format="PNG", compress_level=1)   # 速度優先，頻寬其次
            return buf.getvalue()

        png = await asyncio.to_thread(_encode)
        x0, y0, x1, y1 = box
        item = VideoTile(x=x0, y=y0, width=x1 - x0, height=y1 - y0, data=png)
        try:
            self.ext_out_queue.put_nowait(item)
        except asyncio.QueueFull:
            # 控制端跟不上就丟掉這一張：畫面是「目前狀態」不是事件流，
            # 積壓只會讓延遲越拖越長，下一張整片更新會補回來。
            with contextlib.suppress(asyncio.QueueEmpty):
                self.ext_out_queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.ext_out_queue.put_nowait(item)


def _explain(stderr_text: str) -> str:
    """把 FreeRDP 的錯誤講成使用者看得懂的話，但**保留底層原文**。"""
    low = stderr_text.lower()
    if "logon_failure" in low or "errconnect_logon_failure" in low:
        hint = "帳號或密碼不正確"
    elif "errconnect_connect_transport_failed" in low or "connection reset" in low:
        hint = "連不到目標的 3389"
    elif "errconnect_password_expired" in low:
        hint = "密碼已過期"
    elif "account_disabled" in low:
        hint = "帳號已停用"
    elif "errconnect_security_nego_connect_failed" in low:
        hint = "安全層協商失敗（對方要求的模式與我們送出的不一致）"
    elif "demand_active" in low:
        # 伺服器在能力交換階段就回 DEACTIVATE_ALL。實測過的成因是用戶端指定了色深
        # （/bpp），gnome-remote-desktop 不接受。
        hint = "伺服器在能力交換階段中斷連線（對方不接受我們要求的畫面參數）"
    else:
        hint = "FreeRDP 連線失敗"
    tail = " ".join(stderr_text.split())[-240:]
    return f"{hint}：{tail}" if tail else hint
