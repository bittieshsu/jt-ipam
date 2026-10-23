# OpenStack 測試靶共用設定（由 create.sh／start.sh／seed.sh／destroy.sh 載入，不單獨執行）
# 要改位址或名稱就用環境變數覆寫，四支腳本要用同一組值。

CTL="${OS_TT_CTL:-jtipam-os-ctl}"          # 控制節點容器（也是第一台 fake 運算主機）
CMP="${OS_TT_CMP:-jtipam-os-cmp}"          # 第二台 fake 運算主機
POOL="${OS_TT_POOL:-jtos}"                 # LXD 儲存集區（dir）
BRIDGE="${OS_TT_BRIDGE:-jtosbr0}"          # LXD 私網橋接
NET_PREFIX="${OS_TT_NET_PREFIX:-10.241.73}"
BRIDGE_CIDR="${NET_PREFIX}.1/24"
CTL_IP="${NET_PREFIX}.10"
CMP_IP="${NET_PREFIX}.11"
PROFILE="${OS_TT_PROFILE:-jtipam-openstack}"
IMAGE="${OS_TT_IMAGE:-ubuntu:24.04}"
DEVSTACK_BRANCH="${OS_TT_BRANCH:-stable/2026.1}"
REGION="${OS_TT_REGION:-RegionOne}"
CTL_MEM="${OS_TT_CTL_MEM:-8GiB}"
CMP_MEM="${OS_TT_CMP_MEM:-3GiB}"
CPUS="${OS_TT_CPUS:-4}"

log() { printf '[os-target] %s\n' "$*"; }
die() { printf '[os-target] 錯誤：%s\n' "$*" >&2; exit 1; }
LOGDIR="${OS_TT_LOGDIR:-/var/tmp/jt-os-target}"   # 主機端保留 stack.sh 的輸出
mkdir -p "$LOGDIR"
