"""CubeCOS／OpenStack 整合對真伺服器的測試（選用；沒設環境變數就跳過）。

CubeCOS（scripts/cubecos-test-target/fake_cubecos.py，照 cube-cos-api v3.1.0 原始碼的測試靶）：
OpenStack 的部分用假雲（tests/openstack_fake.py），只有 CubeCOS 的請求真的送到測試靶 —— 驗證外框、
Keystone token、分頁 bug、未驗證時的 401／302／HTML，以及「jt-ipam 從來沒有對 CubeCOS 送過寫入」。

    # 測試靶要接受假雲發的 token（gAAAAAB-token-1…4）
    ACCEPT_KEYSTONE_TOKENS=gAAAAAB-token-1,gAAAAAB-token-2,gAAAAAB-token-3,gAAAAAB-token-4 \\
      python3 scripts/cubecos-test-target/fake_cubecos.py --listen <私網 IP> --port 18471
    CUBECOS_UNAUTH_MODE=redirect302 python3 .../fake_cubecos.py --listen <私網 IP> --port 18472   # 選用
    CUBECOS_UNAUTH_MODE=html200     python3 .../fake_cubecos.py --listen <私網 IP> --port 18473   # 選用
    ACCEPT_KEYSTONE_TOKENS=… CUBECOS_PATH_PREFIX=/cos-api python3 .../fake_cubecos.py … --port 18474  # 選用

    JTIPAM_CUBECOS_LIVE_URL=https://<私網 IP>:18471 \\
    JTIPAM_CUBECOS_LIVE_REDIRECT_URL=https://<私網 IP>:18472 \\
    JTIPAM_CUBECOS_LIVE_HTML_URL=https://<私網 IP>:18473 \\
    JTIPAM_CUBECOS_LIVE_PREFIX_URL=https://<私網 IP>:18474/cos-api \\
    JTIPAM_CUBECOS_LIVE_SAML_URL=https://<私網 IP>:18475 pytest tests/test_openstack_live.py   # 18475：不接受 token

OpenStack（DevStack 之類的真雲，全部走真的 Keystone／Nova／Neutron）：

    JTIPAM_OPENSTACK_LIVE_URL=https://<keystone>/identity/v3 JTIPAM_OPENSTACK_LIVE_USER=… \\
    JTIPAM_OPENSTACK_LIVE_PASSWORD=… JTIPAM_OPENSTACK_LIVE_PROJECT=admin \\
    [JTIPAM_OPENSTACK_LIVE_REGION=RegionOne] [JTIPAM_OPENSTACK_LIVE_CA=/path/ca.pem | JTIPAM_OPENSTACK_LIVE_INSECURE=1]
"""
from __future__ import annotations

import os
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from app.models.openstack import OpenStackInstance, OpenStackPort, OpenStackSubnet
from app.models.virt import VirtualMachine
from app.services import openstack as svc
from sqlalchemy import func, select

from tests import openstack_fake as F

pytestmark = pytest.mark.anyio

LIVE = os.environ.get("JTIPAM_CUBECOS_LIVE_URL")


def _ca_of(url: str) -> str:
    sp = urlsplit(url)
    r = httpx.get(f"{sp.scheme}://{sp.netloc}/_test/ca.pem", verify=False, timeout=5)  # noqa: S501 -- 測試靶自己的 CA
    r.raise_for_status()
    return r.text


def _write_calls(url: str) -> int:
    sp = urlsplit(url)
    r = httpx.get(f"{sp.scheme}://{sp.netloc}/_test/write_calls", verify=False, timeout=5)  # noqa: S501
    return int(r.json()["count"])


class _Router(httpx.AsyncBaseTransport):
    """CubeCOS 測試靶的請求走真的 GuardedTransport（連線當下的 SSRF 檢查、TLS 驗證照常），其他給假雲。"""

    def __init__(self, live: str, verify: Any, cloud: F.FakeCloud) -> None:
        from app.core.safe_http import GuardedTransport
        sp = urlsplit(live)
        self.netloc = (sp.hostname, sp.port)
        self.real = GuardedTransport(verify=verify)
        self.mock = cloud.transport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.host, request.url.port) == self.netloc:
            return await self.real.handle_async_request(request)
        return await self.mock.handle_async_request(request)

    async def aclose(self) -> None:
        await self.real.aclose()


def _use_live(monkeypatch, live: str, cloud: F.FakeCloud) -> None:
    def _make(verify: Any, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_Router(live, verify, cloud), timeout=timeout, follow_redirects=False)
    monkeypatch.setattr(svc, "_http_client", _make)


async def _inst(db, url: str, *, ca: str | None) -> OpenStackInstance:
    inst = OpenStackInstance(id=uuid.uuid4(), name=f"cube-{uuid.uuid4().hex[:6]}", product="cubecos",
                             auth_url=f"{F.KEYSTONE}/v3", username="ipam-reader", project_name="admin",
                             region="RegionOne", cubecos_url=url, ca_cert=ca, secret_enc=b"x", secret_nonce=b"y")
    inst.secret_enc, inst.secret_nonce = svc.encrypt_secret_for(inst.id, "Secret-2026")
    db.add(inst)
    await db.flush()
    return inst


def _cloud() -> F.FakeCloud:
    c = F.FakeCloud()
    c.networks = [F.network(F.uid(0x500), "private")]
    c.servers = [F.server(F.uid(0xA1), "web-1", host="os-compute-1")]
    c.subnets = [F.subnet(F.uid(0xC1), "198.51.100.0/24")]
    c.ports = [F.port(F.uid(0xB1), device_id=F.uid(0xA1), fixed=[(F.uid(0xC1), "198.51.100.21")])]
    return c


@pytest.mark.skipif(not LIVE, reason="JTIPAM_CUBECOS_LIVE_URL not set")
async def test_live_cubecos_nodes_with_keystone_token(db_session, monkeypatch) -> None:
    assert LIVE
    before = _write_calls(LIVE)
    ca = _ca_of(LIVE)
    inst = await _inst(db_session, LIVE, ca=ca)
    _use_live(monkeypatch, LIVE, _cloud())
    out = await svc.sync_instance(db_session, inst)
    cap = inst.capabilities["services"]["cubecos"]
    assert cap["ok"] is True, cap
    assert cap["datacenter"] == "fake-dc"
    hosts = sorted(n["hostname"] for n in inst.nodes)
    assert hosts == ["os-compute-1", "os-compute-2", "os-ctrl-1"]
    by = {n["hostname"]: n for n in inst.nodes}
    # 上線節點的 ip 就是管理 IP：provider_ip 不填（不是 provider IP）
    assert by["os-ctrl-1"]["management_ip"] == "192.0.2.11" and by["os-ctrl-1"]["provider_ip"] is None
    assert by["os-ctrl-1"]["storage_ip"] and by["os-ctrl-1"]["serial"] == "FAKE-SN-0001"
    assert out["vms"] == 1
    # 測試連線也跑一次（短逾時）
    res = await svc.diagnose(inst)
    assert res["ok"] is True, res
    assert _write_calls(LIVE) == before == 0                   # 從來沒有對 CubeCOS 送過寫入


@pytest.mark.skipif(not LIVE, reason="JTIPAM_CUBECOS_LIVE_URL not set")
async def test_live_cubecos_wrong_ca_is_unreachable_not_ignored(db_session, monkeypatch) -> None:
    assert LIVE
    from tests.test_openstack import _self_signed_pem
    inst = await _inst(db_session, LIVE, ca=_self_signed_pem())
    # OpenStack 部分用假雲（它不驗 TLS），CubeCOS 用錯的 CA → 憑證驗不過
    _use_live(monkeypatch, LIVE, _cloud())
    out = await svc.sync_instance(db_session, inst)
    assert out["vms"] == 1
    cap = inst.capabilities["services"]["cubecos"]
    assert cap["ok"] is False and cap["code"] == "cubecos_unreachable"
    assert "certificate" in cap["reason"].lower() or "ssl" in cap["reason"].lower()


@pytest.mark.parametrize("env", ["JTIPAM_CUBECOS_LIVE_REDIRECT_URL", "JTIPAM_CUBECOS_LIVE_HTML_URL"])
async def test_live_cubecos_login_page_variants(db_session, monkeypatch, env: str) -> None:
    url = os.environ.get(env)
    if not url:
        pytest.skip(f"{env} not set")
    inst = await _inst(db_session, url, ca=_ca_of(url))
    _use_live(monkeypatch, url, _cloud())
    out = await svc.sync_instance(db_session, inst)
    assert out["vms"] == 1                                       # OpenStack 照存
    cap = inst.capabilities["services"]["cubecos"]
    assert cap["ok"] is False and cap["code"] == "cubecos_auth_failed", cap
    assert inst.nodes is None
    assert _write_calls(url) == 0


async def test_live_cubecos_saml401_when_token_not_accepted(db_session, monkeypatch) -> None:
    """測試靶不接受這個 token（v3.1.0 原始碼行為：401＋JSON 信封，msg 是 SAML 網址）。"""
    url = os.environ.get("JTIPAM_CUBECOS_LIVE_SAML_URL")
    if not url:
        pytest.skip("JTIPAM_CUBECOS_LIVE_SAML_URL not set")
    inst = await _inst(db_session, url, ca=_ca_of(url))
    _use_live(monkeypatch, url, _cloud())
    await svc.sync_instance(db_session, inst)
    assert inst.capabilities["services"]["cubecos"]["code"] == "cubecos_auth_failed"


async def test_live_cubecos_path_prefix(db_session, monkeypatch) -> None:
    url = os.environ.get("JTIPAM_CUBECOS_LIVE_PREFIX_URL")
    if not url:
        pytest.skip("JTIPAM_CUBECOS_LIVE_PREFIX_URL not set")
    inst = await _inst(db_session, url, ca=_ca_of(url))
    _use_live(monkeypatch, url, _cloud())
    await svc.sync_instance(db_session, inst)
    cap = inst.capabilities["services"]["cubecos"]
    assert cap["ok"] is True, cap
    assert len(inst.nodes) == 3
    assert _write_calls(url) == 0


# ─────────────────────────── 真的 OpenStack ───────────────────────────

OS_LIVE = os.environ.get("JTIPAM_OPENSTACK_LIVE_URL")


@pytest.mark.skipif(not OS_LIVE, reason="JTIPAM_OPENSTACK_LIVE_URL not set")
async def test_live_openstack_full_sync(db_session, monkeypatch) -> None:
    assert OS_LIVE
    ca = None
    if os.environ.get("JTIPAM_OPENSTACK_LIVE_CA"):
        with open(os.environ["JTIPAM_OPENSTACK_LIVE_CA"], encoding="utf-8") as fh:
            ca = fh.read()
    inst = OpenStackInstance(
        id=uuid.uuid4(), name=f"live-{uuid.uuid4().hex[:6]}", auth_url=OS_LIVE,
        username=os.environ.get("JTIPAM_OPENSTACK_LIVE_USER", "admin"),
        user_domain=os.environ.get("JTIPAM_OPENSTACK_LIVE_USER_DOMAIN", "Default"),
        project_name=os.environ.get("JTIPAM_OPENSTACK_LIVE_PROJECT", "admin"),
        project_domain=os.environ.get("JTIPAM_OPENSTACK_LIVE_PROJECT_DOMAIN", "Default"),
        region=os.environ.get("JTIPAM_OPENSTACK_LIVE_REGION") or None,
        interface=os.environ.get("JTIPAM_OPENSTACK_LIVE_INTERFACE", "public"),
        verify_tls=not os.environ.get("JTIPAM_OPENSTACK_LIVE_INSECURE"), ca_cert=ca,
        secret_enc=b"x", secret_nonce=b"y")
    inst.secret_enc, inst.secret_nonce = svc.encrypt_secret_for(inst.id, os.environ["JTIPAM_OPENSTACK_LIVE_PASSWORD"])
    db_session.add(inst)
    await db_session.flush()
    res = await svc.diagnose(inst)
    assert res["ok"] is True, res
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is True, out
    caps = inst.capabilities
    assert caps["services"]["compute"]["ok"] and caps["services"]["network"]["ok"]
    assert caps["nova_microversion"]
    n_vm = (await db_session.execute(select(func.count()).select_from(VirtualMachine).where(
        VirtualMachine.cluster_id == inst.cluster_id))).scalar_one()
    n_port = (await db_session.execute(select(func.count()).select_from(OpenStackPort).where(
        OpenStackPort.instance_id == inst.id))).scalar_one()
    n_sub = (await db_session.execute(select(func.count()).select_from(OpenStackSubnet).where(
        OpenStackSubnet.instance_id == inst.id))).scalar_one()
    assert (n_vm, n_port, n_sub) == (out["vms"], out["ports"], out["subnets"])
    # 筆數可以跟平台核對：環境變數給了期望值就比
    for key, got in (("VMS", n_vm), ("PORTS", n_port), ("SUBNETS", n_sub)):
        want = os.environ.get(f"JTIPAM_OPENSTACK_LIVE_EXPECT_{key}")
        if want:
            assert got == int(want), (key, got, want)
    # 第二輪：沒有任何東西被誤刪
    out2 = await svc.sync_instance(db_session, inst)
    assert out2["removed_vms"] == 0 and out2["removed_ports"] == 0 and out2["removed_subnets"] == 0
