#!/usr/bin/env bash
# jt-ipam certificate distribution agent (pure bash; depends only on curl + coreutils, no Python / jq / YAML).
#
# Periodically (or once) asks jt-ipam "is there a newer version of the certificates I'm responsible for".
# If so, downloads each raw PEM part via curl, writes them to the site atomically, reloads only after
# config-test passes, rolls back automatically on failure, then reports the result back to jt-ipam.
#
# Config file (default /etc/jt-ipam-cert-agent/config, KEY=VALUE, one setting per line, sourced here):
#
#   SERVER=https://ipam.example.com
#   AGENT_KEY=<enrollment key>
#   VERIFY_TLS=true            # set false if the server cert is self-signed, or use CA_CERT
#   # CA_CERT=/etc/ssl/certs/ipam-ca.pem
#   AUTO_UPDATE=true           # auto-update self when the server has a newer agent
#   TLS_BASE=/etc/ssl/jt-ipam  # default write directory for built-in profiles
#
#   # Each deployment is a group of DEPLOY_<N>_* lines (one setting per line). N = 1, 2, 3, ...
#   # Pick the service via PROFILE; it provides the file paths AND the reload command:
#   DEPLOY_1_CERT=wildcard-example-com          # which jt-ipam certificate
#   DEPLOY_1_PROFILE=nginx
#
#   # Built-in PROFILE values (default paths use TLS_BASE; <cert> = the cert name):
#   #   nginx    fullchain <base>/<cert>.fullchain.pem + key .key   reload: systemctl reload nginx
#   #   apache   cert .crt + chain .chain.pem + key .key            reload: systemctl reload apache2 || httpd
#   #   haproxy  combined <base>/<cert>.pem (cert+chain+key)        reload: systemctl reload haproxy
#   #   postfix  fullchain <base>/<cert>.fullchain.pem + key .key   reload: systemctl reload postfix
#   #   dovecot  fullchain <base>/<cert>.fullchain.pem + key .key   reload: systemctl reload dovecot
#   #   pve      /etc/pve/local/pveproxy-ssl.pem + .key             reload: systemctl restart pveproxy
#   #   pmg      /etc/pmg/pmg-api.pem (cert+chain+key)              reload: systemctl restart pmgproxy
#   #   pbs      /etc/proxmox-backup/proxy.pem + .key               reload: systemctl reload proxmox-backup-proxy
#   #   zimbra   Zimbra cert deployment
#   #   generic  no fixed paths/reload - you set the paths + RELOAD yourself
#
#   # Optional: override where files go (keep PROFILE for the reload):
#   #   DEPLOY_1_FULLCHAIN= (cert+chain)  DEPLOY_1_KEY=  DEPLOY_1_CRT= (leaf)  DEPLOY_1_CHAIN=  DEPLOY_1_COMBINED=
#   # Advanced: DEPLOY_1_RELOAD= / DEPLOY_1_TEST= override the profile's reload / config-test command.
#
# Usage: jt_ipam_cert_agent.sh [--config PATH] [--dry-run] [--version]
AGENT_VERSION=0.4.153

set -u
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
ARGS=("$@")
CONFIG=/etc/jt-ipam-cert-agent/config
STATE_DIR=/var/lib/jt-ipam-cert-agent
STATE_FILE="$STATE_DIR/state"
DRY_RUN=0

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --version) echo "$AGENT_VERSION"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 2; }
[ -r "$CONFIG" ] || { echo "cannot read config file $CONFIG" >&2; exit 2; }
# shellcheck disable=SC1090
. "$CONFIG"
: "${SERVER:?config missing SERVER}"
: "${AGENT_KEY:?config missing AGENT_KEY}"
VERIFY_TLS="${VERIFY_TLS:-true}"
AUTO_UPDATE="${AUTO_UPDATE:-true}"
TLS_BASE="${TLS_BASE:-/etc/ssl/jt-ipam}"
SERVER="${SERVER%/}"
API="$SERVER/api/v1/cert-agents"

CURL=(curl -fsS --max-time 30 -H "X-Agent-Key: $AGENT_KEY" -H "X-Agent-Version: $AGENT_VERSION")
[ "$VERIFY_TLS" = "false" ] && CURL+=(-k)
[ -n "${CA_CERT:-}" ] && CURL+=(--cacert "$CA_CERT")

# ─────────────────── Per-profile file list + test + reload ───────────────────
# Outputs to globals: PFILES (each line "kind|path|mode"), PTEST, PRELOAD, PSPECIAL
profile_spec() {
  local profile="$1" cert="$2" base="$3"
  PFILES=""; PTEST=""; PRELOAD=""; PSPECIAL=""
  case "$profile" in
    nginx)
      PFILES="fullchain|$base/$cert.fullchain.pem|644
key|$base/$cert.key|600"
      PTEST="nginx -t"; PRELOAD="systemctl reload nginx" ;;
    apache)
      PFILES="cert|$base/$cert.crt|644
chain|$base/$cert.chain.pem|644
key|$base/$cert.key|600"
      PTEST="apachectl configtest 2>/dev/null || httpd -t"
      PRELOAD="systemctl reload apache2 2>/dev/null || systemctl reload httpd" ;;
    haproxy)
      PFILES="combined|$base/$cert.pem|600"
      PTEST="haproxy -c -f /etc/haproxy/haproxy.cfg"; PRELOAD="systemctl reload haproxy" ;;
    pve)
      PFILES="fullchain|/etc/pve/local/pveproxy-ssl.pem|640
key|/etc/pve/local/pveproxy-ssl.key|600"
      PRELOAD="systemctl restart pveproxy" ;;
    pmg)
      PFILES="combined|/etc/pmg/pmg-api.pem|600"
      PRELOAD="systemctl restart pmgproxy" ;;
    pbs)
      PFILES="fullchain|/etc/proxmox-backup/proxy.pem|640
key|/etc/proxmox-backup/proxy.key|600"
      PRELOAD="systemctl reload proxmox-backup-proxy" ;;
    postfix)
      PFILES="fullchain|$base/$cert.fullchain.pem|644
key|$base/$cert.key|600"
      PRELOAD="systemctl reload postfix" ;;
    dovecot)
      PFILES="fullchain|$base/$cert.fullchain.pem|644
key|$base/$cert.key|600"
      PRELOAD="systemctl reload dovecot" ;;
    zimbra)
      PSPECIAL="zimbra" ;;
    generic)
      : ;;  # overridden entirely via config
    *)
      return 1 ;;
  esac
  return 0
}

# Download one raw PEM part to a file; return 0 on success
fetch_part() {
  local cert="$1" part="$2" dest="$3"
  "${CURL[@]}" -G --data-urlencode "cert=$cert" --data-urlencode "part=$part" \
    -o "$dest" "$API/bundle/raw" 2>/dev/null
}

# ─────────────────── Apply a single deployment ───────────────────
# Global REPORT_LINES accumulates TSV: cert\tprofile\tstatus\tfingerprint\tnot_after\tdry_run\tmessage
REPORT_LINES=()
add_report() {  # cert profile status fp not_after message
  local msg="${6:-}"
  msg="${msg//$'\t'/ }"; msg="${msg//$'\n'/ }"
  REPORT_LINES+=("$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' "$1" "$2" "$3" "$4" "$5" "$DRY_RUN" "$msg")")
}

apply_deployment() {  # cert profile fp not_after  (+ D_* override vars)
  local cert="$1" profile="$2" fp="$3" na="$4"
  local base="$TLS_BASE"
  if ! profile_spec "$profile" "$cert" "$base"; then
    log "[$cert/$profile] unknown profile"; add_report "$cert" "$profile" failed "$fp" "$na" "unknown profile"; return 1
  fi

  # config overrides: reload / test / <kind>_path
  [ -n "${D_reload:-}" ] && PRELOAD="$D_reload"
  [ -n "${D_test:-}" ] && PTEST="$D_test"
  local files="$PFILES" line kind path mode
  # override each kind's path (generic relies on these too)
  local newfiles="" ov
  if [ -n "$files" ]; then
    while IFS='|' read -r kind path mode; do
      [ -z "$kind" ] && continue
      ov="D_${kind}_path"
      [ -n "${!ov:-}" ] && path="${!ov}"
      newfiles+="$kind|$path|$mode"$'\n'
    done <<< "$files"
  fi
  # generic / overrides: add kinds not in the profile defaults
  for kind in cert key chain fullchain combined; do
    ov="D_${kind}_path"
    if [ -n "${!ov:-}" ] && ! printf '%s' "$newfiles" | grep -q "^$kind|"; then
      local m=644; [ "$kind" = key ] || [ "$kind" = combined ] && m=600
      newfiles+="$kind|${!ov}|$m"$'\n'
    fi
  done
  files="$(printf '%s' "$newfiles")"

  if [ "$profile" = generic ] && [ -z "$files" ]; then
    log "[$cert/$profile] generic specifies no *_path"; add_report "$cert" "$profile" failed "$fp" "$na" "generic: no path specified"; return 1
  fi

  # dry-run: just print the plan
  if [ "$DRY_RUN" = 1 ]; then
    log "[$cert/$profile] (dry-run) would write:"
    while IFS='|' read -r kind path mode; do [ -n "$kind" ] && log "    $kind -> $path ($mode)"; done <<< "$files"
    [ -n "$PRELOAD" ] && log "    reload: $PRELOAD"
    add_report "$cert" "$profile" dry-run "$fp" "$na" "planned"
    return 0
  fi

  # Download + install (atomic swap + backup, restore on failure)
  local tmpd; tmpd="$(mktemp -d)"; local -a written=() backed=()
  rollback() {
    local p
    for p in "${written[@]}"; do rm -f "$p"; done
    for p in "${backed[@]}"; do mv -f "$p.jtbak" "$p" 2>/dev/null; done
  }
  while IFS='|' read -r kind path mode; do
    [ -z "$kind" ] && continue
    if ! fetch_part "$cert" "$kind" "$tmpd/f"; then
      log "[$cert/$profile] download $kind failed"; rollback; rm -rf "$tmpd"
      add_report "$cert" "$profile" failed "$fp" "$na" "download $kind failed"; return 1
    fi
    mkdir -p "$(dirname "$path")"
    if [ -e "$path" ]; then cp -p "$path" "$path.jtbak"; backed+=("$path"); else written+=("$path"); fi
    install -m "$mode" "$tmpd/f" "$path"
  done <<< "$files"
  rm -rf "$tmpd"

  # config-test
  if [ -n "$PTEST" ] && ! eval "$PTEST" >/dev/null 2>&1; then
    log "[$cert/$profile] config-test failed -> rollback"; rollback
    add_report "$cert" "$profile" failed "$fp" "$na" "config-test failed"; return 1
  fi
  # reload
  if [ -n "$PRELOAD" ] && ! eval "$PRELOAD" >/dev/null 2>&1; then
    log "[$cert/$profile] reload failed -> rollback"; rollback
    add_report "$cert" "$profile" failed "$fp" "$na" "reload failed"; return 1
  fi
  # success -> drop backups
  local p; for p in "${backed[@]}"; do rm -f "$p.jtbak"; done
  log "[$cert/$profile] applied (${fp:0:12}…)"
  add_report "$cert" "$profile" ok "$fp" "$na" "applied"
  return 0
}

# ─────────────────── Self-update ───────────────────
self_update() {
  local server_sha="$1"
  [ "$AUTO_UPDATE" = "false" ] && return 0
  [ -z "$server_sha" ] && return 0
  command -v sha256sum >/dev/null 2>&1 || return 0
  local self_sha; self_sha="$(sha256sum "$SELF" | cut -d' ' -f1)"
  [ "$server_sha" = "$self_sha" ] && return 0
  log "[update] server has a newer agent, downloading update…"
  local tmp="$SELF.new"
  if "${CURL[@]}" -o "$tmp" "$API/agent.sh" 2>/dev/null; then
    local new_sha; new_sha="$(sha256sum "$tmp" | cut -d' ' -f1)"
    if [ "$new_sha" = "$server_sha" ]; then
      chmod 0755 "$tmp"; mv -f "$tmp" "$SELF"
      log "[update] updated, re-executing new version"
      exec "$SELF" "${ARGS[@]}"
    fi
    log "[update] downloaded sha mismatch, skipping this round"; rm -f "$tmp"
  fi
}

# ─────────────────── Main flow ───────────────────
mkdir -p "$STATE_DIR"; touch "$STATE_FILE"
CHECK="$(mktemp)"
if ! "${CURL[@]}" -o "$CHECK" "$API/check?format=text" 2>/dev/null; then
  log "check failed (cannot reach $SERVER)"; rm -f "$CHECK"; exit 1
fi
SERVER_SHA="$(awk -F= '/^agent_sha=/{print $2}' "$CHECK")"
self_update "$SERVER_SHA"

state_fp() { awk -F'\t' -v c="$1" -v p="$2" '$1==c && $2==p{print $3}' "$STATE_FILE"; }
set_state() {  # cert profile fp
  local tmp; tmp="$(mktemp)"
  awk -F'\t' -v c="$1" -v p="$2" '!($1==c && $2==p)' "$STATE_FILE" > "$tmp"
  printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$tmp"; mv -f "$tmp" "$STATE_FILE"
}

# Read one config field for deployment N, e.g. _f 1 FULLCHAIN -> value of DEPLOY_1_FULLCHAIN
_f() { local v="DEPLOY_${1}_${2}"; printf '%s' "${!v:-}"; }

FAILED=0; n=1
while :; do
  cert="$(_f "$n" CERT)"
  [ -z "$cert" ] && break
  profile="$(_f "$n" PROFILE)"; profile="${profile:-generic}"
  # Map the one-per-line path / reload / test fields to the D_* overrides used by apply_deployment.
  unset "${!D_@}" 2>/dev/null || true
  [ -n "$(_f "$n" FULLCHAIN)" ] && D_fullchain_path="$(_f "$n" FULLCHAIN)"
  [ -n "$(_f "$n" KEY)" ]       && D_key_path="$(_f "$n" KEY)"
  [ -n "$(_f "$n" CHAIN)" ]     && D_chain_path="$(_f "$n" CHAIN)"
  [ -n "$(_f "$n" CRT)" ]       && D_cert_path="$(_f "$n" CRT)"
  [ -n "$(_f "$n" COMBINED)" ]  && D_combined_path="$(_f "$n" COMBINED)"
  [ -n "$(_f "$n" RELOAD)" ]    && D_reload="$(_f "$n" RELOAD)"
  [ -n "$(_f "$n" TEST)" ]      && D_test="$(_f "$n" TEST)"
  n=$((n+1))
  # look up this cert's current fingerprint from the check output
  fp="$(awk -F'\t' -v c="$cert" '$1==c{print $2}' "$CHECK")"
  na="$(awk -F'\t' -v c="$cert" '$1==c{print $3}' "$CHECK")"
  if [ -z "$fp" ]; then
    log "[$cert/$profile] cert not found on server or not in scope, skipping"
    add_report "$cert" "$profile" skipped "" "" "not in scope / no current version"; continue
  fi
  if [ "$DRY_RUN" != 1 ] && [ "$(state_fp "$cert" "$profile")" = "$fp" ]; then
    log "[$cert/$profile] already up to date (${fp:0:12}…), skipping"; continue
  fi
  if apply_deployment "$cert" "$profile" "$fp" "$na"; then
    [ "$DRY_RUN" != 1 ] && set_state "$cert" "$profile" "$fp"
  else
    FAILED=1
  fi
done
rm -f "$CHECK"

# report (TSV; does not affect deployment result)
if [ "${#REPORT_LINES[@]}" -gt 0 ]; then
  printf '%s\n' "${REPORT_LINES[@]}" | \
    "${CURL[@]}" -X POST -H "Content-Type: text/plain" --data-binary @- "$API/report" >/dev/null 2>&1 \
    || log "report failed (does not affect deployment)"
fi
exit "$FAILED"
