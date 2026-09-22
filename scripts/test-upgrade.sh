#!/usr/bin/env bash
# test-upgrade.sh — install the *previous* release in a throwaway container, then upgrade to
# the current one, the way a customer site does it.
#
# Why this is separate from test-fresh-install.sh: a fresh install and an upgrade exercise
# different code. `install` starts from nothing; `upgrade` starts from **a machine that is
# already running an older version** — it has to pull, notice new OS packages, run migrations
# against real data, rebuild the frontend, and restart without losing the instance. Nothing in
# the fresh-install path touches any of that, so a release can pass that gate and still break
# every existing customer.
#
# This is a release gate. See TEST_CHECKLIST.md section 5b.
#
# Usage:  scripts/test-upgrade.sh [v0.6.15] [debian:12]
#         (first argument is the tag to install first; defaults to the previous tag)
# Needs:  docker, and network access to the public repository.
#
# The container runs systemd (privileged + host cgroups) because the upgrade restarts real
# units — a rebuild that succeeds while the service fails to come back is the failure that
# matters here, and only systemd can show it.

set -euo pipefail

FROM_REF="${1:-}"
IMAGE="${2:-debian:12}"
REPO="${JT_IPAM_REPO:-https://github.com/jasoncheng7115/jt-ipam.git}"
NAME="jt-upgrade-test-$(echo "$IMAGE" | tr -c 'a-z0-9' '-')"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
pass() { printf '  \033[1;32mPASS\033[0m %s\n' "$*"; }
fail() { printf '  \033[1;31mFAIL\033[0m %s\n' "$*"; FAILED=$((FAILED + 1)); }
dex()  { docker exec "$NAME" "$@"; }

command -v docker >/dev/null || { echo "docker is required"; exit 1; }

# 要升到的版本（工作樹目前的版本）
TO_VER="$(grep -m1 '"version"' "$ROOT/frontend/package.json" | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"

# 沒有指定起點就用「公開 repo 上、比目前版本舊的最後一個 tag」——
# 升級要從**別人手上真的在跑的那一版**開始，不是從隨便一個提交。
if [[ -z "$FROM_REF" ]]; then
    FROM_REF="$(git -C "$ROOT" tag --sort=-v:refname | grep -v "^v${TO_VER}$" | head -1)"
fi
[[ -n "$FROM_REF" ]] || { echo "cannot work out which version to start from"; exit 1; }

say "Upgrade path under test: $FROM_REF  →  v$TO_VER  (on $IMAGE)"

# 本機路徑當 origin：**關卡要測的是即將發出去的那份，不是已經發出去的那份**。
# 預設指向公開 repo 會讓 `upgrade` 拉到上一個released 版本，等於把剛修好的東西測不到。
MOUNT_ARGS=()
if [[ -d "$REPO" ]]; then
    MOUNT_ARGS=(-v "$(cd "$REPO" && pwd):/srv/jt-ipam-origin:ro")
    CLONE_URL="file:///srv/jt-ipam-origin"
    say "Origin: local repository $REPO (mounted read-only)"
else
    CLONE_URL="$REPO"
fi

say "Starting a clean $IMAGE with systemd"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
    "${MOUNT_ARGS[@]}" \
    -e DEBIAN_FRONTEND=noninteractive "$IMAGE" \
    sh -c 'apt-get update -qq && apt-get install -y -qq systemd systemd-sysv \
           ca-certificates curl git >/dev/null && exec /sbin/init' >/dev/null
state=""
# PID 1 先裝 systemd 再變成 systemd，所以這裡等的其實是**容器裡的 apt**。
# 原本給 90 次（180 秒）—— 在網路慢的建置機上 apt 光下載就要好幾分鐘，關卡會回
# 「systemd never came up」，看起來像 systemd 壞了，其實只是還沒裝完。
# 可用 SYSTEMD_WAIT_TRIES 覆寫。
for _ in $(seq "${SYSTEMD_WAIT_TRIES:-450}"); do
    state=$(dex systemctl is-system-running 2>/dev/null || true)
    [[ "$state" == running || "$state" == degraded ]] && break
    sleep 2
done
[[ "$state" == running || "$state" == degraded ]] || { fail "systemd never came up"; exit 1; }
pass "systemd is up ($state)"

say "Cloning the repository at $FROM_REF (what an existing site has)"
# 刻意建成一般分支而不是 detached HEAD：升級走 `git pull --ff-only`，
# detached HEAD 下那個指令根本不成立，測起來就不是客戶的情境。
# 掛進來的來源是唯讀、且擁有者與容器內的 root 對不上 → git 會擋「dubious ownership」。
# 非 bare 的 repo git 檢查的是 `<path>/.git`，光加 `<path>` 不夠（加了還是會被擋，
# 而且錯誤訊息只出現在 clone 那一步，看起來像連不到 remote）。兩個都加。
dex git config --global --add safe.directory /srv/jt-ipam-origin 2>/dev/null || true
dex git config --global --add safe.directory /srv/jt-ipam-origin/.git 2>/dev/null || true
dex git clone -q "$CLONE_URL" /opt/jt-ipam
dex git -C /opt/jt-ipam checkout -q -B main "$FROM_REF"
dex git -C /opt/jt-ipam branch -q --set-upstream-to=origin/main main
installed_ref="$(dex git -C /opt/jt-ipam describe --tags --always)"
pass "checked out $installed_ref"

say "Installing $FROM_REF (this is the site's starting state)"
if dex env DEBIAN_FRONTEND=noninteractive bash /opt/jt-ipam/scripts/jt-ipam.sh install \
        >/tmp/$NAME.install.log 2>&1; then
    pass "install exited 0"
else
    fail "install of $FROM_REF failed -- last 40 lines:"; tail -40 "/tmp/$NAME.install.log"
    echo "Container kept as '$NAME'"; exit 1
fi

before_ver="$(dex sh -c "grep -m1 '\"version\"' /opt/jt-ipam/frontend/package.json" | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
pass "site is running $before_ver"

say "Leaving a row behind, so the migration has real data to move"
dex bash -c 'set -a; . /etc/jt-ipam/backend.env; set +a;
  PGPASSWORD="$POSTGRES_PASSWORD" psql -h "${POSTGRES_HOST:-127.0.0.1}" -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" -qAtc "INSERT INTO sections (id, name, description, created_at, updated_at)
       VALUES (gen_random_uuid(), '\''upgrade-probe'\'', '\''written before the upgrade'\'', now(), now());"' \
  >/dev/null 2>&1 && pass "probe row written" || fail "could not write the probe row"

say "Running scripts/jt-ipam.sh upgrade (the part an existing site does)"
if dex env DEBIAN_FRONTEND=noninteractive bash /opt/jt-ipam/scripts/jt-ipam.sh upgrade \
        >/tmp/$NAME.upgrade.log 2>&1; then
    pass "upgrade exited 0"
else
    fail "upgrade failed -- last 60 lines:"; tail -60 "/tmp/$NAME.upgrade.log"
fi

say "Checking the result the way the site's admin would"

after_ver="$(dex sh -c "grep -m1 '\"version\"' /opt/jt-ipam/frontend/package.json" | sed -E 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/')"
if [[ "$after_ver" == "$TO_VER" ]]; then
    pass "version moved $before_ver -> $after_ver"
else
    fail "version is $after_ver, expected $TO_VER"
fi

# 版本字串是在**進程啟動時**讀進記憶體的：只 rebuild 前端會讓版本資訊頁落後。
running_ver="$(dex sh -c 'grep -m1 __version__ /opt/jt-ipam/backend/app/version.py' | sed -E 's/.*"([^"]+)".*/\1/')"
[[ "$running_ver" == "$TO_VER" ]] && pass "backend version.py at $running_ver" \
                                  || fail "backend version.py is $running_ver, expected $TO_VER"

if dex systemctl is-active --quiet jt-ipam-backend; then
    pass "jt-ipam-backend came back up"
else
    fail "jt-ipam-backend is not running after the upgrade"
    dex journalctl -u jt-ipam-backend -n 30 --no-pager || true
fi

code="$(dex curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1/api/v1/system/version || true)"
[[ "$code" == 401 || "$code" == 200 ]] && pass "answers over HTTPS (HTTP $code)" \
                                       || fail "no answer over HTTPS (HTTP ${code:-none})"

# 資料要還在 —— 升級把資料弄丟是最糟的失敗，而它不會讓任何指令回非零。
probe="$(dex bash -c 'set -a; . /etc/jt-ipam/backend.env; set +a;
  PGPASSWORD="$POSTGRES_PASSWORD" psql -h "${POSTGRES_HOST:-127.0.0.1}" -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" -qAtc "SELECT count(*) FROM sections WHERE name = '\''upgrade-probe'\'';"' \
  2>/dev/null || echo 0)"
[[ "$probe" == 1 ]] && pass "the row written before the upgrade is still there" \
                    || fail "the pre-upgrade row is gone (found $probe)"

if dex bash /opt/jt-ipam/scripts/jt-ipam.sh doctor >/tmp/$NAME.doctor.log 2>&1; then
    pass "doctor reports a healthy install"
else
    fail "doctor is unhappy after the upgrade:"; tail -30 "/tmp/$NAME.doctor.log"
fi

say "Result"
if [[ $FAILED -eq 0 ]]; then
    printf '\033[1;32mUpgrade %s -> v%s on %s is clean.\033[0m\n' "$FROM_REF" "$TO_VER" "$IMAGE"
else
    printf '\033[1;31m%d check(s) failed.\033[0m\n' "$FAILED"
fi
echo "Container kept as '$NAME' for inspection: docker exec -it $NAME bash"
echo "Remove it with: docker rm -f $NAME"
exit $((FAILED > 0))
