#!/usr/bin/env bash
# test-fresh-install.sh — run a real first-time install in a throwaway container.
#
# Why this exists: every install problem customers have reported was invisible on
# an already-working box. A pre-existing PostgreSQL cluster on a different major,
# a silent pnpm failure, a systemd unit whose ReadWritePaths directory did not
# exist yet (fails as 226/NAMESPACE) -- none of them can reproduce on dev or prod,
# because there the thing is already there. The only way to see what a customer
# sees is to start from a clean OS.
#
# This is a release gate, not an optional extra. See TEST_CHECKLIST.md section 5b.
#
# Usage:  scripts/test-fresh-install.sh [debian:12|ubuntu:24.04|...]
# Needs:  docker, and a source tree at the repo root. Nothing else.
#
# The container runs systemd (privileged + host cgroups) because the whole point
# is to exercise the real units, timers and sandboxing -- not just the Python.

set -euo pipefail

IMAGE="${1:-debian:12}"
NAME="jt-install-test-$(echo "$IMAGE" | tr -c 'a-z0-9' '-')"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
pass() { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); }
dex()  { docker exec "$NAME" "$@"; }

command -v docker >/dev/null || { echo "docker is required"; exit 1; }

say "Starting a clean $IMAGE with systemd"
docker rm -f "$NAME" >/dev/null 2>&1 || true
# The base images ship no init, so PID 1 installs systemd and then becomes it.
docker run -d --name "$NAME" --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
    -e DEBIAN_FRONTEND=noninteractive "$IMAGE" \
    sh -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv \
           ca-certificates curl >/dev/null && exec /sbin/init' >/dev/null
for _ in $(seq 90); do
    state=$(dex systemctl is-system-running 2>/dev/null || true)
    [[ "$state" == running || "$state" == degraded ]] && break
    sleep 2
done
[[ "$state" == running || "$state" == degraded ]] || { fail "systemd never came up in $IMAGE"; exit 1; }
pass "systemd is up ($state)"

say "Copying the working tree in (as a customer's git clone would land)"
dex mkdir -p /opt/jt-ipam
tar -C "$ROOT" --exclude=.git --exclude=node_modules --exclude=.venv \
    --exclude=dist --exclude=__pycache__ --exclude=zap-reports -cf - . \
    | docker cp - "$NAME:/opt/jt-ipam"

say "Running scripts/jt-ipam.sh install (this is the part customers do)"
if dex env DEBIAN_FRONTEND=noninteractive bash /opt/jt-ipam/scripts/jt-ipam.sh install \
        >/tmp/$NAME.install.log 2>&1; then
    pass "install exited 0"
else
    fail "install failed -- last 40 lines:"; tail -40 "/tmp/$NAME.install.log"
fi

say "Checking the result the way a customer would"

# 1. The service actually answers -- on the URL a user would open. "Done" printed
#    by the installer is not evidence, and neither is a listening socket: in nginx
#    mode the backend can be perfectly healthy on 8000 while nginx is stopped and
#    nobody can reach the product (that is a real bug this test caught).
#    Note: /healthz is answered by nginx itself (static 200), so it cannot tell us
#    whether the backend is reachable. Ask for a route that has to be proxied --
#    401 means the backend answered; 502 means it did not.
mode=$(dex sh -c 'grep -oP "^BACKEND_TLS_MODE=\K\S+" /etc/jt-ipam/backend.env 2>/dev/null' || true)
if [[ "$mode" == "nginx" ]]; then
    url="https://127.0.0.1/api/v1/system/version"
else
    port=$(dex sh -c 'grep -oP "^BACKEND_BIND_PORT=\K\S+" /etc/jt-ipam/backend.env 2>/dev/null' || true)
    url="https://127.0.0.1:${port:-8443}/api/v1/system/version"
fi
code=$(dex curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "$url" 2>/dev/null || echo 000)
if [[ "$code" =~ ^[1-4] ]]; then
    pass "answers on $url (mode: ${mode:-unknown}, HTTP $code)"
else
    fail "no answer on $url (mode: ${mode:-unknown}, HTTP $code)"
    dex journalctl -u jt-ipam-backend -n 30 --no-pager || true
    dex systemctl status nginx --no-pager -l 2>/dev/null | head -15 || true
fi

# 2. Timer-driven units. These only ever fail in the field, hours after install,
#    which is exactly why they have to be triggered here.
for unit in jt-ipam-backup jt-ipam-sync; do
    dex systemctl start "$unit.service" >/dev/null 2>&1 || true
    for _ in $(seq 60); do
        dex systemctl is-active --quiet "$unit.service" || break
        sleep 2
    done
    result=$(dex systemctl show -p Result --value "$unit.service" 2>/dev/null || echo unknown)
    if [[ "$result" == success ]]; then
        pass "$unit ran successfully"
    else
        fail "$unit Result=$result"
        dex journalctl -u "$unit" -n 25 --no-pager || true
    fi
done

# 3. The sandbox self-heal: a missing ReadWritePaths directory is 226/NAMESPACE,
#    an error message that says nothing about the actual cause. One customer hit
#    exactly this and had to create /var/backups/jt-ipam by hand.
dex rm -rf /var/backups/jt-ipam
dex systemctl start jt-ipam-backup.service >/dev/null 2>&1 || true
sleep 5
if dex journalctl -u jt-ipam-backup -n 40 --no-pager 2>/dev/null | grep -q '226/NAMESPACE'; then
    fail "backup unit fails with 226/NAMESPACE when its directory is missing"
else
    pass "backup unit survives a missing /var/backups/jt-ipam"
fi

# 4. doctor must agree with reality -- a diagnostic that lies is worse than none.
if dex bash /opt/jt-ipam/scripts/jt-ipam.sh doctor >/tmp/$NAME.doctor.log 2>&1; then
    pass "doctor reports a healthy install"
else
    fail "doctor reports problems:"; grep -E '✗|→' "/tmp/$NAME.doctor.log" || true
fi

say "Result"
if [[ "$FAILED" -eq 0 ]]; then
    printf '\033[1;32mFresh install on %s is clean.\033[0m\n' "$IMAGE"
else
    printf '\033[1;31m%d check(s) failed on %s.\033[0m\n' "$FAILED" "$IMAGE"
fi
echo "Container kept as '$NAME' for inspection: docker exec -it $NAME bash"
echo "Remove it with: docker rm -f $NAME"
exit "$FAILED"
