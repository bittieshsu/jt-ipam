"""瀏覽器 e2e 用的固定樣本資料。

為什麼要有這一支：e2e 的斷言寫的是**具體的名字**（`10.20.0.10`／`web-01`／`nas-01`／
`vcenter-lab`…），而這批資料原本只存在某一台開發機手動建的資料庫裡。換一台機器
就有一半的 spec 跑不動 —— 而且失敗訊息長得跟「功能壞掉」一模一樣（元素找不到、
文字對不上），每次都要重查一輪才能確定只是沒有資料。

只能對名字以 `_e2e` 結尾的資料庫跑。位址一律用文件保留範圍（RFC 5737）與私網範例，
不從實機抄任何名稱或位址。

用法：
    cd backend
    set -a; source /opt/jt-ipam/.dev.env; set +a
    POSTGRES_DB=jt_ipam_e2e .venv/bin/python -m tests.seed_e2e
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta


def _guard() -> None:
    name = os.environ.get("POSTGRES_DB", "")
    if not name.endswith("_e2e"):
        raise SystemExit(
            f"POSTGRES_DB={name!r} —— 這支只能對 *_e2e 的拋棄式資料庫跑（會重建樣本資料）。"
        )


async def seed() -> None:
    from app.core.db import SessionLocal
    from app.models.address import IPAddress
    from app.models.ai_finding import AIFinding
    from app.models.device import Device
    from app.models.dns import DNSRecord, DNSServer, DNSZone
    from app.models.fw_snapshot import FwRuleSnapshot
    from app.models.location import Location, Rack
    from app.models.nat import NATTranslation
    from app.models.physical import DevicePort
    from app.models.section import Section
    from app.models.subnet import Subnet
    from app.models.virt import VirtCluster, VirtualMachine, VMInterface
    from sqlalchemy import delete, select

    async with SessionLocal() as s:
        async def one(cls, **kw):
            """已存在就沿用，不存在才建 —— 重跑不會爆唯一鍵，也不會長出第二份。

            第一個參數叫 `cls` 不叫 `model`：有些資料表自己就有 `model` 欄位
            （裝置的型號），叫 model 會與關鍵字參數撞名。
            """
            key, val = next(iter(kw.items()))
            row = (await s.execute(select(cls).where(getattr(cls, key) == val))).scalars().first()
            if row:
                for k, v in kw.items():
                    setattr(row, k, v)
                return row
            row = cls(**kw)
            s.add(row)
            await s.flush()
            return row

        # ── 地點 / 機櫃 ──────────────────────────────────────────────
        loc = await one(Location, name="測試機房 A", description="e2e fixture")
        rack = await one(Rack, name="RACK-01", location_id=loc.id, u_height=42)

        # ── 區段 / 子網路 ────────────────────────────────────────────
        sec = await one(Section, name="e2e 樣本區段", description="seed_e2e 建立")
        subnets = {}
        for cidr, desc in (
            ("10.20.0.0/24", "伺服器網段"),
            ("198.51.100.0/24", "對外服務網段"),
            ("203.0.113.0/24", "管理網段"),
        ):
            sn = (await s.execute(select(Subnet).where(Subnet.cidr == cidr))).scalars().first()
            if not sn:
                sn = Subnet(section_id=sec.id, cidr=cidr, description=desc)
                s.add(sn)
                await s.flush()
            subnets[cidr] = sn

        # ── IP 位址 ─────────────────────────────────────────────────
        async def ip(subnet, addr, hostname, **kw):
            row = (await s.execute(select(IPAddress).where(
                IPAddress.subnet_id == subnet.id, IPAddress.ip == addr))).scalars().first()
            if not row:
                row = IPAddress(subnet_id=subnet.id, ip=addr, hostname=hostname, **kw)
                s.add(row)
                await s.flush()
            else:
                row.hostname = hostname
            return row

        web = await ip(subnets["10.20.0.0/24"], "10.20.0.10", "web-01",
                       description="e2e：IP 詳細資料的主要樣本")
        await ip(subnets["10.20.0.0/24"], "10.20.0.11", "app-01")
        await ip(subnets["10.20.0.0/24"], "10.20.0.12", "db-01")
        pub = await ip(subnets["198.51.100.0/24"], "198.51.100.7", "web.example.net",
                       description="e2e：對外開放服務的樣本")
        await ip(subnets["203.0.113.0/24"], "203.0.113.5", "ipmi-host-a",
                 description="e2e：管理介面樣本")

        # ── 裝置與連接埠 ─────────────────────────────────────────────
        # ⚠️ U 位置刻意放在機櫃頂端（42U 機櫃的 38~41）：機櫃圖由上往下畫，
        # 位置低的裝置在預設視窗下要捲動才看得到，而 e2e 用的是原始座標
        # `mouse.move`（不像 `.hover()` 會自動捲動）—— 放低了 hover 類的斷言
        # 會失敗，而且失敗訊息看起來像功能壞掉。
        nas = await one(Device, name="nas-01", type="storage", vendor="generic",
                        model="NAS-2000", location_id=loc.id, rack_id=rack.id,
                        u_position=40, u_size=2, rack_face="front")
        sw = await one(Device, name="sw-e2e-01", type="switch", vendor="generic",
                       model="SW-48G", location_id=loc.id, rack_id=rack.id,
                       u_position=38, u_size=1, rack_face="front")
        for dev, names in ((nas, ["eth0", "eth1"]), (sw, ["Gi0/1", "Gi0/2"])):
            for i, n in enumerate(names):
                exists = (await s.execute(select(DevicePort).where(
                    DevicePort.device_id == dev.id, DevicePort.name == n))).scalars().first()
                if not exists:
                    s.add(DevicePort(device_id=dev.id, name=n, type="ethernet", position=i))
        web.device_id = nas.id

        # ── 虛擬化（deep-sweep 期待 vcenter-lab / app-01）────────────
        cluster = await one(VirtCluster, name="vcenter-lab", type="vmware",
                            is_standalone=False, location_id=loc.id,
                            description="e2e fixture")
        # 虛擬化頁會提示「同時有 Proxmox VE 與 VMware」——只有一種平台時提示不出現
        pve = await one(VirtCluster, name="pve-lab", type="proxmox",
                        is_standalone=True, location_id=loc.id, description="e2e fixture")
        if not (await s.execute(select(VirtualMachine).where(
                VirtualMachine.name == "ct-log-01"))).scalars().first():
            s.add(VirtualMachine(cluster_id=pve.id, name="ct-log-01", legacy_vmid=101,
                                 node="pve-01", kind="ct", status="running",
                                 vcpus=2, memory_mb=2048, disk_gb=20))

        vm = (await s.execute(select(VirtualMachine).where(
            VirtualMachine.name == "app-01"))).scalars().first()
        if not vm:
            vm = VirtualMachine(cluster_id=cluster.id, name="app-01", external_id="vm-101",
                                node="esx-01.example.net", kind="vm", status="running",
                                vcpus=4, memory_mb=8192, disk_gb=80)
            s.add(vm)
            await s.flush()
        if not (await s.execute(select(VMInterface).where(VMInterface.vm_id == vm.id))).scalars().first():
            s.add(VMInterface(vm_id=vm.id, name="nic0", mac="00:00:5e:00:53:01",
                              primary_ip="10.20.0.11", bridge="VM Network"))

        # ── 對外開放服務（NAT port forward 指到已登錄的 IP）──────────
        if not (await s.execute(select(NATTranslation).where(
                NATTranslation.name == "e2e-https-forward"))).scalars().first():
            s.add(NATTranslation(name="e2e-https-forward", type="port_forward",
                                 dst_ip_id=pub.id, dst_port=443, protocol="tcp",
                                 src_interface="wan", description="e2e fixture",
                                 disabled=False, source_origin="seed:e2e"))

        # ── DNS（FQDN 視角要看到 A 與 CNAME 各自成列）────────────────
        dsrv = await one(DNSServer, name="e2e-dns", type="bind9",
                         server_address="192.0.2.53", enabled=False)
        zone = (await s.execute(select(DNSZone).where(DNSZone.name == "example.net"))).scalars().first()
        if not zone:
            zone = DNSZone(server_id=dsrv.id, name="example.net", type="forward",
                           managed=False, associated_subnet_ids=[])
            s.add(zone)
            await s.flush()
        for nm, rtype, val in (("web.example.net", "A", "198.51.100.7"),
                               ("meet.example.net", "CNAME", "web.example.net")):
            if not (await s.execute(select(DNSRecord).where(DNSRecord.name == nm))).scalars().first():
                s.add(DNSRecord(zone_id=zone.id, name=nm, type=rtype, value=val, ttl=300,
                                source="from_dns_pulled", consistency_state="consistent",
                                ipam_address_id=pub.id if rtype == "A" else None,
                                last_seen_at=datetime.now(UTC)))

        # ── AI 巡檢發現（UI 測的是渲染，不是模型；沒有 Ollama 也要跑得動）──
        await s.execute(delete(AIFinding).where(AIFinding.model == "gemma4:26b"))
        run_id = uuid.uuid4()
        findings = [
            # 第一筆是**給「忽略」測試用掉的**：spec 是序列跑的，AI 巡檢那組會把最上面
            # 那張卡片忽略掉，之後 deep-sweep 還要看得到「管理介面位於一般用途子網路」。
            # 沒有這筆誘餌的話，兩組測試只能有一組過，而失敗訊息看起來像功能壞掉。
            ("high", "naming", "多個位址共用同一個主機名稱",
             "198.51.100.7 與 10.20.0.10 在不同來源都回報同一個名稱，值班時會指向錯的機器。",
             "確認哪一個才是正解，並把另一個來源的名稱更正或停用。",
             {"ips": ["198.51.100.7", "10.20.0.10"]}),
            ("high", "exposure", "管理介面位於一般用途子網路",
             "203.0.113.5 是 IPMI 管理介面，卻登錄在一般用途的子網路裡。管理平面與服務"
             "平面沒有分開時，任何一台被攻下的服務主機都能直接打到管理介面。",
             "把管理介面移到獨立的管理網段，並限制只有跳板可以連。",
             {"ips": ["203.0.113.5"]}),
            # 至少要有**兩筆帶 IP 的發現**：spec 是序列跑的，前一條測試會忽略掉第一筆，
            # 只留一筆帶 IP 的話，下一條「依據資料的 IP 可以點過去查證」就找不到可點的
            # 位址 —— 失敗訊息看起來像功能壞掉，其實是樣本不夠撐過前一條測試。
            ("medium", "naming", "同一台主機在不同來源有不同名稱",
             "10.20.0.10 在 DNS 與 IPAM 裡的名稱不一致，值班時會對不起來。",
             "以其中一個來源為準，或在來源優先序裡調整順序。",
             {"ips": ["10.20.0.10"]}),
            ("medium", "coverage", "有子網路完全沒有任何監測來源",
             "10.20.0.0/24 裡的位址沒有任何一種存活證據來源（掃描代理／監控／防火牆），"
             "所以「上線」欄位在這個網段一律是未知。",
             "在該網段指派一台掃描代理，或把它納入既有監控的範圍。",
             {"subnets": ["10.20.0.0/24"]}),
        ]
        # 清單依 created_at 由新到舊排。**時間要各自寫死**：同一個交易裡插入的話
        # server_default 會給出一模一樣的時間戳，排序就變成不定 —— e2e 靠「第一張卡片」
        # 操作，順序不定等於測試會間歇性失敗。
        now = datetime.now(UTC)
        for i, (sev, cat, title, detail, rec, ev) in enumerate(findings):
            s.add(AIFinding(run_id=run_id, severity=sev, category=cat, title=title,
                            detail=detail, recommendation=rec, evidence=ev,
                            model="gemma4:26b", status="open",
                            created_at=now - timedelta(minutes=5 * i),
                            fingerprint=hashlib.sha256(title.encode()).hexdigest()[:32]))

        # ── 防火牆規則異動（zz-fwchanges-ai 期待 router-e2e）─────────
        if not (await s.execute(select(FwRuleSnapshot).where(
                FwRuleSnapshot.instance_name == "router-e2e"))).scalars().first():
            rules = [{"id": "1", "action": "pass", "interface": "wan",
                      "destination": "198.51.100.7", "destination_port": "8443",
                      "description": "e2e fixture rule"}]
            s.add(FwRuleSnapshot(
                source_type="opnsense", instance_id=uuid.uuid4(), instance_name="router-e2e",
                taken_at=datetime.now(UTC) - timedelta(hours=1),
                rules_hash=hashlib.sha256(b"e2e").hexdigest(), rule_count=len(rules),
                rules=rules, diff={"added": rules, "removed": [], "changed": []}))

        # 儀表板的 AI 巡檢區塊看 `/me` 的 ai_enabled（＝全域 LLM 開關），沒開就整塊
        # 不渲染 —— 那是設計，不是壞掉。這裡只把開關打開，不需要真的有 Ollama：
        # 區塊要的資料來自 ai-audit 的摘要端點，不會去呼叫模型。
        from app.models.system_setting import SystemSetting
        from app.services.system_config import LLM_KEY
        row = (await s.execute(select(SystemSetting).where(
            SystemSetting.key == LLM_KEY))).scalars().first()
        value = dict(row.value or {}) if row else {}
        value.update({"enabled": True, "url": "http://127.0.0.1:11434",
                      "chat_model": "gemma4:26b",
                      "embedding_model": "granite-embedding:278m"})
        if row:
            row.value = value
        else:
            s.add(SystemSetting(key=LLM_KEY, value=value))

        await s.commit()
        print("seed 完成：區段/子網路/IP/裝置/機櫃/虛擬化/NAT/DNS/AI 發現/防火牆快照")


if __name__ == "__main__":
    _guard()
    asyncio.run(seed())
