"""Bigstack CubeCOS／OpenStack 整合（唯讀，Beta；0229）。

假雲（tests/openstack_fake.py，httpx.MockTransport）回官方形狀的 JSON。守住：
- Keystone 兩種認證、Service Catalog 依 region＋interface 挑端點、版本字尾不重複
- 401 只重新認證一次、只重試一次；token 快到期先換
- Nova all_tenants 被拒 → 退回 token 的 project 並記能力、範圍縮小時不刪 VM
- 分頁（limit＋marker／*_links）、分頁中斷＝不完整 → 不刪、不累計
- port 連續兩次完整同步沒出現才刪；VM 改名／搬移是同一列（Nova UUID）
- 子網路對應：唯一才自動、重疊網段不合併、手動對應不被覆蓋；Floating IP 不當客體 IP
- 主機欄位看不到 → node 是 null，不猜
- CubeCOS 轉址到登入頁＝cubecos_auth_failed，OpenStack 資料照存；CubeCOS 只收 GET
- API：只限管理員、密碼不外流、每個會改資料的動作都有稽核；系統匯出／匯入帶得走
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from app.models.address import IPAddress
from app.models.openstack import OpenStackInstance, OpenStackPort, OpenStackSubnet
from app.models.virt import VirtCluster, VirtualMachine, VMInterface
from app.services import openstack as svc
from sqlalchemy import func, select

from tests import openstack_fake as F
from tests.test_ip_conflict_evidence import _subnet

pytestmark = pytest.mark.anyio

S1, S2, S3 = F.uid(0xA1), F.uid(0xA2), F.uid(0xA3)
P1, P2, P3, P4 = F.uid(0xB1), F.uid(0xB2), F.uid(0xB3), F.uid(0xB4)
NET_PRIV, NET_EXT, NET_ALT = F.uid(0x500), F.uid(0x501), F.uid(0x502)
SUB_PRIV, SUB_EXT, SUB_ALT, SUB_V6 = F.uid(0xC1), F.uid(0xC2), F.uid(0xC3), F.uid(0xC4)


def _cloud(**kw: Any) -> F.FakeCloud:
    c = F.FakeCloud(**kw)
    if not c.networks:
        c.networks = [F.network(NET_PRIV, "private"), F.network(NET_EXT, "public", external=True,
                                                                project=F.ADMIN_PROJECT),
                      F.network(NET_ALT, "alt-private", project=F.ALT_PROJECT)]
    return c


def _use(monkeypatch, cloud: F.FakeCloud) -> F.FakeCloud:
    monkeypatch.setattr(svc, "_http_client", cloud.client_factory())
    return cloud


async def _inst(db, **kw: Any) -> OpenStackInstance:
    secret = kw.pop("secret", "Secret-2026")
    inst = OpenStackInstance(
        id=uuid.uuid4(), name=kw.pop("name", f"os-{uuid.uuid4().hex[:6]}"),
        auth_url=kw.pop("auth_url", f"{F.KEYSTONE}/v3"), username=kw.pop("username", "ipam-reader"),
        project_name=kw.pop("project_name", "admin"), secret_enc=b"x", secret_nonce=b"y",
        region=kw.pop("region", "RegionOne"), **kw)
    inst.secret_enc, inst.secret_nonce = svc.encrypt_secret_for(inst.id, secret)
    db.add(inst)
    await db.flush()
    return inst


def _basic_cloud() -> F.FakeCloud:
    c = _cloud()
    c.servers = [F.server(S1, "web-1"), F.server(S2, "db-1", status="SHUTOFF", host="compute-2.example")]
    c.subnets = [F.subnet(SUB_PRIV, "198.51.100.0/24", gateway="198.51.100.1"),
                 F.subnet(SUB_EXT, "203.0.113.0/24", network=NET_EXT, project=F.ADMIN_PROJECT, name="public-subnet")]
    c.ports = [
        F.port(P1, device_id=S1, mac="fa:16:3e:00:00:01", fixed=[(SUB_PRIV, "198.51.100.21")]),
        F.port(P2, device_id=S2, mac="fa:16:3e:00:00:02", fixed=[(SUB_PRIV, "198.51.100.22")],
               binding_host="compute-2"),
        F.port(P3, device_id=F.uid(0x600), owner="network:router_gateway", network=NET_EXT,
               project=F.ADMIN_PROJECT, mac="fa:16:3e:00:00:03", fixed=[(SUB_EXT, "203.0.113.2")]),
    ]
    c.floatingips = [F.floating(F.uid(0xF1), "203.0.113.20", port_id=P1, fixed="198.51.100.21"),
                     F.floating(F.uid(0xF2), "203.0.113.30", port_id=None)]
    return c


# ─────────────────────────── 連線與認證 ───────────────────────────

def test_auth_url_normalisation() -> None:
    assert svc.tokens_url("https://k:5000/v3") == "https://k:5000/v3/auth/tokens"
    assert svc.tokens_url("https://k:5000/v3/") == "https://k:5000/v3/auth/tokens"
    assert svc.tokens_url("https://k:5000") == "https://k:5000/v3/auth/tokens"
    assert svc.tokens_url("https://k/identity") == "https://k/identity/v3/auth/tokens"


def test_service_url_joining() -> None:
    assert svc.neutron_base("https://n:9696") == "https://n:9696/v2.0"
    assert svc.neutron_base("https://n:9696/v2.0/") == "https://n:9696/v2.0"
    assert svc.neutron_base("https://h/networking") == "https://h/networking/v2.0"
    assert svc.compute_base("https://h:8774/v2.1") == "https://h:8774/v2.1"
    assert svc.compute_base("https://h:8774") == "https://h:8774/v2.1"
    # 舊式目錄網址帶 project id：版本文件要問版本那一層
    assert svc.compute_version_url(f"https://h:8774/v2.1/{F.DEMO_PROJECT}") == "https://h:8774/v2.1/"


@pytest.mark.parametrize(("server_max", "want"), [("2.96", "2.79"), ("2.60", "2.60"), ("2.25", "2.25"),
                                                  (None, None)])
def test_microversion_choice(server_max: str | None, want: str | None) -> None:
    assert svc.choose_microversion(server_max) == want


async def test_password_auth_and_catalog(monkeypatch) -> None:
    cloud = _use(monkeypatch, _basic_cloud())
    cfg = svc.ConnConfig(auth_url=f"{F.KEYSTONE}/v3", auth_method="password", username="ipam-reader",
                         user_domain="Default", project_name="admin", project_domain="Default",
                         secret="Secret-2026", region=None, interface="public")
    async with svc.OpenStackClient(cfg) as cli:
        await cli.authenticate()
        assert cli.endpoint("compute") == F.NOVA
        assert cli.endpoint("network") == F.NEUTRON
        assert cli.region == "RegionOne"                    # 沒指定＝目錄裡第一個
    body = cloud.auth_bodies[0]["auth"]
    assert body["identity"]["methods"] == ["password"]
    assert body["identity"]["password"]["user"] == {"name": "ipam-reader", "domain": {"name": "Default"},
                                                    "password": "Secret-2026"}
    assert body["scope"] == {"project": {"name": "admin", "domain": {"name": "Default"}}}
    # 指定 internal＋RegionTwo：只有 RegionTwo 的 public → 找不到
    cfg.interface, cfg.region = "internal", "RegionTwo"
    async with svc.OpenStackClient(cfg) as cli:
        with pytest.raises(svc.OpenStackError) as e:
            await cli.authenticate()
    assert e.value.code == "openstack_no_catalog_service"
    assert e.value.params["service"] == "compute"


async def test_app_credential_auth(monkeypatch) -> None:
    cloud = _use(monkeypatch, _basic_cloud())
    cfg = svc.ConnConfig(auth_url=F.KEYSTONE, auth_method="app_credential", app_credential_id="appcred-1",
                         secret="appcred-secret", region="RegionOne", interface="public")
    async with svc.OpenStackClient(cfg) as cli:
        await cli.authenticate()
    body = cloud.auth_bodies[0]["auth"]
    assert body["identity"] == {"methods": ["application_credential"],
                                "application_credential": {"id": "appcred-1", "secret": "appcred-secret"}}
    assert "scope" not in body                                # Application Credential 本身就有範圍


async def test_wrong_password_is_auth_failed(monkeypatch) -> None:
    _use(monkeypatch, _basic_cloud())
    cfg = svc.ConnConfig(auth_url=F.KEYSTONE, username="u", project_name="admin", secret="wrong")
    async with svc.OpenStackClient(cfg) as cli:
        with pytest.raises(svc.OpenStackError) as e:
            await cli.authenticate()
    assert e.value.code == "openstack_auth_failed"
    assert "wrong" not in str(e.value) and "wrong" not in repr(e.value.params)


async def test_401_reauth_once_then_retry_once(monkeypatch) -> None:
    cloud = _use(monkeypatch, _basic_cloud())
    cfg = svc.ConnConfig(auth_url=F.KEYSTONE, username="u", project_name="admin", secret="Secret-2026",
                         region="RegionOne")
    async with svc.OpenStackClient(cfg) as cli:
        await cli.authenticate()
        cloud.unauthorized_next = 1                           # token 被撤銷：重新認證後就好了
        data = await cli.get_json("network", f"{F.NEUTRON}/v2.0/extensions")
        assert data["extensions"]
        assert cloud.tokens_issued == 2
        cloud.unauthorized_next = 5                           # 一直 401：只重試一次就放棄
        with pytest.raises(svc.OpenStackError) as e:
            await cli.get_json("network", f"{F.NEUTRON}/v2.0/extensions")
        assert e.value.code == "openstack_auth_failed"
        assert cloud.tokens_issued == 3


async def test_token_near_expiry_is_renewed_first(monkeypatch) -> None:
    from datetime import UTC, datetime
    cloud = _use(monkeypatch, _basic_cloud())
    cfg = svc.ConnConfig(auth_url=F.KEYSTONE, username="u", project_name="admin", secret="Secret-2026",
                         region="RegionOne")
    async with svc.OpenStackClient(cfg) as cli:
        await cli.authenticate()
        await cli.get_json("network", f"{F.NEUTRON}/v2.0/extensions")
        assert cloud.tokens_issued == 1                          # 還有一小時：不換
        cli.expires_at = datetime.now(UTC) + timedelta(minutes=2)   # 時間過去了，剩不到 5 分鐘
        before = len(cloud.calls)
        await cli.get_json("network", f"{F.NEUTRON}/v2.0/extensions")
        assert cloud.tokens_issued == 2
        # 先換 token 再送請求：中間沒有吃到 401 的那一次
        assert [c[0] for c in cloud.calls[before:]] == ["POST", "GET"]


async def test_short_lived_tokens_do_not_reauth_every_request(monkeypatch) -> None:
    """token 本身只有 4 分鐘壽命的雲：不可以每個請求都重新認證（提早換的門檻最多是壽命的一半）。"""
    cloud = _use(monkeypatch, _basic_cloud())
    cloud.token_ttl = timedelta(minutes=4)
    cfg = svc.ConnConfig(auth_url=F.KEYSTONE, username="u", project_name="admin", secret="Secret-2026",
                         region="RegionOne")
    async with svc.OpenStackClient(cfg) as cli:
        await cli.authenticate()
        for _ in range(3):
            await cli.get_json("network", f"{F.NEUTRON}/v2.0/extensions")
    assert cloud.tokens_issued == 1


async def test_transport_errors_are_classified(monkeypatch) -> None:
    import httpx

    def _boom(exc: Exception):
        def _make(verify: Any, timeout: float) -> httpx.AsyncClient:
            def h(req: httpx.Request) -> httpx.Response:
                raise exc
            return httpx.AsyncClient(transport=httpx.MockTransport(h), follow_redirects=False)
        return _make

    cfg = svc.ConnConfig(auth_url=F.KEYSTONE, username="u", project_name="p", secret="s")
    for exc, code in ((httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
                       "openstack_tls_failed"),
                      (httpx.ConnectError("Connection refused"), "openstack_unreachable"),
                      (httpx.ReadTimeout("timed out"), "openstack_unreachable")):
        monkeypatch.setattr(svc, "_http_client", _boom(exc))
        async with svc.OpenStackClient(cfg) as cli:
            with pytest.raises(svc.OpenStackError) as e:
                await cli.authenticate()
        assert e.value.code == code, exc
        assert e.value.params.get("reason")


async def test_blocked_address_is_unreachable() -> None:
    cfg = svc.ConnConfig(auth_url="https://169.254.169.254/v3", username="u", project_name="p", secret="s")
    async with svc.OpenStackClient(cfg) as cli:
        with pytest.raises(svc.OpenStackError) as e:
            await cli.authenticate()
    assert e.value.code == "openstack_unreachable"


# ─────────────────────────── 同步 ───────────────────────────

async def test_sync_writes_vms_ports_subnets(db_session, monkeypatch) -> None:
    sub = await _subnet(db_session, "198.51.100.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.21", state="active")
    db_session.add(ip)
    inst = await _inst(db_session)
    cloud = _use(monkeypatch, _basic_cloud())
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is True, out
    assert out["vms"] == 2
    cl = await db_session.get(VirtCluster, inst.cluster_id)
    assert (cl.type, cl.name) == ("openstack", inst.name)
    vms = {v.name: v for v in (await db_session.execute(
        select(VirtualMachine).where(VirtualMachine.cluster_id == cl.id))).scalars()}
    assert vms["web-1"].external_id == S1
    assert (vms["web-1"].status, vms["web-1"].node, vms["web-1"].vcpus, vms["web-1"].memory_mb,
            vms["web-1"].disk_gb) == ("running", "compute-1.example", 2, 4096, 40)
    assert vms["web-1"].description == "project: demo"
    assert vms["db-1"].status == "stopped"
    assert vms["web-1"].primary_ip_id == ip.id
    nics = (await db_session.execute(select(VMInterface).where(VMInterface.vm_id == vms["web-1"].id))).scalars().all()
    assert [(n.name, str(n.mac), str(n.primary_ip), n.bridge) for n in nics] == [
        (P1, "fa:16:3e:00:00:01", "198.51.100.21", "private")]
    await db_session.refresh(ip)
    assert str(ip.mac) == "fa:16:3e:00:00:01"
    ports = {p.port_id: p for p in (await db_session.execute(
        select(OpenStackPort).where(OpenStackPort.instance_id == inst.id))).scalars()}
    assert set(ports) == {P1, P2, P3}
    assert ports[P1].vm_id == vms["web-1"].id
    assert ports[P3].vm_id is None                               # 路由器的 port 不是 VM
    assert ports[P1].binding_host == "compute-1"
    assert ports[P1].project_name == "demo" and ports[P1].network_name == "private"
    assert ports[P1].floating_ips == [{"floating_ip_address": "203.0.113.20", "floating_ip_id": F.uid(0xF1),
                                       "router_id": F.uid(0x600)}]
    subs = {s.subnet_id: s for s in (await db_session.execute(
        select(OpenStackSubnet).where(OpenStackSubnet.instance_id == inst.id))).scalars()}
    assert subs[SUB_PRIV].mapped_subnet_id == sub.id             # 唯一一個同 CIDR → 自動對應
    assert subs[SUB_EXT].mapped_subnet_id is None                # 沒有同 CIDR → 待對應
    assert str(subs[SUB_PRIV].gateway_ip) == "198.51.100.1"
    caps = inst.capabilities
    assert caps["all_projects"] is True and caps["host_visible"] is True and caps["port_binding_visible"] is True
    assert caps["nova_microversion"] == "2.79"
    assert caps["services"]["compute"]["ok"] and caps["services"]["network"]["ok"]
    assert "binding" in caps["neutron_extensions"]
    assert caps["projects_visible"] == 3
    # 未綁定的 Floating IP 也看得到（不屬於任何 port）
    assert [f["floating_ip_address"] for f in caps["unbound_floating_ips"]] == ["203.0.113.30"]
    assert inst.last_full_success_at is not None and inst.last_error is None
    # Nova 用協商出的 microversion
    nova = [c for c in cloud.calls if c[1].startswith(f"{F.NOVA}/servers")]
    assert nova and all(c[2].get("openstack-api-version") == "compute 2.79" for c in nova)
    # 只讀：除了 Keystone 拿 token，沒有任何非 GET
    assert {(m, u.split("?")[0]) for m, u, _ in cloud.calls if m != "GET"} == {("POST", f"{F.KEYSTONE}/v3/auth/tokens")}


async def test_hostname_reported_from_vm_name(db_session, monkeypatch) -> None:
    from app.models.ip_hostname import IPHostnameReport
    sub = await _subnet(db_session, "198.51.100.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.21", state="active")
    db_session.add(ip)
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    rep = (await db_session.execute(select(IPHostnameReport).where(
        IPHostnameReport.ip_id == ip.id, IPHostnameReport.source == "openstack"))).scalars().one()
    assert rep.hostname == "web-1"


async def test_auto_create_only_inside_the_mapped_subnet(db_session, monkeypatch) -> None:
    await _subnet(db_session, "198.51.100.0/24")
    inst = await _inst(db_session, auto_create_ips=True)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    rows = {str(r.ip): r for r in (await db_session.execute(select(IPAddress))).scalars()}
    assert set(rows) == {"198.51.100.21", "198.51.100.22"}       # 外部網段沒對應 → 路由器位址不建
    assert rows["198.51.100.21"].discovery_source == "openstack"


async def test_old_microversion_fetches_flavors(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.nova_max = "2.25"
    cloud.servers = [F.server(S1, "web-1", flavor={"id": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20})]
    cloud.flavors = {"m1.small": {"id": "m1.small", "name": "m1.small", "vcpus": 1, "ram": 2048, "disk": 20}}
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    vm = (await db_session.execute(select(VirtualMachine).where(
        VirtualMachine.cluster_id == inst.cluster_id))).scalars().one()
    assert (vm.vcpus, vm.memory_mb, vm.disk_gb) == (1, 2048, 20)
    assert inst.capabilities["nova_microversion"] == "2.25"
    assert sum(1 for c in cloud.calls if "/flavors/m1.small" in c[1]) == 1     # 同一個 flavor 只問一次


async def test_pagination_reads_everything(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.page_size = 2
    cloud.servers = [F.server(F.uid(0xD00 + i), f"vm-{i}") for i in range(5)]
    cloud.ports = [F.port(F.uid(0xE00 + i), device_id=F.uid(0xD00 + i), mac=f"fa:16:3e:00:01:{i:02x}",
                          fixed=[(SUB_PRIV, f"198.51.100.{100 + i}")]) for i in range(5)]
    _use(monkeypatch, cloud)
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is True
    assert out["vms"] == 5 and out["ports"] == 5
    markers = [c[1] for c in cloud.calls if "/servers/detail" in c[1] and "marker=" in c[1]]
    assert len(markers) >= 2


async def test_incomplete_pass_never_deletes_or_counts(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    # 第二輪：P2 不見了，但 ports 的第二頁壞掉 → port 清單不完整：不刪、不累計、網卡不重建
    cloud.page_size = 1
    cloud.fail_page = {"ports": 2}
    cloud.ports = [p for p in cloud.ports if p["id"] != P2]
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is False
    assert out["removed_ports"] == 0
    assert inst.last_error and "ports" in inst.last_error
    assert inst.last_full_success_at is not None                  # 上一次完整的時間不動
    rows = {p.port_id: p.missing_syncs for p in (await db_session.execute(
        select(OpenStackPort).where(OpenStackPort.instance_id == inst.id))).scalars()}
    assert rows == {P1: 0, P2: 0, P3: 0}
    n = (await db_session.execute(select(func.count()).select_from(VMInterface))).scalar_one()
    assert n == 2
    # 第三輪：S2 不見了，但 servers 的第二頁壞掉 → VM 不刪
    cloud.fail_page = {"servers": 2}
    cloud.servers = [s for s in cloud.servers if s["id"] != S2]
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is False and out["removed_vms"] == 0
    names = set((await db_session.execute(select(VirtualMachine.name).where(
        VirtualMachine.cluster_id == inst.cluster_id))).scalars())
    assert names == {"web-1", "db-1"}
    # Nova 與 Neutron 都讀不到＝硬失敗（不是「成功、0 筆」）
    cloud.fail_page = {"servers": 1, "ports": 1}
    with pytest.raises(svc.OpenStackError):
        await svc.sync_instance(db_session, inst)


async def test_403_midway_through_all_projects_is_incomplete(db_session, monkeypatch) -> None:
    """已經決定跨 project（第一頁探測通過），翻到第二頁才 403：不是「退回」，是這一輪 VM 清單不完整。"""
    inst = await _inst(db_session)
    cloud = _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    cloud.page_size = 1
    cloud.forbid_page = {"servers": 2}
    cloud.servers = [s for s in cloud.servers if s["id"] != S2]
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["complete"] is False and out["removed_vms"] == 0
    assert "openstack_forbidden" in (inst.last_error or "")
    assert inst.capabilities["services"]["compute"]["code"] == "openstack_forbidden"


async def test_port_needs_two_complete_misses(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    cloud.ports = [p for p in cloud.ports if p["id"] != P3]
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["removed_ports"] == 0
    row = (await db_session.execute(select(OpenStackPort).where(OpenStackPort.port_id == P3))).scalars().one()
    assert row.missing_syncs == 1
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["removed_ports"] == 1
    assert (await db_session.execute(select(OpenStackPort).where(OpenStackPort.port_id == P3))).first() is None
    # 回來了就歸零
    cloud.ports.append(F.port(P4, device_id="", owner="network:dhcp", fixed=[(SUB_PRIV, "198.51.100.2")]))
    await svc.sync_instance(db_session, inst)
    cloud.ports = [p for p in cloud.ports if p["id"] != P4]
    await svc.sync_instance(db_session, inst)
    cloud.ports.append(F.port(P4, device_id="", owner="network:dhcp", fixed=[(SUB_PRIV, "198.51.100.2")]))
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    row = (await db_session.execute(select(OpenStackPort).where(OpenStackPort.port_id == P4))).scalars().one()
    assert row.missing_syncs == 0


async def test_rename_and_move_keep_one_vm(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    first = (await db_session.execute(select(VirtualMachine).where(VirtualMachine.external_id == S1))).scalars().one()
    cloud.servers[0] = F.server(S1, "web-1-renamed", host="compute-9.example")
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    rows = (await db_session.execute(select(VirtualMachine).where(VirtualMachine.external_id == S1))).scalars().all()
    assert len(rows) == 1 and rows[0].id == first.id
    assert (rows[0].name, rows[0].node) == ("web-1-renamed", "compute-9.example")


async def test_hidden_host_is_null_not_guessed(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.servers = [F.server(S1, "web-1", hide_host=True)]
    cloud.ports = [F.port(P1, device_id=S1, fixed=[(SUB_PRIV, "198.51.100.21")], hide_binding=True)]
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    vm = (await db_session.execute(select(VirtualMachine).where(VirtualMachine.external_id == S1))).scalars().one()
    assert vm.node is None
    port = (await db_session.execute(select(OpenStackPort).where(OpenStackPort.port_id == P1))).scalars().one()
    assert port.binding_host is None
    assert inst.capabilities["host_visible"] is False
    assert inst.capabilities["port_binding_visible"] is False


async def test_all_tenants_forbidden_falls_back_and_keeps_vms(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.token_scope_project = F.DEMO_PROJECT
    _use(monkeypatch, cloud)
    cloud.servers.append(F.server(S3, "alt-vm", project=F.ALT_PROJECT))
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert inst.capabilities["all_projects"] is True
    cloud.forbid_all_tenants = True                               # 角色被收回
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert inst.capabilities["all_projects"] is False
    assert out["removed_vms"] == 0                                # 範圍縮小：別的 project 的 VM 不可以當成刪除
    names = set((await db_session.execute(select(VirtualMachine.name).where(
        VirtualMachine.cluster_id == inst.cluster_id))).scalars())
    assert "alt-vm" in names
    # 自己 project 的清單是完整的（10-22 起 complete 看的是「每一頁都抓完」）；別的 project 照樣不刪
    assert out["complete"] is True
    # 使用者自己關掉「跨 project」：那就是只看這個 project，可以清
    inst.all_projects = False
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["removed_vms"] == 1
    assert not any("all_tenants" in c[1] for c in cloud.calls[-10:])


async def test_overlapping_cidrs_across_projects_stay_pending(db_session, monkeypatch) -> None:
    sub = await _subnet(db_session, "10.0.0.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="10.0.0.5", state="active")
    db_session.add(ip)
    inst = await _inst(db_session)
    cloud = _cloud()
    cloud.servers = [F.server(S1, "demo-vm"), F.server(S2, "alt-vm", project=F.ALT_PROJECT)]
    cloud.subnets = [F.subnet(SUB_PRIV, "10.0.0.0/24"),
                     F.subnet(SUB_ALT, "10.0.0.0/24", network=NET_ALT, project=F.ALT_PROJECT)]
    cloud.ports = [F.port(P1, device_id=S1, mac="fa:16:3e:00:00:01", fixed=[(SUB_PRIV, "10.0.0.5")]),
                   F.port(P2, device_id=S2, mac="fa:16:3e:00:00:02", network=NET_ALT, project=F.ALT_PROJECT,
                          fixed=[(SUB_ALT, "10.0.0.5")])]
    _use(monkeypatch, cloud)
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    subs = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.instance_id == inst.id))).scalars().all()
    assert [s.mapped_subnet_id for s in subs] == [None, None]     # 兩個 project 都是 10.0.0.0/24：不合併
    assert out["pending_subnets"] == 2
    await db_session.refresh(ip)
    assert ip.mac is None                                          # 沒對應 → 不連 IPAM
    vms = (await db_session.execute(select(VirtualMachine).where(VirtualMachine.cluster_id == inst.cluster_id))).scalars().all()
    assert all(v.primary_ip_id is None for v in vms)


async def test_ambiguous_mapping_stays_pending_and_scope_disambiguates(db_session, monkeypatch) -> None:
    a = await _subnet(db_session, "198.51.100.0/24")
    b = await _subnet(db_session, "198.51.100.0/24")
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    row = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.subnet_id == SUB_PRIV))).scalars().one()
    assert row.mapped_subnet_id is None
    # 限定範圍之後只剩一個 → 可以自動對應
    inst.scope_subnet_ids = [str(b.id)]
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    await db_session.refresh(row)
    assert row.mapped_subnet_id == b.id
    assert a.id != b.id


async def test_manual_mapping_is_never_overwritten(db_session, monkeypatch) -> None:
    auto = await _subnet(db_session, "198.51.100.0/24")
    other = await _subnet(db_session, "198.51.0.0/16")
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    row = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.subnet_id == SUB_PRIV))).scalars().one()
    assert row.mapped_subnet_id == auto.id
    row.mapped_subnet_id, row.mapped_manually = other.id, True
    ext = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.subnet_id == SUB_EXT))).scalars().one()
    ext.mapped_manually = True                                     # 手動設成「不對應」也算
    await db_session.flush()
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    await db_session.refresh(row)
    assert (row.mapped_subnet_id, row.mapped_manually) == (other.id, True)
    await db_session.refresh(ext)
    assert ext.mapped_subnet_id is None


async def test_floating_ip_is_stored_but_never_a_guest_ip(db_session, monkeypatch) -> None:
    ext_sub = await _subnet(db_session, "203.0.113.0/24")
    fip = IPAddress(subnet_id=ext_sub.id, ip="203.0.113.20", state="active")
    gw = IPAddress(subnet_id=ext_sub.id, ip="203.0.113.2", state="active")
    db_session.add_all([fip, gw])
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    row = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.subnet_id == SUB_EXT))).scalars().one()
    assert row.mapped_subnet_id == ext_sub.id
    await db_session.refresh(fip)
    await db_session.refresh(gw)
    assert fip.mac is None and gw.mac is None                     # Floating IP、路由器位址都不是客體 IP
    vm = (await db_session.execute(select(VirtualMachine).where(VirtualMachine.external_id == S1))).scalars().one()
    assert vm.primary_ip_id is None                               # 私網段沒對應 → 沒有主要 IP（也不拿 Floating IP 充數）
    port = (await db_session.execute(select(OpenStackPort).where(OpenStackPort.port_id == P1))).scalars().one()
    assert port.floating_ips[0]["floating_ip_address"] == "203.0.113.20"


async def test_cubecos_nodes(db_session, monkeypatch) -> None:
    inst = await _inst(db_session, product="cubecos")
    cloud = _basic_cloud()
    cloud.cubecos_nodes = [F.cube_node("cube-1"), F.cube_node("cube-2", status="down", mgmt="192.0.2.12",
                                                              ip="10.1.0.12", storage="10.2.0.12", serial="SN-0002")]
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    # provider_ip 只在跟管理 IP 不同時才填（上線節點的 ip 就是管理 IP，不是 provider IP）
    assert inst.nodes == [
        {"hostname": "cube-1", "role": "control-converged", "status": "up", "management_ip": "192.0.2.11",
         "provider_ip": None, "storage_ip": "10.2.0.11", "serial": "SN-0001"},
        {"hostname": "cube-2", "role": "control-converged", "status": "down", "management_ip": "192.0.2.12",
         "provider_ip": "10.1.0.12", "storage_ip": "10.2.0.12", "serial": "SN-0002"},
    ]
    assert inst.capabilities["services"]["cubecos"]["ok"] is True
    assert inst.capabilities["services"]["cubecos"]["datacenter"] == "dc-tpe"
    cube = [c for c in cloud.calls if c[1].startswith(F.CUBECOS + "/")]
    assert cube and {m for m, _u, _h in cube} == {"GET"}          # CubeCOS 只收 GET
    # 節點清單帶的是 Keystone token
    nodes_call = next(c for c in cube if c[1].endswith("/nodes"))
    assert nodes_call[2].get("x-auth-token", "").startswith("gAAAAAB-token-")


@pytest.mark.parametrize("mode", ["redirect", "html", "unauthorized"])
async def test_cubecos_login_page_is_auth_failed_and_openstack_still_saved(db_session, monkeypatch, mode) -> None:
    inst = await _inst(db_session, product="cubecos")
    cloud = _basic_cloud()
    cloud.cubecos_nodes = [F.cube_node("cube-1")]
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    assert len(inst.nodes) == 1
    cloud.cubecos_mode = mode
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["vms"] == 2                                         # OpenStack 照常
    assert len(inst.nodes) == 1                                    # 上一次的節點留著
    cap = inst.capabilities["services"]["cubecos"]
    assert cap["ok"] is False and cap["code"] == "cubecos_auth_failed"
    assert cap["params"] == {"service": "cubecos", "reason": cap["reason"]} and cap["message"]
    # 不跟著轉址走
    assert not any("/saml/" in c[1] for c in cloud.calls)
    assert inst.last_error and "cubecos" in inst.last_error.lower()


async def test_cubecos_url_with_path_prefix(db_session, monkeypatch) -> None:
    inst = await _inst(db_session, product="cubecos", cubecos_url=f"{F.CUBECOS}/cos-api/")
    cloud = _basic_cloud()
    cloud.cubecos_prefix = "/cos-api"
    cloud.cubecos_nodes = [F.cube_node("cube-1")]
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    assert inst.capabilities["services"]["cubecos"]["ok"] is True, inst.capabilities["services"]["cubecos"]
    assert [n["hostname"] for n in inst.nodes] == ["cube-1"]
    assert any(c[1] == f"{F.CUBECOS}/cos-api/api/v1/datacenters/dc-tpe/nodes" for c in cloud.calls)


async def test_cubecos_unreachable(db_session, monkeypatch) -> None:
    inst = await _inst(db_session, product="cubecos")
    cloud = _basic_cloud()
    cloud.cubecos_mode = "down"
    _use(monkeypatch, cloud)
    out = await svc.sync_instance(db_session, inst)
    assert out["vms"] == 2
    assert inst.capabilities["services"]["cubecos"]["code"] == "cubecos_unreachable"
    assert inst.nodes is None


async def test_plain_openstack_never_calls_cubecos(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    assert not any(c[1].startswith(F.CUBECOS + "/api") for c in cloud.calls)


async def test_auth_failure_is_a_hard_failure(db_session, monkeypatch) -> None:
    inst = await _inst(db_session, secret="wrong")
    _use(monkeypatch, _basic_cloud())
    with pytest.raises(svc.OpenStackError) as e:
        await svc.sync_instance(db_session, inst)
    assert e.value.code == "openstack_auth_failed"
    assert inst.last_error


async def test_neutron_catalog_with_version_suffix(db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.neutron_catalog_suffix = "/v2.0"
    _use(monkeypatch, cloud)
    out = await svc.sync_instance(db_session, inst)
    assert out["complete"] is True
    assert not any("/v2.0/v2.0/" in c[1] for c in cloud.calls)


async def test_ipv6_and_multiple_fixed_ips(db_session, monkeypatch) -> None:
    sub4 = await _subnet(db_session, "198.51.100.0/24")
    sub6 = await _subnet(db_session, "2001:db8:1::/64")
    ip4 = IPAddress(subnet_id=sub4.id, ip="198.51.100.21", state="active")
    ip6 = IPAddress(subnet_id=sub6.id, ip="2001:db8:1::21", state="active")
    db_session.add_all([ip4, ip6])
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.subnets.append(F.subnet(SUB_V6, "2001:db8:1::/64", name="private-v6"))
    cloud.ports[0] = F.port(P1, device_id=S1, mac="fa:16:3e:00:00:01",
                            fixed=[(SUB_V6, "2001:db8:1::21"), (SUB_PRIV, "198.51.100.21")])
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    await db_session.flush()
    nic = (await db_session.execute(select(VMInterface).where(VMInterface.name == P1))).scalars().one()
    assert str(nic.primary_ip) == "198.51.100.21"                  # IPv4 優先
    await db_session.refresh(ip6)
    assert str(ip6.mac) == "fa:16:3e:00:00:01"                     # IPv6 也連得上
    vm = (await db_session.execute(select(VirtualMachine).where(VirtualMachine.external_id == S1))).scalars().one()
    assert vm.primary_ip_id == ip4.id


# ─────────────────────────── API ───────────────────────────

def _payload(**kw: Any) -> dict[str, Any]:
    return {"name": f"os-{uuid.uuid4().hex[:6]}", "product": "openstack", "auth_url": f"{F.KEYSTONE}/v3",
            "auth_method": "password", "username": "ipam-reader", "user_domain": "Default",
            "project_name": "admin", "project_domain": "Default", "secret": "Secret-2026",
            "region": "RegionOne", "interface": "public", "verify_tls": True, "all_projects": True,
            "enabled": True, "sync_interval_seconds": 300, "scope_subnet_ids": None, "auto_create_ips": False,
            "description": None, **kw}


async def test_api_crud_secrets_and_audit(client, auth_headers, db_session, monkeypatch) -> None:
    from app.models.audit import AuditLog
    r = await client.post("/api/v1/openstack-instances", json=_payload(), headers=auth_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["has_secret"] is True and body["has_ca_cert"] is False
    assert not any(k in body for k in ("secret", "secret_enc", "secret_nonce", "ca_cert"))
    assert "Secret-2026" not in r.text
    iid = body["id"]
    inst = await db_session.get(OpenStackInstance, uuid.UUID(iid))
    assert svc.decrypt_secret_of(inst) == "Secret-2026"
    # 同名
    r = await client.post("/api/v1/openstack-instances", json=_payload(name=body["name"]), headers=auth_headers)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "openstack_name_taken"
    # 密碼方式少了帳號
    assert (await client.post("/api/v1/openstack-instances", json=_payload(username=None),
                              headers=auth_headers)).status_code == 422
    # 不合法的網址
    assert (await client.post("/api/v1/openstack-instances", json=_payload(auth_url="ftp://x"),
                              headers=auth_headers)).status_code == 422
    # PATCH：不給 secret＝保留；ca_cert 寫入後只回 has_ca_cert；"" 清掉
    pem = _self_signed_pem()
    r = await client.patch(f"/api/v1/openstack-instances/{iid}", json={"region": None, "ca_cert": pem},
                           headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["has_ca_cert"] is True and r.json()["region"] is None
    await db_session.refresh(inst)
    assert svc.decrypt_secret_of(inst) == "Secret-2026"
    r = await client.patch(f"/api/v1/openstack-instances/{iid}", json={"ca_cert": "", "secret": "New-2026"},
                           headers=auth_headers)
    assert r.json()["has_ca_cert"] is False
    await db_session.refresh(inst)
    assert svc.decrypt_secret_of(inst) == "New-2026"
    assert (await client.patch(f"/api/v1/openstack-instances/{iid}", json={"ca_cert": "not a pem"},
                               headers=auth_headers)).status_code == 422
    # 測試連線（同步、短逾時）
    cloud = _use(monkeypatch, _basic_cloud())
    cloud.password = "New-2026"
    r = await client.post(f"/api/v1/openstack-instances/{iid}/test", headers=auth_headers)
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["ok"] is True and t["error"] is None
    assert t["capabilities"]["services"]["identity"]["ok"] is True
    assert t["capabilities"]["all_projects"] is True
    cloud.password = "other"
    t = (await client.post(f"/api/v1/openstack-instances/{iid}/test", headers=auth_headers)).json()
    assert t["ok"] is False and t["error"]["code"] == "openstack_auth_failed"
    ident = t["capabilities"]["services"]["identity"]
    assert ident["ok"] is False and ident["code"] == "openstack_auth_failed"
    assert set(ident["params"]) == {"service", "reason"}
    assert "New-2026" not in str(t)
    # 列表
    lst = (await client.get("/api/v1/openstack-instances", headers=auth_headers)).json()
    row = next(x for x in lst if x["id"] == iid)
    assert {"vm_count", "port_count", "node_count", "capabilities", "last_full_success_at"} <= set(row)
    assert "New-2026" not in str(lst)
    acts = set((await db_session.execute(select(AuditLog.action).where(AuditLog.object_id == iid))).scalars())
    assert {"create", "update", "test"} <= acts
    # 稽核內容也不含密碼
    diffs = (await db_session.execute(select(AuditLog.diff).where(AuditLog.object_id == iid))).scalars().all()
    assert "Secret-2026" not in str(diffs) and "New-2026" not in str(diffs)
    assert (await client.delete(f"/api/v1/openstack-instances/{iid}", headers=auth_headers)).status_code == 204
    r = await client.delete(f"/api/v1/openstack-instances/{iid}", headers=auth_headers)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "openstack_not_found"


async def test_api_subnet_mapping_and_nodes(client, auth_headers, db_session, monkeypatch) -> None:
    from app.models.audit import AuditLog
    sub = await _subnet(db_session, "198.51.100.0/24")
    other = await _subnet(db_session, "198.51.0.0/16")
    inst = await _inst(db_session, product="cubecos")
    cloud = _basic_cloud()
    cloud.cubecos_nodes = [F.cube_node("cube-1")]
    _use(monkeypatch, cloud)
    await svc.sync_instance(db_session, inst)
    await db_session.commit()
    rows = (await client.get(f"/api/v1/openstack-instances/{inst.id}/subnets", headers=auth_headers)).json()
    by = {r["subnet_id"]: r for r in rows}
    assert by[SUB_PRIV]["mapped_subnet_id"] == str(sub.id) and by[SUB_PRIV]["mapped_subnet_cidr"] == "198.51.100.0/24"
    assert by[SUB_EXT]["mapped_subnet_id"] is None and by[SUB_EXT]["pending"] is True
    r = await client.patch(f"/api/v1/openstack-instances/{inst.id}/subnets/{by[SUB_PRIV]['id']}",
                           json={"mapped_subnet_id": str(other.id)}, headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["mapped_manually"] is True and r.json()["mapped_subnet_id"] == str(other.id)
    # 不存在的子網路
    r = await client.patch(f"/api/v1/openstack-instances/{inst.id}/subnets/{by[SUB_PRIV]['id']}",
                           json={"mapped_subnet_id": str(uuid.uuid4())}, headers=auth_headers)
    assert r.status_code == 404
    acts = (await db_session.execute(select(AuditLog.action).where(
        AuditLog.object_type == "openstack_subnet"))).scalars().all()
    assert "update" in acts
    nodes = (await client.get(f"/api/v1/openstack-instances/{inst.id}/nodes", headers=auth_headers)).json()
    assert nodes[0]["hostname"] == "cube-1"


async def test_api_ports_by_ip(client, auth_headers, db_session, monkeypatch) -> None:
    sub = await _subnet(db_session, "198.51.100.0/24")
    ip = IPAddress(subnet_id=sub.id, ip="198.51.100.21", state="active")
    db_session.add(ip)
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.commit()
    r = await client.get(f"/api/v1/openstack-instances/ports/by-ip/{ip.id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 1
    p = rows[0]
    assert (p["port_id"], p["mac"], p["network_name"], p["project_name"], p["vm_name"], p["binding_host"]) == (
        P1, "fa:16:3e:00:00:01", "private", "demo", "web-1", "compute-1")
    assert p["vm_node"] == "compute-1.example"
    assert p["floating_ips"][0]["floating_ip_address"] == "203.0.113.20"
    assert p["instance_name"] == inst.name


async def test_virt_vm_list_carries_project(client, auth_headers, db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.commit()
    r = await client.get(f"/api/v1/virt/vms?cluster_id={inst.cluster_id}", headers=auth_headers)
    assert r.status_code == 200, r.text
    by = {v["name"]: v for v in r.json()["items"]}
    assert by["web-1"]["project"] == "demo"
    assert by["web-1"]["node"] == "compute-1.example"


async def test_api_is_admin_only(client, db_session) -> None:
    from app.core.security import hash_password
    from app.models.user import User
    from app.services.auth import issue_access_token
    u = User(username=f"na-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local",
             password_hash=hash_password("TestPassword2026!"), is_admin=False, is_active=True)
    db_session.add(u)
    await db_session.commit()
    h = {"Authorization": f"Bearer {issue_access_token(u)}"}
    assert (await client.get("/api/v1/openstack-instances", headers=h)).status_code == 403
    assert (await client.post("/api/v1/openstack-instances", json=_payload(), headers=h)).status_code == 403
    assert (await client.get(f"/api/v1/openstack-instances/ports/by-ip/{uuid.uuid4()}",
                             headers=h)).status_code in (403, 404)


async def test_sync_endpoint_audits_and_merges(client, auth_headers, db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    await db_session.commit()
    _use(monkeypatch, _basic_cloud())
    r = await client.post(f"/api/v1/openstack-instances/{inst.id}/sync", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["task_id"]


async def test_delete_takes_back_what_it_wrote(client, auth_headers, db_session, monkeypatch) -> None:
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    await db_session.commit()
    cluster_id, inst_id = inst.cluster_id, inst.id
    r = await client.delete(f"/api/v1/openstack-instances/{inst_id}", headers=auth_headers)
    assert r.status_code == 204
    db_session.expire_all()
    assert not (await db_session.execute(select(VirtualMachine).where(
        VirtualMachine.cluster_id == cluster_id))).scalars().first()
    assert not (await db_session.execute(select(OpenStackPort).where(
        OpenStackPort.instance_id == inst_id))).scalars().first()
    assert await db_session.get(VirtCluster, cluster_id) is not None


async def test_system_transfer_round_trip(db_session, monkeypatch) -> None:
    from app.services.system_transfer import exporter, importer
    sub = await _subnet(db_session, "198.51.100.0/24")
    inst = await _inst(db_session)
    _use(monkeypatch, _basic_cloud())
    await svc.sync_instance(db_session, inst)
    row = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.subnet_id == SUB_PRIV))).scalars().one()
    row.mapped_manually = True
    await db_session.commit()
    inst_id, sub_id = inst.id, sub.id
    inner = await exporter.build_export(db_session, ["core", "integrations", "synced"])
    exported = inner["tables"]["openstack_instances"][0]
    assert exported["__secrets__"]["secret"] == "Secret-2026"
    assert "secret_enc" not in exported
    assert len(inner["tables"]["openstack_subnets"]) == 2
    assert len(inner["tables"]["openstack_ports"]) == 3
    report = await importer.apply_import(db_session, inner, mode="replace", dry_run=False)
    for t in ("openstack_instances", "openstack_subnets", "openstack_ports"):
        assert report["tables"][t]["errored"] == 0, (t, report["tables"][t])
    db_session.expire_all()
    back = await db_session.get(OpenStackInstance, inst_id)
    assert svc.decrypt_secret_of(back) == "Secret-2026"
    row = (await db_session.execute(select(OpenStackSubnet).where(OpenStackSubnet.subnet_id == SUB_PRIV))).scalars().one()
    assert (row.mapped_subnet_id, row.mapped_manually) == (sub_id, True)
    assert (await db_session.execute(select(func.count()).select_from(OpenStackPort))).scalar_one() == 3


def test_transfer_categories() -> None:
    from app.services.system_transfer import registry
    assert registry.CATEGORY["openstack_instances"] == "integrations"
    # 子網路對應含使用者的手動決定：跟著連線設定走，不當成可重抓的鏡像
    assert registry.CATEGORY["openstack_subnets"] == "integrations"
    assert registry.CATEGORY["openstack_ports"] == "synced"


def _self_signed_pem() -> str:
    from datetime import UTC, datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Internal CA")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


async def test_reduced_scope_is_not_an_error_and_still_cleans_own_project(db_session, monkeypatch) -> None:
    """reader 被拒絕跨專案（10-22 對真的 DevStack 驗到）：

    - 這是權限限制、不是同步失敗：不寫 last_error（否則「整合同步失敗」通知會一直發），能力裡記 all_projects=false
    - 自己這個 project 的清單是完整的：這個 project 裡被刪掉的 VM 照樣清；別的 project 的不動
    """
    inst = await _inst(db_session)
    cloud = _basic_cloud()
    cloud.token_scope_project = F.DEMO_PROJECT
    _use(monkeypatch, cloud)
    cloud.servers.append(F.server(S3, "alt-vm", project=F.ALT_PROJECT))
    await svc.sync_instance(db_session, inst)           # 先以跨專案看過一次（alt-vm 也在）
    await db_session.flush()
    cloud.forbid_all_tenants = True
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert inst.last_error is None, inst.last_error
    assert inst.capabilities["all_projects"] is False
    assert out["removed_vms"] == 0
    # 自己 project 的一台在 OpenStack 上被刪掉
    gone = next(s for s in cloud.servers if s.get("tenant_id") == F.DEMO_PROJECT)
    cloud.servers.remove(gone)
    out = await svc.sync_instance(db_session, inst)
    await db_session.flush()
    assert out["removed_vms"] == 1
    names = set((await db_session.execute(select(VirtualMachine.name).where(
        VirtualMachine.cluster_id == inst.cluster_id))).scalars())
    assert "alt-vm" in names, "別的 project 的 VM 看不到不代表被刪"
    assert gone["name"] not in names
