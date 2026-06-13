#!/usr/bin/env bash
# jt-ipam 憑證派送代理一鍵安裝器（systemd timer，每日檢查）。
#
# 支援:Debian 11/12/13、Ubuntu 22.04/24.04/26.04、RHEL/Rocky/AlmaLinux/CentOS、Fedora、
#       openSUSE/SLES（apt / dnf / yum / zypper 自動偵測；皆走 systemd）。
# 目標站台 profile:nginx / apache(httpd) / haproxy / postfix / dovecot /
#                  Proxmox VE(pve) / Proxmox Mail Gateway(pmg) / Proxmox Backup Server(pbs) /
#                  Zimbra / generic（自訂路徑+reload）。
#
# 用法:
#   sudo JT_IPAM_URL=https://ipam.example.com JT_IPAM_AGENT_KEY=<key> ./jt-ipam-cert-agent-installer.sh
# 選項:
#   JT_IPAM_INSECURE=1      server 憑證為自簽時設 1（config 寫 verify_tls: false）
#   JT_IPAM_ONCALENDAR=...  自訂排程（預設 daily）
#
# 重跑會重新下載最新 agent 並覆蓋;設定檔（含你的 deployments）不會被覆蓋。
set -euo pipefail

DEST=/usr/local/lib/jt-ipam-cert-agent
CONFDIR=/etc/jt-ipam-cert-agent
CONF="$CONFDIR/config.yaml"
SVC=jt-ipam-cert-agent

: "${JT_IPAM_URL:?JT_IPAM_URL is required, e.g. https://ipam.example.com}"
: "${JT_IPAM_AGENT_KEY:?JT_IPAM_AGENT_KEY is required（建立 cert-agent 時取得）}"
JT_IPAM_INSECURE="${JT_IPAM_INSECURE:-}"
JT_IPAM_ONCALENDAR="${JT_IPAM_ONCALENDAR:-daily}"

[[ $EUID -eq 0 ]] || { echo "請用 root / sudo 執行" >&2; exit 1; }

# ── 套件管理器偵測（跨發行版）──
if   command -v apt-get >/dev/null; then PM=apt
elif command -v dnf     >/dev/null; then PM=dnf
elif command -v yum     >/dev/null; then PM=yum
elif command -v zypper  >/dev/null; then PM=zypper
else PM=""; fi

pkg_install() {  # 安裝套件（任一失敗回非零）
    case "$PM" in
        apt)    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf)    dnf install -y -q "$@" ;;
        yum)    yum install -y -q "$@" ;;
        zypper) zypper --non-interactive --quiet install "$@" ;;
        *)      return 1 ;;
    esac
}
[[ "$PM" == "apt" ]] && apt-get update -qq || true

# ── 依賴:python3 + PyYAML + curl ──
command -v python3 >/dev/null || pkg_install python3 || { echo "需要 python3，請手動安裝" >&2; exit 1; }
command -v curl    >/dev/null || pkg_install curl    || true

if ! python3 -c 'import yaml' 2>/dev/null; then
    case "$PM" in
        apt)         pkg_install python3-yaml ;;
        dnf|yum)     pkg_install python3-pyyaml || pkg_install python3-PyYAML ;;
        zypper)      pkg_install python3-PyYAML || pkg_install python3-pyyaml ;;
    esac
    python3 -c 'import yaml' 2>/dev/null || pip3 install pyyaml 2>/dev/null || true
fi
python3 -c 'import yaml' 2>/dev/null || {
    echo "需要 PyYAML，請手動安裝（python3-yaml / python3-pyyaml / python3-PyYAML 之一）" >&2; exit 1; }

# ── 下載 agent ──
install -d "$DEST" "$CONFDIR"
CURL_OPTS=(-fsSL); [[ -n "$JT_IPAM_INSECURE" ]] && CURL_OPTS+=(-k)
curl "${CURL_OPTS[@]}" "${JT_IPAM_URL%/}/api/v1/cert-agents/agent.py" -o "$DEST/jt_ipam_cert_agent.py"
chmod 0755 "$DEST/jt_ipam_cert_agent.py"

# ── 設定檔（只在不存在時建樣板，不覆蓋你的 deployments）──
if [[ ! -f "$CONF" ]]; then
    cat > "$CONF" <<EOF
server: ${JT_IPAM_URL%/}
agent_key: "${JT_IPAM_AGENT_KEY}"
verify_tls: $([[ -n "$JT_IPAM_INSECURE" ]] && echo false || echo true)

# 列出此主機要部署哪些憑證、用哪個 profile。
# 內建 profile:nginx / apache / haproxy / postfix / dovecot / pve / pmg / pbs / zimbra / generic
# generic 必須自訂 *_path 與 reload。範例:
deployments: []
#  - cert: wildcard-example-com
#    profile: nginx
#  - cert: mail-cert
#    profile: pmg
EOF
    chmod 0600 "$CONF"
    echo "已建立設定檔樣板:$CONF（請編輯 deployments 後再啟用）"
else
    echo "設定檔已存在，保留不動:$CONF"
fi

# ── systemd service + timer（各發行版皆 systemd）──
cat > "/etc/systemd/system/${SVC}.service" <<EOF
[Unit]
Description=jt-ipam certificate distribution agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 ${DEST}/jt_ipam_cert_agent.py --config ${CONF}
EOF

cat > "/etc/systemd/system/${SVC}.timer" <<EOF
[Unit]
Description=Run jt-ipam cert agent on a schedule

[Timer]
OnCalendar=${JT_IPAM_ONCALENDAR}
RandomizedDelaySec=600
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable "${SVC}.timer" >/dev/null 2>&1 || true
systemctl start "${SVC}.timer"

echo "完成（套件管理器:${PM:-unknown}）。下一步:"
echo "  1) 編輯 ${CONF} 的 deployments"
echo "  2) 先試跑（不動檔）: python3 ${DEST}/jt_ipam_cert_agent.py --config ${CONF} --dry-run"
echo "  3) 正式跑一次:       python3 ${DEST}/jt_ipam_cert_agent.py --config ${CONF}"
echo "  排程:${SVC}.timer（${JT_IPAM_ONCALENDAR}）;狀態:systemctl status ${SVC}.timer"
