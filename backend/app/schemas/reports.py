"""報告文件（PDF 產生用）。前端把畫面上組好的報告大綱送來，後端只負責排版成 PDF ——
不查任何資料，所以權限只要求登入；內容的可見範圍在前端組大綱時就已經照權限過濾過。

上限是為了擋住惡意或失控的請求把 CPU／記憶體吃光（排一份 300 列的表約 4 秒、170 MB）。
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.schemas.base import StrictModel

Cell = Annotated[str, Field(max_length=4000)]
Short = Annotated[str, Field(max_length=300)]
Tone = Literal["danger", "warning", "info"]

MAX_CELLS = 12_000          # 全部表格加起來的儲存格數
MAX_TEXT = 1_500_000        # 全部文字加起來的字數


class ReportTableIn(StrictModel):
    cols: Annotated[list[Short], Field(min_length=1, max_length=12)]
    rows: Annotated[list[Annotated[list[Cell], Field(max_length=12)]], Field(max_length=3000)]
    # 各欄寬度比例（不給就平均）
    widths: Annotated[list[Annotated[float, Field(gt=0, le=1000)]], Field(max_length=12)] | None = None
    # 每一列的語氣（阻擋＝danger…），只影響第一欄的顏色
    tones: Annotated[list[Tone | None], Field(max_length=3000)] | None = None


class ReportSectionIn(StrictModel):
    heading: Short
    paragraphs: Annotated[list[Cell], Field(max_length=100)] = []
    bullets: Annotated[list[Annotated[str, Field(max_length=2000)]], Field(max_length=1000)] = []
    table: ReportTableIn | None = None
    # 段落最後的小字說明（例：「AI 只根據這份結果解說…」）
    caption: Annotated[str, Field(max_length=1000)] = ""


class ReportIn(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=300)]
    subtitle: Annotated[str, Field(max_length=500)] = ""
    brand: Annotated[str, Field(max_length=60)] = "jt-ipam"
    generated_at: Annotated[str, Field(max_length=60)] = ""
    meta: Annotated[list[tuple[Short, Annotated[str, Field(max_length=1000)]]], Field(max_length=60)] = []
    sections: Annotated[list[ReportSectionIn], Field(max_length=60)] = []
    note: Annotated[str, Field(max_length=2000)] = ""
    footer_text: Annotated[str, Field(max_length=200)] = ""
    lang: Literal["zh-TW", "en-US", "ja-JP"] = "zh-TW"
    filename: Annotated[str, Field(max_length=150)] = "report"

    @model_validator(mode="after")
    def _bounded(self) -> ReportIn:
        cells = sum(len(r) for s in self.sections if s.table for r in s.table.rows)
        if cells > MAX_CELLS:
            raise ValueError(f"too many table cells ({cells} > {MAX_CELLS})")
        text = sum(len(c) for s in self.sections if s.table for r in s.table.rows for c in r)
        text += sum(len(p) for s in self.sections for p in [*s.paragraphs, *s.bullets, s.caption])
        if text > MAX_TEXT:
            raise ValueError(f"too much text ({text} > {MAX_TEXT})")
        return self
