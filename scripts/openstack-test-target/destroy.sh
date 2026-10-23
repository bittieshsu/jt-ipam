#!/bin/bash
# 刪除 OpenStack 測試靶：兩個容器、LXD 設定檔、網路、儲存集區，以及 DOCKER-USER 裡我們加的兩條規則。
# 只刪這個測試靶自己建立的東西（名稱見 common.sh），其他 LXD／docker 容器不受影響。
#
#   scripts/openstack-test-target/destroy.sh        確認後刪除
#   scripts/openstack-test-target/destroy.sh -y     不確認直接刪
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

if [ "${1:-}" != "-y" ]; then
    read -r -p "要刪除 $CTL、$CMP 與網路 $BRIDGE、儲存集區 $POOL 嗎？[y/N] " ans
    [ "$ans" = y ] || [ "$ans" = Y ] || exit 1
fi
for c in "$CMP" "$CTL"; do
    lxc info "$c" >/dev/null 2>&1 && { log "刪除容器 $c"; lxc delete --force "$c"; }
done
lxc profile show "$PROFILE" >/dev/null 2>&1 && lxc profile delete "$PROFILE"
lxc network show "$BRIDGE" >/dev/null 2>&1 && lxc network delete "$BRIDGE"
if lxc storage show "$POOL" >/dev/null 2>&1; then
    lxc storage delete "$POOL" || log "儲存集區 $POOL 還有別的東西在用，保留"
fi
if iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    while iptables -D DOCKER-USER -i "$BRIDGE" -j ACCEPT 2>/dev/null; do :; done
    while iptables -D DOCKER-USER -o "$BRIDGE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null; do :; done
fi
log "已刪除。openvswitch 核心模組沒有卸載（其他程式可能在用），要卸載請手動 modprobe -r openvswitch"
