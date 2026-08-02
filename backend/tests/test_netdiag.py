"""網路診斷：目標驗證與輸出解析。不打真的網路 —— 用實機取得的輸出樣本。"""

from __future__ import annotations

import pytest
from app.services import netdiag as nd

# 本機 iputils ping 的實際輸出
_PING_OK = """PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=113 time=5.49 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=113 time=5.31 ms

--- 8.8.8.8 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 1001ms
rtt min/avg/max/mdev = 5.310/5.400/5.490/0.090 ms
"""
_PING_DEAD = """PING 192.0.2.1 (192.0.2.1) 56(84) bytes of data.

--- 192.0.2.1 ping statistics ---
2 packets transmitted, 0 received, 100% packet loss, time 1024ms
"""
# tracepath 的實際輸出格式（位址改為文件保留範圍 RFC 5737，格式一字未改）
_TRACEPATH = """ 1?: [LOCALHOST]                      pmtu 1500
 1:  192.0.2.1                                           0.886ms
 1:  192.0.2.1                                           0.632ms
 2:  no reply
 3:  198.51.100.14                                        5.045ms
 4:  no reply
 5:  203.0.113.70                                         6.294ms asymm  7
     Too many hops: pmtu 1500
"""


def test_parse_ping_alive():
    r = nd.parse_ping_output("8.8.8.8", _PING_OK)
    assert r.alive is True
    assert (r.sent, r.received) == (2, 2)
    assert r.loss_pct == 0.0
    assert r.rtt_avg_ms == 5.400


def test_parse_ping_unreachable():
    r = nd.parse_ping_output("192.0.2.1", _PING_DEAD)
    assert r.alive is False
    assert r.received == 0
    assert r.loss_pct == 100.0
    assert r.rtt_avg_ms is None


def test_tracepath_keeps_hops_that_did_not_answer():
    """沒回應的躍點必須列出來。

    省略掉的話路徑看起來是 1→3→5 中間沒東西，讀的人會以為那幾跳不存在，
    而不是「不回應」—— 那是誤導，也是這裡曾經的 bug。
    """
    res = nd.parse_tracepath("8.8.8.8", _TRACEPATH)
    assert res.path_mtu == 1500
    assert [h.hop for h in res.hops] == [1, 2, 3, 4, 5]     # 2 與 4 不可消失
    assert res.hops[1].host is None
    assert res.hops[1].note == "無回應"
    assert res.hops[0].host == "192.0.2.1"                # 同一跳重複探測只留一筆
    assert res.hops[4].rtt_ms == 6.294


@pytest.mark.parametrize("raw", ["8.8.8.8", "example.com", "sub.example.co.uk", "2001:db8::1"])
def test_accepts_addresses_and_hostnames(raw):
    assert nd.normalize_target(raw)


@pytest.mark.parametrize("raw", [
    "$(whoami)", "`id`", "8.8.8.8|cat /etc/passwd", "../../etc/passwd",
    "-oProxyCommand=x", "", "   ", "a" * 300,
])
def test_rejects_anything_that_is_not_a_target(raw):
    """不經 shell 所以本來就不可能注入；這是第二道防線，也擋掉會被誤當旗標的字串。"""
    with pytest.raises(nd.NetDiagError):
        nd.normalize_target(raw)


def test_cidr_expands_to_hosts():
    assert nd.expand_targets("192.0.2.0/30") == ["192.0.2.1", "192.0.2.2"]


def test_mixed_separators_and_dedup():
    assert nd.expand_targets("8.8.8.8, 8.8.8.8\n1.1.1.1  8.8.4.4") == [
        "8.8.8.8", "1.1.1.1", "8.8.4.4"]


def test_target_cap_is_enforced():
    """沒有上限的話一個 /16 就是六萬多個目標，等於把伺服器當掃描器用。"""
    with pytest.raises(nd.NetDiagError, match="上限"):
        nd.expand_targets("10.0.0.0/16")
    assert len(nd.expand_targets(f"192.0.2.0/{32 - 6}")) <= nd.MAX_TARGETS


def test_unavailable_is_a_distinct_error():
    """端點靠這個分辨 501（環境缺工具）與其他錯誤；混在一起會把兩種問題講成同一種。"""
    assert issubclass(nd.NetDiagUnavailable, nd.NetDiagError)


def test_tool_availability_shape():
    caps = nd.tool_availability()
    assert set(caps) == {"ping", "tracepath", "traceroute", "tcp"}
    assert caps["tcp"] is True      # 純 Python，不依賴外部指令
