"""IP 變更評估匯出的 Markdown 儲存格跳脫（同前端 CodeQL #54：反斜線沒先跳脫，`a\\|b` 會讓欄位錯位）。"""
from app.services.change_impact.export import _cell


def test_backslash_and_pipe_are_escaped() -> None:
    assert _cell("a\\|b") == "a\\\\\\|b"
    assert _cell("C:\\path") == "C:\\\\path"
    assert _cell("x|y\nz") == "x\\|y z"
    assert _cell(None) == ""


def test_truncation_never_splits_an_escape() -> None:
    out = _cell("|" * 300)
    assert out == "\\|" * 200
