#!/bin/bash
# 把 jt-ipam 的測試資料灌進 OpenStack 測試靶（在主機上執行；實際動作在控制節點容器裡用 openstack CLI 做）。
#
#   scripts/openstack-test-target/seed.sh                 建立測試資料（可重複執行，已存在的沿用）
#   scripts/openstack-test-target/seed.sh --reset         先刪掉種子資料再重建（UUID 會全部換新）
#   scripts/openstack-test-target/seed.sh --info [檔案]   只印連線資訊與密碼（給 jt-ipam 設定用）
#
# 種子摘要（各物件 UUID、IP、所在主機）存在控制節點的 /root/jt-target-seed.json。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
. "$HERE/common.sh"

KEYSTONE="http://${CTL_IP}/identity/v3"

info() {
    local sec
    sec="$(lxc exec "$CTL" -- cat /root/jt-target.secrets)"
    cat <<INFO
Keystone v3       : ${KEYSTONE}   （http，無 TLS；網址帶 /identity 路徑，不是 :5000）
區域／端點介面     : ${REGION} / public
管理員            : admin / $(sed -n 's/^ADMIN_PASSWORD=//p' <<<"$sec")   （專案 admin，網域 Default）
唯讀帳號          : jtipam-reader / $(sed -n 's/^READER_PASSWORD=//p' <<<"$sec")   （reader：admin、proj-a、proj-b、proj-c＋system all）
一般成員          : jtipam-member / $(sed -n 's/^MEMBER_PASSWORD=//p' <<<"$sec")   （member：只有 proj-a）
應用程式憑證       : id $(sed -n 's/^APPCRED_ID=//p' <<<"$sec")
                    secret $(sed -n 's/^APPCRED_SECRET=//p' <<<"$sec")   （jtipam-reader 建立，範圍 proj-a，角色 reader）
運算主機          : ${CTL}、${CMP}
INFO
}

case "${1:-}" in
    --info) info; exit 0 ;;
    --reset|"") ;;
    *) echo "用法：$0 [--reset|--info]" >&2; exit 2 ;;
esac

lxc exec "$CTL" -- test -f /opt/stack/.jt-stacked || die "控制節點還沒建好，先跑 $HERE/create.sh"
lxc file push "$HERE/seed-inner.sh" "$CTL/root/jt-seed-inner.sh" --mode 0700
lxc exec "$CTL" --env CTL_HOST="$CTL" --env CMP_HOST="$CMP" --env KEYSTONE="$KEYSTONE" \
    -- /root/jt-seed-inner.sh "$@"
info
log "種子摘要：lxc exec $CTL -- cat /root/jt-target-seed.json"
