#!/usr/bin/env bash
# jt-ipam certificate distribution agent one-shot installer (systemd timer).
#
# Supported: Debian 11/12/13, Ubuntu 22.04/24.04/26.04, RHEL/Rocky/AlmaLinux/CentOS, Fedora,
#            openSUSE/SLES (apt / dnf / yum / zypper auto-detected; all use systemd).
# The agent itself depends only on curl + coreutils (no Python / jq / YAML).
# Target-site profiles: nginx / apache(httpd) / haproxy / postfix / dovecot /
#                       Proxmox VE(pve) / Proxmox Mail Gateway(pmg) / Proxmox Backup Server(pbs) /
#                       Zimbra / generic (custom paths + reload).
#
# Usage:
#   sudo JT_IPAM_URL=https://ipam.example.com JT_IPAM_AGENT_KEY=<key> ./jt-ipam-cert-agent-installer.sh
# Options:
#   JT_IPAM_INSECURE=1      set to 1 when the server cert is self-signed (writes VERIFY_TLS=false)
#   JT_IPAM_ONCALENDAR=...  custom schedule (default: daily)
#   JT_IPAM_UNINSTALL=1     remove the agent (timer, service, agent files, config and state) and exit
#
# Re-running re-downloads the latest agent and overwrites it; your config (with your deployments) is kept.
set -euo pipefail

DEST=/usr/local/lib/jt-ipam-cert-agent
CONFDIR=/etc/jt-ipam-cert-agent
CONF="$CONFDIR/config"
AGENT="$DEST/jt_ipam_cert_agent.sh"
STATEDIR=/var/lib/jt-ipam-cert-agent
SVC=jt-ipam-cert-agent

[[ $EUID -eq 0 ]] || { echo "Run as root / sudo" >&2; exit 1; }

# ── Uninstall ──
if [[ -n "${JT_IPAM_UNINSTALL:-}" ]]; then
    systemctl stop "${SVC}.timer" "${SVC}.service" 2>/dev/null || true
    systemctl disable "${SVC}.timer" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SVC}.timer" "/etc/systemd/system/${SVC}.service"
    systemctl daemon-reload 2>/dev/null || true
    rm -rf "$DEST" "$CONFDIR" "$STATEDIR"
    echo "Uninstalled: removed timer/service, ${DEST}, ${CONFDIR} and ${STATEDIR}."
    echo "Note: certificate files already deployed to your services are left untouched."
    exit 0
fi

: "${JT_IPAM_URL:?JT_IPAM_URL is required, e.g. https://ipam.example.com}"
: "${JT_IPAM_AGENT_KEY:?JT_IPAM_AGENT_KEY is required (obtained when creating a cert-agent)}"
JT_IPAM_INSECURE="${JT_IPAM_INSECURE:-}"
JT_IPAM_ONCALENDAR="${JT_IPAM_ONCALENDAR:-daily}"

# ── Detect package manager (only needed to install curl if missing) ──
if   command -v apt-get >/dev/null; then PM=apt
elif command -v dnf     >/dev/null; then PM=dnf
elif command -v yum     >/dev/null; then PM=yum
elif command -v zypper  >/dev/null; then PM=zypper
else PM=""; fi

pkg_install() {
    case "$PM" in
        apt)    DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" ;;
        dnf)    dnf install -y -q "$@" ;;
        yum)    yum install -y -q "$@" ;;
        zypper) zypper --non-interactive --quiet install "$@" ;;
        *)      return 1 ;;
    esac
}

# ── Only dependency: curl (coreutils ships on every distro) ──
command -v curl >/dev/null || pkg_install curl || { echo "curl is required, please install it manually" >&2; exit 1; }

# ── Download agent ──
install -d "$DEST" "$CONFDIR"
CURL_OPTS=(-fsSL); [[ -n "$JT_IPAM_INSECURE" ]] && CURL_OPTS+=(-k)
curl "${CURL_OPTS[@]}" "${JT_IPAM_URL%/}/api/v1/cert-agents/agent.sh" -o "$AGENT"
chmod 0755 "$AGENT"

# ── Config (create a template only if absent; never overwrite your deployments) ──
if [[ ! -f "$CONF" ]]; then
    cat > "$CONF" <<EOF
SERVER=${JT_IPAM_URL%/}
AGENT_KEY=${JT_IPAM_AGENT_KEY}
VERIFY_TLS=$([[ -n "$JT_IPAM_INSECURE" ]] && echo false || echo true)
AUTO_UPDATE=true
TLS_BASE=/etc/ssl/jt-ipam

# Each deployment is a group of DEPLOY_<N>_* lines (one setting per line). N = 1, 2, 3, ...
# Pick the service via PROFILE; it provides the file paths AND the reload command.
#
#DEPLOY_1_CERT=wildcard-example-com
#DEPLOY_1_PROFILE=nginx
#
# Built-in PROFILE values (default paths use TLS_BASE above; <cert> = the cert name):
#   nginx    fullchain <base>/<cert>.fullchain.pem + key <base>/<cert>.key       reload: systemctl reload nginx
#   apache   cert <base>/<cert>.crt + chain .chain.pem + key .key                reload: systemctl reload apache2 || httpd
#   haproxy  combined <base>/<cert>.pem (cert+chain+key)                         reload: systemctl reload haproxy
#   postfix  fullchain <base>/<cert>.fullchain.pem + key .key                    reload: systemctl reload postfix
#   dovecot  fullchain <base>/<cert>.fullchain.pem + key .key                    reload: systemctl reload dovecot
#   pve      /etc/pve/local/pveproxy-ssl.pem + .key                              reload: systemctl restart pveproxy
#   pmg      /etc/pmg/pmg-api.pem (cert+chain+key)                               reload: systemctl restart pmgproxy
#   pbs      /etc/proxmox-backup/proxy.pem + .key                                reload: systemctl reload proxmox-backup-proxy
#   zimbra   Zimbra cert deployment
#   generic  no fixed paths/reload - you set the paths and RELOAD yourself (below)
#
# Optionally override where the files go (keep PROFILE for the reload):
#   DEPLOY_1_FULLCHAIN=  cert + chain          DEPLOY_1_KEY=       private key
#   DEPLOY_1_CRT=        leaf cert only        DEPLOY_1_CHAIN=     intermediate chain
#   DEPLOY_1_COMBINED=   cert + chain + key in one file
# Advanced: DEPLOY_1_RELOAD= / DEPLOY_1_TEST=  override the profile's reload / config-test command.
EOF
    chmod 0600 "$CONF"
    echo "Created config template: $CONF (edit DEPLOY_N before enabling)"
else
    echo "Config already exists, leaving it untouched: $CONF"
fi

# ── systemd service + timer (all distros use systemd) ──
cat > "/etc/systemd/system/${SVC}.service" <<EOF
[Unit]
Description=jt-ipam certificate distribution agent
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/env bash ${AGENT} --config ${CONF}
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

echo "Done (package manager: ${PM:-unknown}). Next steps:"
echo "  1) Edit DEPLOY_N in ${CONF}"
echo "  2) Dry-run first (no changes): bash ${AGENT} --config ${CONF} --dry-run"
echo "  3) Run once for real:          bash ${AGENT} --config ${CONF}"
echo "  Schedule: ${SVC}.timer (${JT_IPAM_ONCALENDAR}); status: systemctl status ${SVC}.timer"
