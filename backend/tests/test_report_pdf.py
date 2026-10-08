"""PDF 報告（使用者 2026-10-08：「匯出 PDF 卻出了瀏覽器列印頁，這不對」）。

後端把前端組好的報告大綱排成真正的 PDF：要內嵌中文字型子集（任何閱讀器都顯示得出來），
表格不可以超出右邊界，輸入有上限。
"""
from __future__ import annotations

import re
import zlib

import pytest
from app.schemas.reports import ReportIn, ReportTableIn
from app.services import report_pdf

DOC = {
    "title": "192.0.2.10 改為 192.0.2.20",
    "subtitle": "IP 變更評估 · 第 1 版",
    "generated_at": "2026-10-08 15:20",
    "filename": "change-impact-192.0.2.10_改為_192.0.2.20",
    "meta": [["評估目標", "192.0.2.10"], ["決策狀態", "有阻擋項目"], ["阻擋", "2"]],
    "sections": [
        {"heading": "AI 摘要", "bullets": ["變更被阻擋：新位址已經登記在用。"], "caption": "AI 只根據這份結果解說。"},
        {"heading": "影響清單", "table": {
            "cols": ["處置／嚴重度", "物件", "原因", "影響／證據"], "widths": [14, 28, 44, 14],
            "tones": ["danger", None],
            "rows": [["阻擋\n嚴重", "IP 位址\n192.0.2.20 host-b", "新位址 192.0.2.20 已經登記在用", "可能中斷\n明確"],
                     ["參考\n低", "防火牆規則\nrouter-1 #8", "規則同時涵蓋新舊位址", "僅有引用\n明確"]]}},
        {"heading": "空的表", "table": {"cols": ["a"], "rows": []}},
    ],
    "note": "這是評估，不會執行任何變更。",
}

needs_font = pytest.mark.skipif(report_pdf.find_font("zh-TW") is None, reason="這台沒有中文字型（fonts-noto-cjk）")


def _streams(pdf: bytes) -> list[bytes]:
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            out.append(zlib.decompress(m.group(1)))
        except zlib.error:
            out.append(m.group(1))
    return out


async def test_login_required(client) -> None:
    r = await client.post("/api/v1/reports/pdf", json=DOC)
    assert r.status_code == 401


@needs_font
async def test_real_pdf_with_embedded_cjk_font_subset(client, auth_headers) -> None:
    r = await client.post("/api/v1/reports/pdf", json=DOC, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    body = r.content
    assert body.startswith(b"%PDF-")
    # 字型檔真的內嵌（子集名稱是 6 個大寫字母＋「+」）：不靠閱讀器剛好有中文字型
    assert re.search(rb"/FontFile[23]", body)
    assert re.search(rb"/BaseFont\s*/[A-Z]{6}\+", body)
    # 文字可以被複製／搜尋：ToUnicode 對照表裡有「評」（U+8A55）與「阻」（U+963B）
    cmaps = b"".join(s for s in _streams(body) if b"begincmap" in s)
    assert b"<8A55>" in cmaps.upper() and b"<963B>" in cmaps.upper()
    # 下載檔名：中文用 RFC 5987，另有 ASCII 退路
    cd = r.headers["content-disposition"]
    assert "filename*=UTF-8''change-impact-192.0.2.10_%E6%94%B9%E7%82%BA_192.0.2.20.pdf" in cd
    assert 'filename="' in cd


@needs_font
async def test_japanese_uses_the_japanese_face(client, auth_headers) -> None:
    f = report_pdf.find_font("ja-JP")
    assert f is not None
    r = await client.post("/api/v1/reports/pdf", json={**DOC, "lang": "ja-JP", "title": "変更評価レポート"},
                          headers=auth_headers)
    assert r.status_code == 200, r.text
    if f.name.startswith("Noto"):
        assert b"NotoSansCJKJP" in r.content


async def test_no_cjk_font_is_a_clear_error_not_a_pdf_full_of_boxes(client, auth_headers, monkeypatch) -> None:
    monkeypatch.setattr(report_pdf, "_search_font", lambda lang: None)
    report_pdf.clear_font_cache()
    try:
        r = await client.post("/api/v1/reports/pdf", json=DOC, headers=auth_headers)
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "report_pdf_no_font"
        # 補裝字型後不必重啟：「沒找到」不會被記住
        monkeypatch.undo()
        if report_pdf.find_font("zh-TW") is not None:
            assert (await client.post("/api/v1/reports/pdf", json=DOC, headers=auth_headers)).status_code == 200
    finally:
        report_pdf.clear_font_cache()


async def test_oversized_or_unknown_input_is_rejected(client, auth_headers) -> None:
    big = {**DOC, "sections": [{"heading": "x", "table": {"cols": ["a"] * 12, "rows": [["1"] * 12] * 1100}}]}
    assert (await client.post("/api/v1/reports/pdf", json=big, headers=auth_headers)).status_code == 422
    rows = {**DOC, "sections": [{"heading": "x", "table": {"cols": ["a"], "rows": [["1"]] * 3001}}]}
    assert (await client.post("/api/v1/reports/pdf", json=rows, headers=auth_headers)).status_code == 422
    extra = {**DOC, "script": "alert(1)"}
    assert (await client.post("/api/v1/reports/pdf", json=extra, headers=auth_headers)).status_code == 422


def test_column_widths_always_add_up_to_the_text_width() -> None:
    """欄寬比例怎麼給（沒給、給錯數量、加總不是 100）都要剛好等於可用寬度，表格才不會超出右邊。"""
    for widths in (None, [14, 28, 44, 14], [1, 1], [3, 7, 9]):
        t = ReportTableIn(cols=["a", "b", "c", "d"][: len(widths) if widths and len(widths) <= 4 else 4],
                          rows=[], widths=widths)
        w = report_pdf._widths(t, 174.0)
        assert abs(sum(w) - 174.0) < 1e-6


def test_model_accepts_the_frontend_document() -> None:
    doc = ReportIn.model_validate(DOC)
    assert doc.sections[1].table is not None
    assert doc.sections[1].table.tones == ["danger", None]
    # 前端 pdfPayload 的實際形狀：沒有的欄位送 null／空字串
    ReportIn.model_validate({"title": "t", "subtitle": "", "brand": "jt-ipam", "generated_at": "", "meta": [],
                             "note": "", "footer_text": "", "lang": "en-US", "filename": "x",
                             "sections": [{"heading": "h", "paragraphs": [], "bullets": ["b"], "caption": "",
                                           "table": None},
                                          {"heading": "t", "paragraphs": [], "bullets": [], "caption": "",
                                           "table": {"cols": ["a"], "rows": [["1"]], "widths": None, "tones": None}}]})


async def test_falls_back_to_another_cjk_font_when_noto_is_missing(client, auth_headers, monkeypatch) -> None:
    """站台還沒裝 fonts-noto-cjk（升級時 apt 連不上）：有文泉驛之類的就用它，PDF 照樣產生。"""
    monkeypatch.setattr(report_pdf, "_NOTO", [])
    report_pdf.clear_font_cache()
    try:
        f = report_pdf.find_font("zh-TW")
        if f is None:
            pytest.skip("這台沒有任何備用 CJK 字型")
        assert not f.name.startswith("Noto")
        r = await client.post("/api/v1/reports/pdf", json=DOC, headers=auth_headers)
        assert r.status_code == 200, r.text
        assert re.search(rb"/FontFile[23]", r.content)
    finally:
        report_pdf.clear_font_cache()
