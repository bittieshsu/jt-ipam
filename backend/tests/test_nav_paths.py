"""說明文字裡的「到哪裡設定」路徑必須和實際選單一致（使用者 2026-10-11）。

起因：工具頁的 GeoIP 提示寫「Settings → System」，實際是「管理 → 系統設定」；一查發現十幾處都是改版前的
名稱（「管理 → 整合 FortiGate」「整合 → OPNsense」「個人設定 → 安全」「管理 → 系統檢查」），英文清單還夾著中文選單名。
選單一改名，散在各處的說明就全部變成錯的，而且沒有任何測試會紅 —— 這支測試就是那道守門。

規則（三種語言、文件網站、README、測試清單都掃）：
- 以「管理 →」「進階 →」開頭的路徑，每一段都要是那一層真的存在的選單名稱（逐字、含大小寫，同一條路徑同一種語言）
- 「外部系統整合 →」後面要是真的整合項目；「系統設定 →」後面要是系統設定頁上看得到的文字
- 「系統設定 →」「外部系統整合 →」不可以單獨當開頭（要從「管理」開始寫）
- 已知寫錯過的說法直接擋（FORBIDDEN）

選單結構從 MainLayout.vue 現場解析，系統設定頁的文字從 SystemSettings.vue 解析；改選單時這支測試會跟著變。
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FE = ROOT / "frontend" / "src"
LOCALES = ("zh-TW", "en-US", "ja-JP")
ARROW = "→"

#: 寫錯過、而且鏈狀檢查抓不到的說法
FORBIDDEN = [
    (re.compile(r"設定 ?→ ?系統[）)]"), "工具頁 GeoIP 提示曾寫「設定→系統」：實際是「管理 → 系統設定」"),
    (re.compile(r"Settings → System\)"), "實際是 Admin → System Settings"),
    (re.compile(r"設定 → システム[）)]"), "実際は「管理 → システム設定」"),
    (re.compile(r"(?<!偏好)設定 ?→ ?安全"), "帳號的安全分頁：右上角帳號選單的「偏好設定 → 安全」（選單裡沒有「個人設定」）"),
    (re.compile(r"(?<!Preferences )Settings → Security"), "the account page is Preferences → Security (account menu, top right)"),
    (re.compile(r"(?<!外部系統)整合 ?→"), "整合在「管理 → 外部系統整合 → 產品名」"),
    (re.compile(r"(?<![Ee]xternal )Integrations ?→"), "integrations live under Admin → External integrations"),
    (re.compile(r"(?<!外部システム)連携 ?→"), "連携は「管理 → 外部システム連携 → 製品名」"),
    (re.compile(r"Admin ?-> ?"), "use → like the rest of the UI"),
]


def _labels() -> dict[str, dict[str, object]]:
    """每種語言的選單結構：root → 子項目；整合子選單；系統設定頁看得到的文字。"""
    layout = (FE / "components" / "layout" / "MainLayout.vue").read_text(encoding="utf-8")
    a0 = layout.index('t("nav.admin_section")')
    a1 = layout.index('t("nav.chat_history")', a0)
    admin_keys = re.findall(r't\("(nav\.[a-z_]+)"\)', layout[a0:a1 + 30])
    admin_literals = re.findall(r'label: \(\) => "([^"]+)"', layout[a0:a1 + 30])
    p0 = layout.index('t("nav.phase3_section")')
    p1 = layout.index('t("nav.tools")', p0)
    adv_keys = re.findall(r't\("(nav\.[a-z_]+)"\)', layout[p0:p1])
    intg_keys = re.findall(r'\["[a-z_]+", "(nav\.[a-z_]+)", [A-Za-z]+Icon\]', layout)
    ss = (FE / "views" / "SystemSettings.vue").read_text(encoding="utf-8")
    ss_keys = set(re.findall(r"""t\(["']([a-z_]+\.[a-z_.]+)["']""", ss))
    assert len(admin_keys) > 20 and len(intg_keys) > 10 and adv_keys and len(ss_keys) > 50, "選單解析不到（格式改了？）"

    out: dict[str, dict[str, object]] = {}
    for loc in LOCALES:
        d = json.loads((FE / "i18n" / f"{loc}.json").read_text(encoding="utf-8"))

        def tr(key: str, d: dict = d) -> str | None:
            o: object = d
            for part in key.split("."):
                o = o.get(part) if isinstance(o, dict) else None
            return o if isinstance(o, str) else None

        admin_children = {tr(k) for k in admin_keys if k != "nav.admin_section"} | set(admin_literals)
        out[loc] = {
            "admin": tr("nav.admin_section"),
            "advanced": tr("nav.phase3_section"),
            "admin_children": {x for x in admin_children if x},
            "adv_children": {x for x in (tr(k) for k in adv_keys if k != "nav.phase3_section") if x},
            "integrations": tr("nav.integrations"),
            "intg_children": {x for x in (tr(k) for k in intg_keys) if x},
            "system_settings": tr("nav.system_settings"),
            "ss_children": {x for x in (tr(k) for k in ss_keys) if x and len(x) <= 60},
            "strings": d,
        }
    return out


_STRIP = " \t　「」『』“”\"'*`《》(（"


def _norm(s: str) -> str:
    """介面用全形「／」與半形括號，文件規定半形斜線（test_docs_site_coverage）：比對時視為相同。"""
    return (s.replace("／", "/").replace("（", "(").replace("）", ")").replace(" (", "(")
             .replace(" / ", "/"))


def _match(text: str, cands: set[str]) -> str | None:
    """text 開頭是哪一個候選名稱（最長者）；回傳正規化後的名稱。"""
    t = _norm(text.lstrip(_STRIP))
    best = None
    for c in (_norm(x) for x in cands):
        if t.startswith(c) and (best is None or len(c) > len(best)):
            best = c
    return best


def _check_chain(rest: str, loc: dict[str, object], level: str) -> str | None:
    """rest 是根節點後面「→」之後的文字。回傳錯誤描述（None＝正確）。"""
    cands = loc["admin_children"] if level == "admin" else loc["adv_children"]
    hit = _match(rest, cands)  # type: ignore[arg-type]
    if hit is None:
        return f"「{rest.lstrip(_STRIP)[:30]}」不是這一層的選單"
    after = _norm(rest.lstrip(_STRIP))[len(hit):].lstrip(" 」』”\"*`")
    if not after.startswith(ARROW):
        return None
    nxt = after[1:]
    if hit == _norm(str(loc["integrations"])):
        if _match(nxt, loc["intg_children"]) is None:  # type: ignore[arg-type]
            return f"「{nxt.lstrip(_STRIP)[:30]}」不是外部系統整合裡的項目"
    elif hit == _norm(str(loc["system_settings"])):
        if _match(nxt, loc["ss_children"]) is None:  # type: ignore[arg-type]
            return f"「{nxt.lstrip(_STRIP)[:30]}」不是系統設定頁上的文字"
    return None


def _problems(text: str, labels: dict[str, dict[str, object]], locales: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for rx, why in FORBIDDEN:
        for m in rx.finditer(text):
            out.append(f"{why}：…{text[max(0, m.start() - 20):m.end() + 20]}…")
    roots: list[tuple[str, str]] = []
    for loc in locales:
        roots += [(str(labels[loc]["admin"]), "admin"), (str(labels[loc]["advanced"]), "advanced")]
    for root, level in set(roots):
        for m in re.finditer(re.escape(root) + r" ?" + ARROW, text):
            rest = text[m.end():]
            # 同一條路徑同一種語言：只要有一種語言整條對得上就算對
            errs = [_check_chain(rest, labels[loc], level) for loc in locales
                    if labels[loc][level] == root]
            if errs and all(e is not None for e in errs):
                out.append(f"{root} → {errs[0]}：…{text[max(0, m.start() - 10):m.end() + 40]}…")
    # 「系統設定 →」「外部系統整合 →」要從「管理」開始寫
    for loc in locales:
        for key in ("system_settings", "integrations"):
            label = str(labels[loc][key])
            for m in re.finditer(re.escape(label) + r" ?" + ARROW, text):
                before = text[max(0, m.start() - 4):m.start()]
                if ARROW not in before:
                    out.append(f"「{label} →」要從「{labels[loc]['admin']} →」開始寫：…{text[max(0, m.start() - 20):m.end() + 30]}…")
    return out


def _walk(o: object, path: str = ""):  # type: ignore[no-untyped-def]
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(o, str):
        yield path, o


def test_ui_strings_point_to_real_menus() -> None:
    labels = _labels()
    bad: list[str] = []
    for loc in LOCALES:
        for key, s in _walk(labels[loc]["strings"]):
            bad += [f"[{loc}] {key}: {p}" for p in _problems(s, labels, (loc,))]
    assert not bad, "說明裡的選單路徑對不上實際選單：\n" + "\n".join(bad)


def _doc_text(p: Path) -> str:
    t = p.read_text(encoding="utf-8")
    if p.suffix == ".html":
        t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return t


DOCS = [*sorted((ROOT / "docs").glob("*.html")), *sorted(ROOT.glob("README*.md")),
        *sorted(ROOT.glob("TEST_CHECKLIST*.md")),
        *[p for p in sorted((ROOT / "docs").glob("*.md")) if not p.name.startswith("SPEC")]]


def test_docs_point_to_real_menus() -> None:
    labels = _labels()
    bad: list[str] = []
    for p in DOCS:
        bad += [f"{p.relative_to(ROOT)}: {x}" for x in _problems(_doc_text(p), labels, LOCALES)]
    assert not bad, "文件裡的選單路徑對不上實際選單：\n" + "\n".join(bad)


def _backend_strings():  # type: ignore[no-untyped-def]
    """後端程式裡的字串（API 錯誤的退回訊息、AI 回覆、系統診斷的修正建議會直接給使用者看）。
    註解與說明文件字串（docstring）不算：那是寫給開發者看的。"""
    import ast
    for p in sorted((ROOT / "backend" / "app").rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docs: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                yield p, node.lineno, node.value


def test_backend_messages_point_to_real_menus() -> None:
    labels = _labels()
    bad: list[str] = []
    for p, line, s in _backend_strings():
        bad += [f"{p.relative_to(ROOT)}:{line}: {x}" for x in _problems(s, labels, LOCALES)]
    assert not bad, "後端訊息裡的選單路徑對不上實際選單：\n" + "\n".join(bad)


def test_the_menu_parser_sees_known_items() -> None:
    """解析器本身的健全性：看得到幾個一定存在的項目，否則上面兩支測試形同虛設。"""
    zh = _labels()["zh-TW"]
    assert zh["admin"] == "管理" and "系統設定" in zh["admin_children"] and "LLM / AI" in zh["admin_children"]
    assert "外部系統整合" in zh["admin_children"] and "OPNsense" in zh["intg_children"]
    assert "GeoIP（MaxMind）" in zh["ss_children"] and "RDP 連線引擎" in zh["ss_children"]
