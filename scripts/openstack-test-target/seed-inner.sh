#!/bin/bash
# 在控制節點容器裡執行（由 seed.sh 推進去跑，不要在主機上直接跑）。
# 用 openstack CLI 建立 jt-ipam 測試用的專案、帳號、網路、埠、浮動 IP 與 fake 虛擬機。
# 可以重複執行：已存在的物件沿用不重建。加 --reset 會先刪掉這支腳本建立的所有物件。
# 全部是假資料：位址只用 RFC 5737（203.0.113.0/24）、RFC 3849（2001:db8::/32）與私網。
set -euo pipefail

. /root/jt-target.secrets
export OS_CLOUD=devstack-admin
OSC=/opt/stack/data/venv/bin/openstack
[ -x "$OSC" ] || OSC=openstack
CTL_HOST="${CTL_HOST:?}"
CMP_HOST="${CMP_HOST:?}"
KEYSTONE="${KEYSTONE:?}"
PROJECTS="proj-a proj-b proj-c"
BULK_COUNT="${BULK_COUNT:-35}"

o() { "$OSC" "$@"; }
log() { printf '[seed] %s\n' "$*"; }
# 依名稱取 id（找不到回空字串）
idof() { o "$@" -f value -c id 2>/dev/null || true; }
inproj() { local p=$1; shift; "$OSC" --os-project-name "$p" --os-project-domain-name Default "$@"; }

reset_all() {
    log "刪除舊的種子資料"
    for p in $PROJECTS; do
        pid=$(idof project show "$p")
        [ -n "$pid" ] || continue
        for s in $(o server list --all-projects --project "$pid" -f value -c ID); do o server delete --wait "$s" || true; done
        for f in $(o floating ip list --project "$pid" -f value -c ID); do o floating ip delete "$f" || true; done
        for r in $(o router list --project "$pid" -f value -c ID); do
            o router unset --external-gateway "$r" || true
            for sn in $(o port list --router "$r" -f json | python3 -c 'import json,sys;[print(i["subnet_id"]) for p in json.load(sys.stdin) for i in p["Fixed IP Addresses"]]' | sort -u); do
                o router remove subnet "$r" "$sn" || true
            done
            o router delete "$r" || true
        done
        for pt in $(o port list --project "$pid" -f value -c ID); do o port delete "$pt" || true; done
        for n in $(o network list --project "$pid" -f value -c ID); do o network delete "$n" || true; done
    done
    for n in shared-net ext-net; do
        nid=$(idof network show "$n")
        [ -n "$nid" ] || continue
        for pt in $(o port list --network "$nid" --device-owner '' -f value -c ID); do o port delete "$pt" || true; done
        o network delete "$nid" || true
    done
    for u in jtipam-reader jtipam-member; do o user delete "$u" 2>/dev/null || true; done
    for p in $PROJECTS; do o project delete "$p" 2>/dev/null || true; done
    o image delete jt-fake-image 2>/dev/null || true
    o flavor delete jt.tiny 2>/dev/null || true
    sed -i '/^APPCRED_/d' /root/jt-target.secrets
}

if [ "${1:-}" = "--reset" ]; then
    reset_all
fi

# ---------- Keystone ----------
for p in $PROJECTS; do
    [ -n "$(idof project show "$p")" ] || o project create --domain Default --description "jt-ipam 測試專案（假資料）" "$p" >/dev/null
    # 種子腳本用 admin 帳號代替各專案建立資源，所以 admin 要在各專案有 admin 角色
    o role add --project "$p" --user admin admin
done
log "專案：admin $PROJECTS"

ensure_user() {
    local u=$1 pw=$2
    if [ -z "$(idof user show --domain Default "$u")" ]; then
        o user create --domain Default --password "$pw" --description "jt-ipam 測試帳號" "$u" >/dev/null
    else
        o user set --password "$pw" "$u"
    fi
}
ensure_user jtipam-reader "$READER_PASSWORD"
ensure_user jtipam-member "$MEMBER_PASSWORD"
for p in admin $PROJECTS; do o role add --project "$p" --project-domain Default --user jtipam-reader --user-domain Default reader; done
o role add --system all --user jtipam-reader --user-domain Default reader
o role add --project proj-a --project-domain Default --user jtipam-member --user-domain Default member
log "帳號：jtipam-reader（reader：admin/proj-a/proj-b/proj-c＋system all）、jtipam-member（member：proj-a）"

# 應用程式憑證：以 jtipam-reader 本人建立，範圍 proj-a、角色 reader（密鑰只在建立時拿得到，存進 secrets）
if ! grep -q '^APPCRED_ID=' /root/jt-target.secrets; then
    RDR=(--os-auth-url "$KEYSTONE" --os-identity-api-version 3 --os-username jtipam-reader
         --os-password "$READER_PASSWORD" --os-user-domain-name Default --os-project-name proj-a
         --os-project-domain-name Default)
    env -u OS_CLOUD "$OSC" "${RDR[@]}" application credential delete jtipam-reader-ac 2>/dev/null || true
    env -u OS_CLOUD "$OSC" "${RDR[@]}" application credential create --role reader \
        --description "jt-ipam 測試用唯讀憑證" jtipam-reader-ac -f json >/tmp/ac.json
    python3 - <<'PY' >>/root/jt-target.secrets
import json
d = {k.lower(): v for k, v in json.load(open("/tmp/ac.json")).items()}
print(f"APPCRED_ID={d['id']}")
print(f"APPCRED_SECRET={d['secret']}")
PY
    rm -f /tmp/ac.json
fi
log "應用程式憑證：jtipam-reader-ac"

# ---------- 映像與規格（fake 驅動不會真的開機，1 MB 的假映像就夠） ----------
if [ -z "$(idof image show jt-fake-image)" ]; then
    head -c 1048576 /dev/zero >/tmp/jt-fake.img
    o image create --disk-format raw --container-format bare --public --file /tmp/jt-fake.img jt-fake-image >/dev/null
    rm -f /tmp/jt-fake.img
fi
[ -n "$(idof flavor show jt.tiny)" ] || o flavor create --vcpus 1 --ram 128 --disk 1 --public jt.tiny >/dev/null
log "映像 jt-fake-image、規格 jt.tiny"

# ---------- Neutron ----------
ensure_net() {   # 專案 名稱 [其他參數]
    local p=$1 n=$2; shift 2
    [ -n "$(idof network show "$n")" ] || inproj "$p" network create "$@" "$n" >/dev/null
}
ensure_subnet() {  # 專案 網路 子網路名稱 [其他參數]
    local p=$1 n=$2 s=$3; shift 3
    [ -n "$(idof subnet show "$s")" ] || inproj "$p" subnet create --network "$n" "$@" "$s" >/dev/null
}

ensure_net admin ext-net --external --provider-network-type flat --provider-physical-network public
ensure_subnet admin ext-net ext-subnet --subnet-range 203.0.113.0/24 --gateway 203.0.113.1 --no-dhcp \
    --allocation-pool start=203.0.113.100,end=203.0.113.199
ensure_net admin shared-net --share
ensure_subnet admin shared-net shared-subnet --subnet-range 10.50.0.0/24
ensure_net proj-a net-a
ensure_subnet proj-a net-a subnet-a --subnet-range 192.168.100.0/24
ensure_net proj-b net-b
ensure_subnet proj-b net-b subnet-b --subnet-range 192.168.100.0/24   # 刻意和 net-a 重疊
ensure_net proj-c net-c-v6
ensure_subnet proj-c net-c-v6 subnet-c-v4 --subnet-range 172.31.10.0/24
ensure_subnet proj-c net-c-v6 subnet-c-v6 --ip-version 6 --subnet-range 2001:db8:10::/64 \
    --ipv6-ra-mode slaac --ipv6-address-mode slaac
ensure_net proj-c net-c-bulk
ensure_subnet proj-c net-c-bulk subnet-c-bulk --subnet-range 172.31.20.0/24
log "網路：ext-net shared-net net-a net-b net-c-v6 net-c-bulk"

ensure_router() {  # 專案 路由器 子網路...
    local p=$1 r=$2; shift 2
    if [ -z "$(idof router show "$r")" ]; then
        inproj "$p" router create "$r" >/dev/null
        inproj "$p" router set --external-gateway ext-net "$r"
        for s in "$@"; do inproj "$p" router add subnet "$r" "$s"; done
    fi
}
ensure_router proj-a router-a subnet-a
ensure_router proj-b router-b subnet-b
ensure_router proj-c router-c subnet-c-v4 subnet-c-v6
log "路由器：router-a router-b router-c（閘道都在 ext-net）"

ensure_port() {  # 專案 名稱 [其他參數]
    local p=$1 n=$2; shift 2
    [ -n "$(idof port show "$n")" ] || inproj "$p" port create "$@" "$n" >/dev/null
}
# VIP 用的埠：沒接任何裝置（unattached），a1 的埠用 allowed_address_pairs 允許這個位址
ensure_port proj-a port-a-vip --network net-a --fixed-ip subnet=subnet-a,ip-address=192.168.100.200 \
    --description "VIP（未接裝置）"
ensure_port proj-a port-a1 --network net-a --fixed-ip subnet=subnet-a,ip-address=192.168.100.10 \
    --allowed-address ip-address=192.168.100.200 --description "a1 的網卡（allowed_address_pairs）"
log "埠：port-a-vip（未接裝置）、port-a1（allowed_address_pairs）"

# ---------- Nova（fake 驅動） ----------
ensure_server() {  # 專案 名稱 主機 [其他參數]
    local p=$1 n=$2 h=$3; shift 3
    local az=()
    [ -n "$h" ] && az=(--availability-zone "nova:$h")
    if [ -z "$(o server list --all-projects --name "^${n}\$" -f value -c ID)" ]; then
        inproj "$p" server create --image jt-fake-image --flavor jt.tiny "${az[@]}" "$@" "$n" >/dev/null
    fi
}
ensure_server proj-a a1 "$CTL_HOST" --port port-a1
ensure_server proj-a a2 "$CMP_HOST" --nic net-id="$(idof network show net-a)" --nic net-id="$(idof network show shared-net)"
ensure_server proj-a a3 "$CTL_HOST" --nic net-id="$(idof network show net-a)"
ensure_server proj-b b1 "$CMP_HOST" --nic net-id="$(idof network show net-b)",v4-fixed-ip=192.168.100.10
ensure_server proj-b b2 "$CTL_HOST" --nic net-id="$(idof network show shared-net)"
ensure_server proj-c c1 "$CMP_HOST" --nic net-id="$(idof network show net-c-v6)"
BULK_NET=$(idof network show net-c-bulk)
for i in $(seq -w 1 "$BULK_COUNT"); do
    ensure_server proj-c "bulk-$i" "" --nic net-id="$BULK_NET"
done

log "等虛擬機全部 ACTIVE"
for _ in $(seq 1 90); do
    pending=$(o server list --all-projects -f value -c Status | grep -vc -E '^(ACTIVE|SHUTOFF)$' || true)
    [ "$pending" = 0 ] && break
    sleep 2
done
o server list --all-projects -f value -c Name -c Status | grep -E 'ERROR' && { echo "有虛擬機是 ERROR" >&2; exit 1; }
# admin 在 admin 專案下用名稱找不到別的專案的虛擬機，要先用 --all-projects 拿到 UUID
sid_of() { o server list --all-projects --name "^${1}\$" -f value -c ID | head -1; }
A3=$(sid_of a3)
[ "$(o server show "$A3" -f value -c status)" = SHUTOFF ] || o server stop "$A3"
for _ in $(seq 1 30); do [ "$(o server show "$A3" -f value -c status)" = SHUTOFF ] && break; sleep 2; done
log "虛擬機：a1 a2 a3（關機） b1 b2 c1 bulk-01..bulk-$BULK_COUNT"

# ---------- 浮動 IP ----------
port_of() { o port list --device-id "$(sid_of "$1")" --network "$2" -f value -c ID | head -1; }
ensure_fip() {  # 專案 位址 [埠]
    local p=$1 ip=$2 port=${3:-}
    if [ -z "$(o floating ip list --floating-ip-address "$ip" -f value -c ID)" ]; then
        if [ -n "$port" ]; then
            inproj "$p" floating ip create --floating-ip-address "$ip" --port "$port" ext-net >/dev/null
        else
            inproj "$p" floating ip create --floating-ip-address "$ip" --description "已配置未綁定" ext-net >/dev/null
        fi
    fi
}
ensure_fip proj-a 203.0.113.110 "$(port_of a1 net-a)"
ensure_fip proj-b 203.0.113.120 "$(port_of b1 net-b)"
ensure_fip proj-c 203.0.113.130
log "浮動 IP：203.0.113.110→a1、203.0.113.120→b1、203.0.113.130（未綁定）"

# ---------- 匯出摘要（給 seed.sh 抓回主機） ----------
python3 - "$OSC" >/root/jt-target-seed.json <<'PY'
import json, subprocess, sys
osc = sys.argv[1]
def j(*a):
    return json.loads(subprocess.check_output([osc, *a, "-f", "json"]))
projects = {p["Name"]: p["ID"] for p in j("project", "list")}
pname = {v: k for k, v in projects.items()}
out = {"projects": projects,
       "users": {u["Name"]: u["ID"] for u in j("user", "list", "--domain", "Default") if u["Name"].startswith("jtipam-")},
       "networks": [], "subnets": [], "routers": [], "servers": [], "ports": [], "floating_ips": [], "hypervisors": []}
for n in j("network", "list", "--long"):
    out["networks"].append({"name": n["Name"], "id": n["ID"], "project": pname.get(n["Project"], n["Project"]),
                            "shared": n["Shared"], "external": n["Router Type"] in (True, "External")})
for s in j("subnet", "list", "--long"):
    out["subnets"].append({"name": s["Name"], "id": s["ID"], "cidr": s["Subnet"], "network_id": s["Network"],
                           "project": pname.get(s["Project"], s["Project"])})
for r in j("router", "list", "--long"):
    out["routers"].append({"name": r["Name"], "id": r["ID"], "project": pname.get(r["Project"], r["Project"])})
for s in j("server", "list", "--all-projects", "--long"):
    out["servers"].append({"name": s["Name"], "id": s["ID"], "status": s["Status"], "host": s["Host"],
                           "networks": s["Networks"]})
for p in j("port", "list", "--long"):
    out["ports"].append({"name": p["Name"], "id": p["ID"], "mac": p["MAC Address"], "fixed_ips": p["Fixed IP Addresses"],
                         "device_owner": p["Device Owner"], "status": p["Status"]})
for f in j("floating", "ip", "list", "--long"):
    out["floating_ips"].append({"id": f["ID"], "floating_ip": f["Floating IP Address"], "fixed_ip": f["Fixed IP Address"],
                                "port_id": f["Port"], "project": pname.get(f["Project"], f["Project"])})
for h in j("hypervisor", "list"):
    out["hypervisors"].append({"name": h["Hypervisor Hostname"], "id": h["ID"], "state": h["State"]})
json.dump(out, sys.stdout, indent=1, ensure_ascii=False)
PY
log "完成；摘要寫在 /root/jt-target-seed.json"
