"""ISOinsight 整合：租約回應的解析與正規化（純函式，不碰網路與資料庫）。

樣本是依截圖欄位做的匿名資料（RFC 5737 位址、文件用 MAC），**不是**原廠格式的保證：
真機的時間格式、時區、是否全量都還待驗證。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from app.services.isoinsight import parser as P
from app.services.isoinsight.errors import IsoError

TZ = "Asia/Taipei"
# 台北時間 2026/10/07 16:00:00 = UTC 08:00:00
NOW = datetime(2026, 10, 7, 8, 0, 0, tzinfo=UTC)


def _body(rows, **extra) -> bytes:
    return json.dumps({"dhcp_lease": rows, **extra}).encode()


def _row(ip="192.0.2.20", mac="02:00:5e:00:53:20", name="laptop-07",
         start="2026/10/07 15:51:40", end="2026/10/07 19:51:40"):
    return {"ip": ip, "mac": mac, "name": name, "start_time": start, "end_time": end}


def _parse(body: bytes, *, status=200, ctype="application/json", max_rows=100_000):
    return P.parse_leases(status, ctype, body, tz=TZ, now=NOW, max_rows=max_rows)


# ── 結構 ─────────────────────────────────────────────────────────────────────

def test_normal_response_is_parsed_and_times_go_to_utc() -> None:
    got = _parse(_body([_row()]))
    assert got.fetched == 1 and got.invalid == 0 and len(got.leases) == 1
    le = got.leases[0]
    assert le.ip == "192.0.2.20" and le.version == 4
    assert le.mac == "02:00:5e:00:53:20" and le.mac_key == "02005e005320"
    assert le.name == "laptop-07"
    assert le.start == datetime(2026, 10, 7, 7, 51, 40, tzinfo=UTC)
    assert le.end == datetime(2026, 10, 7, 11, 51, 40, tzinfo=UTC)
    assert le.state == "active"
    assert got.warnings == []


def test_empty_list_is_a_valid_empty_response() -> None:
    got = _parse(_body([]))
    assert got.fetched == 0 and got.leases == []


@pytest.mark.parametrize("body", [
    b'{"other": []}',                     # 缺 dhcp_lease
    b'{"dhcp_lease": null}',
    b'{"dhcp_lease": "192.0.2.1"}',
    b'{"dhcp_lease": {"ip": "192.0.2.1"}}',
    b'[]',                                # 頂層不是物件
    b'null',
    b'"text"',
    b'{"dhcp_lease": [',                  # 壞 JSON
    b'',
])
def test_malformed_structures_are_errors_not_empty_lists(body: bytes) -> None:
    with pytest.raises(IsoError) as ei:
        _parse(body)
    assert ei.value.spec_code == "INVALID_RESPONSE"


def test_html_is_rejected_even_with_a_json_content_type() -> None:
    with pytest.raises(IsoError) as ei:
        _parse(b"<!DOCTYPE html><html><body>login</body></html>")
    assert ei.value.spec_code == "INVALID_RESPONSE"
    assert ei.value.params.get("kind") == "html"


def test_html_content_type_is_rejected() -> None:
    with pytest.raises(IsoError) as ei:
        _parse(b'{"dhcp_lease": []}', ctype="text/html; charset=utf-8")
    assert ei.value.spec_code == "INVALID_RESPONSE"


def test_204_is_not_an_empty_lease_list() -> None:
    with pytest.raises(IsoError) as ei:
        _parse(b"", status=204)
    assert ei.value.spec_code == "INVALID_RESPONSE"


def test_wrong_content_type_with_valid_json_is_a_warning() -> None:
    got = _parse(_body([_row()]), ctype="text/plain")
    assert len(got.leases) == 1
    assert "content_type_mismatch" in got.warnings


def test_unknown_fields_are_ignored() -> None:
    row = {**_row(), "vendor": "x", "extra": {"a": 1}}
    got = _parse(_body([row], version="9.9"))
    assert len(got.leases) == 1


def test_charset_from_content_type_is_honoured() -> None:
    body = json.dumps({"dhcp_lease": [_row(name="會議室-01")]}, ensure_ascii=False).encode("big5")
    got = _parse(body, ctype="application/json; charset=big5")
    assert got.leases[0].name == "會議室-01"


def test_utf8_bom_is_accepted() -> None:
    got = _parse(b"\xef\xbb\xbf" + _body([_row()]))
    assert len(got.leases) == 1


@pytest.mark.parametrize("extra", [
    {"total": 5},                          # 回了 1 筆、說總共 5 筆
    {"has_more": True},
    {"next": "/isosvc?act=DhcpLease&page=2"},
    {"page": 1, "total_pages": 3},
])
def test_recognisable_unfinished_pagination_is_incomplete(extra: dict) -> None:
    with pytest.raises(IsoError) as ei:
        _parse(_body([_row()], **extra))
    assert ei.value.spec_code == "INCOMPLETE_RESPONSE"


@pytest.mark.parametrize("extra", [
    {"total": 1}, {"has_more": False}, {"next": None}, {"page": 3, "total_pages": 3},
])
def test_finished_pagination_markers_are_fine(extra: dict) -> None:
    assert len(_parse(_body([_row()], **extra)).leases) == 1


def test_row_limit_fails_instead_of_truncating() -> None:
    rows = [_row(ip=f"192.0.2.{i}", mac=f"02:00:5e:00:53:{i:02x}") for i in range(1, 12)]
    with pytest.raises(IsoError) as ei:
        _parse(_body(rows), max_rows=10)
    assert ei.value.spec_code == "RESPONSE_LIMIT_EXCEEDED"


# ── 逐筆欄位 ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ip", ["", None, "999.1.1.1", "192.0.2.0/24", "not-an-ip", 12345])
def test_invalid_ip_skips_the_whole_row(ip) -> None:
    got = _parse(_body([_row(ip=ip), _row(ip="192.0.2.21", mac="02:00:5e:00:53:21")]))
    assert got.fetched == 2 and got.invalid == 1
    assert [le.ip for le in got.leases] == ["192.0.2.21"]
    assert got.invalid_samples and got.invalid_samples[0]["reason"] == "ip_invalid"


def test_non_object_rows_are_invalid() -> None:
    got = _parse(_body(["192.0.2.1", None, _row()]))
    assert got.invalid == 2 and len(got.leases) == 1


def test_ipv6_is_parsed_but_marked_unverified() -> None:
    got = _parse(_body([_row(ip="2001:DB8::10")]))
    le = got.leases[0]
    assert le.ip == "2001:db8::10" and le.version == 6
    assert "ipv6_unverified" in le.quality


@pytest.mark.parametrize(("raw", "mac", "tag"), [
    ("02-00-5E-00-53-20", "02:00:5e:00:53:20", None),
    ("0200.5e00.5320", "02:00:5e:00:53:20", None),
    ("02005e005320", "02:00:5e:00:53:20", None),
    ("", None, "mac_empty"),
    (None, None, "mac_empty"),
    ("zz:zz", None, "mac_invalid"),
    ("00:00:00:00:00:00", None, "mac_invalid"),
    ("ff:ff:ff:ff:ff:ff", None, "mac_invalid"),
])
def test_mac_normalisation_and_quality(raw, mac, tag) -> None:
    le = _parse(_body([_row(mac=raw)])).leases[0]
    assert le.mac == mac
    if tag:
        assert tag in le.quality and le.mac_key == ""
    else:
        assert not ({"mac_empty", "mac_invalid"} & le.quality)


def test_empty_name_is_none_and_tagged_but_not_a_problem() -> None:
    le = _parse(_body([_row(name="  ")])).leases[0]
    assert le.name is None and "name_empty" in le.quality
    assert not (le.quality & P.PROBLEM_TAGS)


def test_name_is_cleaned_of_control_characters_and_truncated() -> None:
    le = _parse(_body([_row(name="ab\x00c\x1bd" + "x" * 400)])).leases[0]
    assert "\x00" not in le.name and "\x1b" not in le.name
    assert len(le.name) <= 255


# ── 時間與租約狀態 ───────────────────────────────────────────────────────────

def test_timezone_is_applied_not_the_host_timezone() -> None:
    utc = P.parse_leases(200, "application/json", _body([_row()]), tz="UTC", now=NOW, max_rows=10)
    tpe = _parse(_body([_row()]))
    assert (utc.leases[0].start - tpe.leases[0].start).total_seconds() == 8 * 3600


def test_expiry_boundary_end_equal_now_is_expired() -> None:
    # 台北 16:00:00 = NOW
    le = _parse(_body([_row(start="2026/10/07 12:00:00", end="2026/10/07 16:00:00")])).leases[0]
    assert le.state == "expired"
    le = _parse(_body([_row(start="2026/10/07 16:00:00", end="2026/10/07 16:00:01")])).leases[0]
    assert le.state == "active"


def test_future_start_is_flagged() -> None:
    le = _parse(_body([_row(start="2026/10/07 17:00:00", end="2026/10/07 21:00:00")])).leases[0]
    assert le.state == "not_started" and "not_started" in le.quality


def test_end_before_start_is_an_invalid_period() -> None:
    le = _parse(_body([_row(start="2026/10/07 15:00:00", end="2026/10/07 14:00:00")])).leases[0]
    assert le.state == "invalid_period" and "invalid_period" in le.quality


@pytest.mark.parametrize("raw", ["", None, "Permanent", "infinite", "2026-10-07T15:51:40Z", 1696666300, "2026/13/40 25:61:61"])
def test_unparseable_times_are_unknown_never_permanent(raw) -> None:
    le = _parse(_body([_row(end=raw)])).leases[0]
    assert le.end is None and le.state == "unknown"
    assert "time_unknown" in le.quality
    if isinstance(raw, str) and raw:
        assert le.end_raw == raw[:40]


def test_expired_with_unknown_start_is_still_expired() -> None:
    le = _parse(_body([_row(start="", end="2026/10/07 10:00:00")])).leases[0]
    assert le.state == "expired"


# ── 重複與衝突 ──────────────────────────────────────────────────────────────

def test_same_ip_same_mac_duplicates_collapse_to_the_latest_start() -> None:
    rows = [_row(start="2026/10/07 10:00:00", end="2026/10/07 14:00:00"),
            _row(start="2026/10/07 15:00:00", end="2026/10/07 19:00:00"),
            _row(start="2026/10/07 15:00:00", end="2026/10/07 18:00:00")]
    got = _parse(_body(rows))
    assert got.fetched == 3 and got.duplicates == 2 and len(got.leases) == 1
    le = got.leases[0]
    assert le.raw_count == 3 and "duplicate" in le.quality
    assert le.end == datetime(2026, 10, 7, 11, 0, 0, tzinfo=UTC)


def test_overlapping_leases_with_different_macs_are_a_conflict_without_a_winner() -> None:
    rows = [_row(mac="02:00:5e:00:53:01"), _row(mac="02:00:5e:00:53:02")]
    got = _parse(_body(rows))
    sel = P.pick_current(got.leases)
    assert sel["192.0.2.20"].current is None
    assert sel["192.0.2.20"].conflict is True
    assert all("mac_conflict" in le.quality for le in got.leases)


def test_expired_history_and_current_lease_do_not_conflict() -> None:
    rows = [_row(mac="02:00:5e:00:53:01", start="2026/10/07 08:00:00", end="2026/10/07 12:00:00"),
            _row(mac="02:00:5e:00:53:02", start="2026/10/07 12:30:00", end="2026/10/07 20:00:00")]
    got = _parse(_body(rows))
    sel = P.pick_current(got.leases)["192.0.2.20"]
    assert sel.conflict is False
    assert sel.current is not None and sel.current.mac == "02:00:5e:00:53:02"


def test_a_lease_without_mac_does_not_conflict_with_one_that_has_it() -> None:
    rows = [_row(mac="02:00:5e:00:53:01"), _row(mac="", start="2026/10/07 15:00:00")]
    got = _parse(_body(rows))
    sel = P.pick_current(got.leases)["192.0.2.20"]
    assert sel.conflict is False and sel.current.mac == "02:00:5e:00:53:01"
    assert any(le.mac is None for le in sel.superseded)


def test_problem_tags_make_the_quality_summary() -> None:
    rows = [_row(), _row(ip="192.0.2.30", mac="bad"), _row(ip="192.0.2.31", mac="02:00:5e:00:53:31", end="x")]
    got = _parse(_body(rows))
    qc = got.quality_counts()
    assert qc["mac_invalid"] == 1 and qc["time_unknown"] == 1
    assert got.has_problems()
