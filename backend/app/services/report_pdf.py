"""PDF 報告（使用者 2026-10-08：「匯出 PDF 卻出了瀏覽器列印頁，這不對」）。

前端組好報告大綱（標題、基本資料、段落、條列、表格）送來，這裡排版成真正的 PDF 檔：
A4 直式、內嵌字型子集（中文／日文在任何閱讀器都顯示得出來）、表頭跨頁重複、頁尾頁碼。
DOCX／ODT 由前端產生，三種格式的版面規則刻意一致（顏色、欄寬算法、表格樣式）。

字型：Noto Sans CJK（apt 的 fonts-noto-cjk；install／upgrade 都會補）優先，依語系挑 TC／JP 字面；
沒有就退回文泉驛等其他 CJK 字型；一個都沒有時回明確的錯誤代碼，不產生一份中文全變方塊的檔案。
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any

from app.core.ui_error import UiError
from app.schemas.reports import ReportIn, ReportTableIn

# ── 色彩（與 frontend/src/utils/reportExport.ts 一致）──
ACCENT = (47, 93, 155)        # #2F5D9B 品牌色：標題下的線、段落標題前的色條
HEADING = (31, 58, 95)        # #1F3A5F 段落標題
HEAD_FILL = (47, 75, 124)     # #2F4B7C 表頭底色（白字）
INK = (31, 41, 55)            # #1F2937 內文
MUTED = (100, 110, 125)       # #646E7D 次要文字
RULE = (213, 219, 227)        # #D5DBE3 表格線
LABEL_FILL = (241, 244, 248)  # #F1F4F8 基本資料的欄名底色
ZEBRA = (246, 248, 251)       # #F6F8FB 斑馬紋
TONE = {"danger": (180, 35, 24), "warning": (181, 71, 8), "info": (23, 92, 211)}

MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 18.0, 16.0, 18.0     # mm；可用寬度 174 mm


class ReportPdfError(UiError):
    pass


@dataclass(frozen=True)
class FontChoice:
    regular: str
    regular_index: int
    bold: str
    bold_index: int
    name: str


# 依優先序：(一般, 粗體, 字面名稱要包含的字)。字面名稱用來在 .ttc 字型集裡挑出 TC／JP 那一個
_NOTO = [("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"),
         ("NotoSansCJKtc-Regular.otf", "NotoSansCJKtc-Bold.otf"),
         ("NotoSansCJKjp-Regular.otf", "NotoSansCJKjp-Bold.otf")]
_FALLBACK = ["wqy-microhei.ttc", "wqy-zenhei.ttc", "DroidSansFallbackFull.ttf", "DroidSansFallback.ttf"]
_FONT_DIRS = ["/usr/share/fonts", "/usr/local/share/fonts"]


def _find_file(name: str) -> str | None:
    for d in _FONT_DIRS:
        hits = sorted(glob.glob(os.path.join(d, "**", name), recursive=True))
        if hits:
            return hits[0]
    return None


def _face_index(path: str, want: str) -> int | None:
    """字型集（.ttc）裡字面名稱含 want（例：「CJK TC」）的那一個；單一字型檔回 0。"""
    if not path.lower().endswith(".ttc"):
        return 0
    from fontTools.ttLib import TTCollection  # type: ignore[import-untyped]
    try:
        coll = TTCollection(path, lazy=True)
        for i, face in enumerate(coll.fonts):
            if want in str(face["name"].getDebugName(1) or "") and "Mono" not in str(face["name"].getDebugName(1)):
                return i
    except Exception:
        return None
    return None


#: 只快取「找到了」的結果：沒找到時管理員補裝字型後，下一次匯出就要用得到（不必重啟）
_FONT_CACHE: dict[str, FontChoice] = {}


def clear_font_cache() -> None:
    _FONT_CACHE.clear()


def find_font(lang: str = "zh-TW") -> FontChoice | None:
    if lang not in _FONT_CACHE:
        found = _search_font(lang)
        if found is None:
            return None
        _FONT_CACHE[lang] = found
    return _FONT_CACHE[lang]


def _search_font(lang: str) -> FontChoice | None:
    want = "CJK JP" if lang == "ja-JP" else "CJK TC"
    for reg_name, bold_name in _NOTO:
        reg = _find_file(reg_name)
        if not reg:
            continue
        ri = _face_index(reg, want)
        if ri is None:
            continue
        bold = _find_file(bold_name) or reg
        bi = _face_index(bold, want)
        if bi is None:
            bold, bi = reg, ri
        return FontChoice(reg, ri, bold, bi, f"Noto Sans {want}")
    for name in _FALLBACK:
        path = _find_file(name)
        if path:
            # 這些沒有粗體：粗體也用同一個（表頭靠底色區分，不靠字重）
            return FontChoice(path, 0, path, 0, name.rsplit(".", 1)[0])
    return None


def font_status() -> dict[str, Any]:
    """版本資訊頁用：有沒有可用的中文字型、用的是哪一個。"""
    f = find_font("zh-TW")
    return {"present": f is not None, "name": f.name if f else None}


_CJK = re.compile(r"[\u2e80-\u9fff\u3000-\u303f\u3040-\u30ff\uac00-\ud7af\uff00-\uffef]")


def _short_latin(tok: str) -> bool:
    return 0 < len(tok) <= 5 and not _CJK.search(tok)


def _glue(text: str) -> str:
    """換行規則：以「字」為單位換行（位址、主機名稱不會從中間被切開），但中文沒有空白，
    整句會被當成一個字 —— 句子前面的「DNS」這種短英文就會單獨留在上一行。
    把「短英文＋中文」之間的空白換成不換行空白，讓它們黏成一段，過長時在行尾逐字切開。"""
    out = []
    for line in text.split("\n"):
        toks = line.split(" ")
        buf = toks[0]
        for prev, tok in pairwise(toks):
            a_cjk = bool(prev) and bool(_CJK.search(prev[-1]))
            b_cjk = bool(tok) and bool(_CJK.search(tok[0]))
            glue = (a_cjk and (b_cjk or _short_latin(tok))) or (b_cjk and _short_latin(prev))
            buf += ("\u00a0" if glue else " ") + tok
        out.append(buf)
    return "\n".join(out)


def _widths(t: ReportTableIn, total: float) -> list[float]:
    """欄寬比例換成實際寬度，加總一定等於可用寬度（表格不會超出右邊界）。"""
    n = max(len(t.cols), max((len(r) for r in t.rows), default=0), 1)
    w = t.widths if t.widths and len(t.widths) == n else [1.0] * n
    s = sum(w) or 1.0
    return [total * x / s for x in w]


def _render(doc: ReportIn, font: FontChoice) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import TableBordersLayout, TableCellFillMode, WrapMode, XPos, YPos
    from fpdf.fonts import FontFace

    wrap = WrapMode.WORD
    footer_left = " · ".join(x for x in (doc.footer_text or doc.brand, doc.generated_at) if x)

    class Pdf(FPDF):
        def header(self) -> None:
            if self.page_no() == 1:
                return
            self.set_font("cjk", size=8)
            self.set_text_color(*MUTED)
            self.set_xy(MARGIN_X, 8)
            self.cell(self.epw * 0.75, 4, doc.title[:90])
            self.cell(self.epw * 0.25, 4, doc.brand, align="R")
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(MARGIN_X, 13, self.w - MARGIN_X, 13)
            self.set_y(MARGIN_TOP)

        def footer(self) -> None:
            self.set_y(-12)
            self.set_draw_color(*RULE)
            self.set_line_width(0.2)
            self.line(MARGIN_X, self.get_y() - 1.5, self.w - MARGIN_X, self.get_y() - 1.5)
            self.set_font("cjk", size=8)
            self.set_text_color(*MUTED)
            self.cell(self.epw * 0.8, 4, footer_left)
            self.cell(self.epw * 0.2, 4, f"{self.page_no()} / {{nb}}", align="R")

    pdf = Pdf(format="A4", unit="mm")
    pdf.set_title(doc.title)
    pdf.set_creator(doc.brand)
    pdf.set_author(doc.brand)
    pdf.set_creation_date(datetime.now().astimezone())
    pdf.add_font("cjk", "", font.regular, collection_font_number=font.regular_index)
    pdf.add_font("cjk", "B", font.bold, collection_font_number=font.bold_index)
    pdf.set_margins(MARGIN_X, MARGIN_TOP, MARGIN_X)
    pdf.set_auto_page_break(True, margin=MARGIN_BOTTOM)
    pdf.add_page()

    def para(text: str, size: float = 10, color: tuple[int, int, int] = INK, bold: bool = False,
             h: float = 5.2, x: float | None = None, w: float = 0) -> None:
        pdf.set_font("cjk", "B" if bold else "", size)
        pdf.set_text_color(*color)
        if x is not None:
            pdf.set_x(x)
        pdf.multi_cell(w, h, _glue(text), wrapmode=wrap, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # ── 標題區 ──
    para(doc.brand, size=9, color=ACCENT, bold=True, h=4.5)
    pdf.ln(1)
    para(doc.title, size=18, bold=True, h=8.5)
    if doc.subtitle:
        para(doc.subtitle, size=10, color=MUTED, h=5)
    pdf.ln(2)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.7)
    pdf.line(MARGIN_X, pdf.get_y(), pdf.w - MARGIN_X, pdf.get_y())
    pdf.ln(5)

    def table(rows: list[list[str]], widths: list[float], *, head: list[str] | None, tones: list[Any] | None = None,
              label_cols: tuple[int, ...] = ()) -> None:
        pdf.set_font("cjk", size=8.8)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*RULE)
        pdf.set_line_width(0.2)
        with pdf.table(
            col_widths=widths, width=pdf.epw, align="LEFT", v_align="TOP", text_align="LEFT", wrapmode=wrap,
            line_height=4.4, padding=(1.4, 1.8), first_row_as_headings=head is not None,
            headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=HEAD_FILL),
            borders_layout=TableBordersLayout.HORIZONTAL_LINES if head is not None else TableBordersLayout.ALL,
            # 斑馬紋自己逐格給底色：交給 cell_fill_mode 的話，有自訂樣式的格子會沿用表頭的深色底
            cell_fill_mode=TableCellFillMode.NONE, repeat_headings=1,
        ) as t:
            if head is not None:
                hr = t.row()
                for c in head:
                    hr.cell(c)
            for i, r in enumerate(rows):
                tr = t.row()
                tone = (tones[i] if tones and i < len(tones) else None)
                fill = ZEBRA if head is not None and i % 2 == 1 else (255, 255, 255)
                for j in range(len(widths)):
                    text = r[j] if j < len(r) else ""
                    if j in label_cols:
                        style = FontFace(color=MUTED, fill_color=LABEL_FILL)
                    elif j == 0 and tone in TONE:
                        style = FontFace(emphasis="BOLD", color=TONE[tone], fill_color=fill)
                    else:
                        style = FontFace(color=INK, fill_color=fill)
                    tr.cell(_glue(text), style=style)
        pdf.ln(3)

    # ── 基本資料：每列兩組「欄名／值」 ──
    if doc.meta:
        pairs = list(doc.meta)
        rows = []
        for i in range(0, len(pairs), 2):
            a = pairs[i]
            b = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
            rows.append([a[0], a[1], b[0], b[1]])
        table(rows, [pdf.epw * x for x in (0.17, 0.33, 0.17, 0.33)], head=None, label_cols=(0, 2))
        pdf.ln(1)

    # ── 各段落 ──
    for s in doc.sections:
        if pdf.will_page_break(18):
            pdf.add_page()
        pdf.ln(2)
        y = pdf.get_y()
        pdf.set_fill_color(*ACCENT)
        pdf.rect(MARGIN_X, y + 0.6, 1.2, 5.2, style="F")
        para(s.heading, size=12.5, color=HEADING, bold=True, h=6.4, x=MARGIN_X + 3.2)
        pdf.ln(1.5)
        for p in s.paragraphs:
            para(p)
            pdf.ln(0.8)
        for item in s.bullets:
            pdf.set_font("cjk", size=10)
            pdf.set_text_color(*ACCENT)
            pdf.set_x(MARGIN_X + 1.5)
            pdf.cell(4, 5.2, "•")
            para(item, x=MARGIN_X + 5.5, w=pdf.epw - 5.5)
            pdf.ln(0.4)
        if s.table is not None:
            if s.table.rows:
                table(s.table.rows, _widths(s.table, pdf.epw), head=list(s.table.cols), tones=s.table.tones)
            else:
                para("—", color=MUTED)
        if s.caption:
            para(s.caption, size=8.5, color=MUTED, h=4.4)
        pdf.ln(1)

    # ── 結尾說明 ──
    if doc.note:
        pdf.ln(2)
        pdf.set_fill_color(*ZEBRA)
        pdf.set_font("cjk", size=8.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(0, 4.6, _glue(doc.note), fill=True, padding=3, wrapmode=wrap, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())


#: 同時最多排幾份（每份可能吃到 200 MB、好幾秒 CPU）
_SEM = asyncio.Semaphore(2)


async def render_pdf(doc: ReportIn) -> bytes:
    font = find_font(doc.lang)
    if font is None:
        raise ReportPdfError("no CJK font installed (apt install fonts-noto-cjk)", code="report_pdf_no_font")
    try:
        await asyncio.wait_for(_SEM.acquire(), timeout=30)
    except TimeoutError as exc:
        raise ReportPdfError("too many reports being generated, try again", code="report_pdf_busy") from exc
    try:
        return await asyncio.to_thread(_render, doc, font)
    except ReportPdfError:
        raise
    except Exception as exc:
        raise ReportPdfError(f"PDF rendering failed: {exc.__class__.__name__}: {exc}",
                             code="report_pdf_failed", reason=f"{exc.__class__.__name__}: {exc}"[:300]) from exc
    finally:
        _SEM.release()
