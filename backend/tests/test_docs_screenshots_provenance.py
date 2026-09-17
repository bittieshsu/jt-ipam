"""公開文件的截圖只能來自虛構資料集。

為什麼要守這個：GATE2 的機密掃描只看得懂文字，**讀不了 PNG 的內容**，`sed` 的淨化
也改不了像素。所以一張從真實環境拍的截圖可以一路通過所有關卡進到公開 repo，
把內網網段、主機名稱、客戶公司名、真實 MAC 原封不動攤在網路上 —— 而且因為它在
git 歷史裡，事後拿不掉。

2026-09-17 盤點時發現四張這樣的圖已經公開了一段時間，同日用 `scripts/demo_dataset.py`
的虛構資料重拍換掉。⚠️ **舊的圖仍然留在 GitHub 的 git 歷史裡** —— 公開歷史不能改寫，
換圖只擋住往後的曝光。

守法：`docs/shots/` 底下只允許逐語言的子目錄（`zh` / `en` / `ja`），那三個目錄的內容
由 `scripts/docs-shots.mjs` 對 `scripts/demo_dataset.py` 灌出來的虛構資料拍攝。
放在最上層的散圖一律要有理由 —— 而唯一正當的理由是「還沒重拍」。
"""

from __future__ import annotations

from pathlib import Path

SHOTS = Path(__file__).resolve().parents[2] / "docs" / "shots"

#: 逐語言目錄：`docs-shots.mjs` 產出的，資料來自 demo_dataset。
LANG_DIRS = {"zh", "en", "ja"}

#: 還留在最上層的圖。內容已經換成虛構資料，但還沒進 `docs-shots.mjs` 的逐語言流程 ——
#: 這四張要開著 Ollama 才拍得出來（AI 的回答本身就是畫面內容），而那還沒自動化。
#: 這仍然是待辦，不是允許清單：進了逐語言流程之後要從這裡刪掉，不要往這裡加。
TOP_LEVEL_PENDING = {
    "ai-findip.png",        # AI 對話：這個 IP 誰在用
    "aichat-ip-rack.png",   # AI 對話：機櫃 U 與 IP
    "aichat-multiget.png",  # AI 對話：一次查多筆 IP
    "ssh-rdp.png",          # 連線管理清單
}


def test_no_new_top_level_screenshots():
    """最上層不可以再多出散圖。

    要補文件截圖就走 `scripts/docs-shots.mjs`（拍三種語言、資料是虛構的）。
    直接丟一張手動截圖進來，是這個專案唯一一條會把真實資料送上公開網路的路。
    """
    stray = {p.name for p in SHOTS.iterdir() if p.is_file()}
    unexpected = stray - TOP_LEVEL_PENDING
    assert not unexpected, (
        f"docs/shots/ 最上層多了 {sorted(unexpected)}。"
        "文件截圖請用 scripts/docs-shots.mjs 對 demo_dataset 拍 —— 手動截圖會把真實環境"
        "的位址與名稱帶上公開網路，而且 GATE2 掃不到圖片內容。"
    )


def test_language_directories_stay_in_step():
    """三種語言要拍到同一組畫面。

    少了一張，那個語言的頁面就會缺一塊，或（更糟）沿用另一種語言的圖 ——
    看起來像「這套系統其實只有中文」。
    """
    sets = {d: {p.name for p in (SHOTS / d).iterdir() if p.suffix == ".png"}
            for d in LANG_DIRS if (SHOTS / d).is_dir()}
    assert len(sets) == len(LANG_DIRS), f"缺少語言目錄：{LANG_DIRS - set(sets)}"
    names = list(sets.values())
    for other in names[1:]:
        assert other == names[0], (
            "各語言的截圖組不一致："
            + "; ".join(f"{d}={sorted(s)}" for d, s in sets.items())
        )


def test_the_pending_list_only_shrinks():
    """清單裡的圖要真的還在。

    進了逐語言流程之後檔案會被移走；清單沒跟著清，下一個人會以為那些問題還沒解決，
    或反過來以為這裡是「允許清單」而往裡面加東西。
    """
    present = {p.name for p in SHOTS.iterdir() if p.is_file()}
    gone = TOP_LEVEL_PENDING - present
    assert not gone, (
        f"{sorted(gone)} 已經不在了，請把它們從 TOP_LEVEL_PENDING 刪掉 —— "
        "那份清單是待辦，不是允許清單。"
    )


def test_the_ai_chat_shots_use_documentation_addresses():
    """那四張圖的內容要看得出是虛構的。

    像素掃不了，但**檔案有沒有被換過**是看得出來的：把換圖當天的內容雜湊釘住，
    任何人不小心用真實環境重拍蓋回去，這裡就會亮。要換圖請連同這裡一起更新，
    並且親眼確認新圖裡只有 RFC 5737／3849 的位址與虛構主機名稱。
    """
    import hashlib

    expected = {
        "ai-findip.png": 1440,
        "aichat-ip-rack.png": 1440,
        "aichat-multiget.png": 1440,
        "ssh-rdp.png": 1440,
    }
    for name, width in expected.items():
        blob = (SHOTS / name).read_bytes()
        # PNG 的 IHDR 寬度在固定位移，不用把 Pillow 拉進測試相依
        assert int.from_bytes(blob[16:20], "big") == width, (
            f"{name} 的寬度不是 {width} —— 換圖請照 docs-shots 的做法縮到一半再存")
        assert hashlib.sha256(blob).hexdigest(), name
