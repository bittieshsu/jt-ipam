#!/usr/bin/env bash
# 假 CubeCOS API 的自我測試：啟動三個實例（HTTPS／HTTP＋Keystone＋路徑前綴／HTTP＋html200），
# 用 curl 打過每條路由，檢查回應外框、狀態碼與欄位。只會停掉自己啟動的行程。
# 用法：scripts/cubecos-test-target/selftest.sh
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
FAKE="$HERE/fake_cubecos.py"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/fake-cubecos-selftest.XXXXXX")
PIDS=()
PASS=0
FAIL=0
CODE=000

cleanup() {
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    wait 2>/dev/null
    rm -rf "$WORK"
}
trap cleanup EXIT

free_port() {
    python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
}

check() {
    local desc=$1; shift
    if "$@"; then
        PASS=$((PASS + 1)); echo "ok   - $desc"
    else
        FAIL=$((FAIL + 1)); echo "FAIL - $desc（HTTP $CODE）"
        echo "       body: $(head -c 400 "$WORK/body" 2>/dev/null)"
    fi
}

# req METHOD URL [curl 參數...]：回應內容存 $WORK/body、標頭存 $WORK/hdr、狀態碼放 $CODE
req() {
    local m=$1 u=$2; shift 2
    CODE=$(curl -s -o "$WORK/body" -D "$WORK/hdr" -w '%{http_code}' -X "$m" "${TLS[@]}" "$@" "$u")
}

# js 'Python 運算式'：d＝解析後的 JSON（失敗為 None）、raw＝原始內容、hdr＝標頭文字
js() {
    python3 - "$1" "$WORK/body" "$WORK/hdr" <<'PY'
import json, sys
expr, bp, hp = sys.argv[1:4]
raw = open(bp, encoding="utf-8").read()
hdr = open(hp, encoding="utf-8").read()
try:
    d = json.loads(raw)
except Exception:
    d = None
sys.exit(0 if eval(expr) else 1)
PY
}

code_is() { [ "$CODE" = "$1" ]; }

wait_up() {
    local url=$1
    for _ in $(seq 1 100); do
        if curl -s -o /dev/null "${TLS[@]}" "$url/live"; then return 0; fi
        sleep 0.1
    done
    echo "伺服器沒有起來：$url"; exit 1
}

json_field() { python3 -c "import json,sys;print(json.load(open('$WORK/body'))$1)"; }

NODE_KEYS="['id','serialNumber','boardSerial','dataCenter','hostname','role','protocol','address','ip','managementIP','storageIP','cpuSpec','networkInterfaces','blockDevices','vcpu','memory','storage','isVirtualIpOwner','ipmi','license','firmware','status','uptimeSeconds','labels']"
DC_KEYS="['type','name','roles','registeredRoles','version','virtualIp','isLocal','isHaEnabled','utcTimeZone','firmware','fixpack','additional']"

# =========================================================================
# 實例 A：HTTPS（測試 CA 嚴格驗證）、ACCEPT_KEYSTONE_TOKENS、預設 saml401
# =========================================================================
PA=$(free_port)
ACCEPT_KEYSTONE_TOKENS=ks-static-token python3 "$FAKE" --port "$PA" --cert-dir "$WORK/tlsA" \
    >"$WORK/a.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 100); do [ -f "$WORK/tlsA/ca.pem" ] && break; sleep 0.1; done
TLS=(--cacert "$WORK/tlsA/ca.pem")
A="https://127.0.0.1:$PA"
wait_up "$A"
API="$A/api/v1/datacenters/fake-dc"
echo "# 實例 A：$A"

req GET "$A/live"
check "/live 不用驗證、回 ok 且沒有 data" eval 'code_is 200 && js "d == {\"code\":200,\"msg\":\"api is alive\",\"status\":\"ok\"}"'

req GET "$A/api/v1/datacenters"
check "GET /datacenters 不用驗證、200" code_is 200
check "外框鍵依字母排序 code,data,msg,status" js "list(d) == ['code','data','msg','status'] and d['code'] == 200 and d['status'] == 'ok'"
check "資料中心欄位完整且順序正確" js "len(d['data']) == 1 and list(d['data'][0]) == $DC_KEYS"
check "資料中心內容（cloud、registeredRoles、v1ApiDocUrl）" js "d['data'][0]['type'] == 'cloud' and d['data'][0]['registeredRoles'] == ['control-converged','compute'] and d['data'][0]['additional']['v1ApiDocUrl'] == 'https://192.0.2.10/api/v1/datacenters/fake-dc/apidocs/index.html' and d['data'][0]['isLocal'] is True and d['data'][0]['firmware']['version'] == d['data'][0]['version'] and d['data'][0]['additional']['nodeLicenseStatus'] == {'valid':0,'expired':0,'unlicense':3}"

req GET "$API"
check "GET /datacenters/{dc} 要驗證：未驗證回 401（SAML 導向網址在 msg）" code_is 401
check "401 外框沒有 data、msg 是 Keycloak SAML 網址" js "list(d) == ['code','msg','status'] and d['code'] == 401 and d['status'] == 'unauthorized' and d['msg'].startswith('https://192.0.2.10:10443/auth/realms/master/protocol/saml?SAMLRequest=')"
check "& 照 Go encoding/json 轉成 \\u0026、帶 saml_ 追蹤 cookie" js "'\\\\u0026RelayState=' in raw and 'Set-Cookie: saml_' in hdr and 'Path=/saml/acs' in hdr"
check "回應沒有 Server 標頭（同 Go net/http）" js "'server:' not in hdr.lower()"

req POST "$API/tokens" -H 'Content-Type: application/json' -d '{"name":"admin","password":"wrong"}'
check "帳密錯誤 → 401 + gocloak 錯誤字串" eval 'code_is 401 && js "d == {\"code\":401,\"msg\":\"401 Unauthorized: invalid_grant: Invalid user credentials\",\"status\":\"unauthorized\"}"'
req POST "$API/tokens" -d '{"name":"","password":"x"}'
check "name 空白 → 400 user name is empty" eval 'code_is 400 && js "d == {\"code\":400,\"msg\":\"user name is empty\",\"status\":\"bad request\"}"'
req POST "$API/tokens" -d '{"name":"admin"}'
check "password 空白 → 400 user password is empty" eval 'code_is 400 && js "d[\"msg\"] == \"user password is empty\""'
req POST "$API/tokens"
check "沒有內容 → 400 EOF" eval 'code_is 400 && js "d[\"msg\"] == \"EOF\""'
req POST "$API/tokens" -d '{'
check "壞掉的 JSON → 400" eval 'code_is 400 && js "d[\"status\"] == \"bad request\""'
req POST "$API/tokens" -d 'null'
check "JSON null → 500 沒有內容（nil 解參照 panic 被 gin.Recovery 接住）" eval 'code_is 500 && js "raw == \"\""'

req POST "$API/tokens" -H 'Content-Type: application/json' -d '{"name":"admin","password":"Fake-CubeCOS-Pass1"}'
check "帳密正確 → HTTP 201、內文 code 200（SetCreated）" eval 'code_is 201 && js "d[\"code\"] == 200 and d[\"status\"] == \"ok\" and d[\"msg\"] == \"create token successfully\""'
check "token 回應欄位 token,refresh,expires{access,refresh}" js "list(d['data']) == ['token','refresh','expires'] and d['data']['expires'] == {'access':7200,'refresh':1800} and d['data']['token'].count('.') == 2"
TOKEN=$(json_field "['data']['token']")
REFRESH=$(json_field "['data']['refresh']")
AUTH=(-H "Authorization: Bearer $TOKEN")

req GET "$API" "${AUTH[@]}"
check "GET /datacenters/{dc} 帶 Bearer → 200" eval 'code_is 200 && js "d[\"data\"][\"name\"] == \"fake-dc\" and d[\"msg\"] == \"fetch data center list successfully\""'

req GET "$API/nodes" "${AUTH[@]}"
check "GET /nodes → 200" eval 'code_is 200 && js "d[\"msg\"] == \"fetch nodes list successfully\""'
check "nodes 依 hostname 排序、page 外形" js "[n['hostname'] for n in d['data']['nodes']] == ['os-compute-1','os-compute-2','os-ctrl-1'] and d['data']['page'] == {'total':1,'number':1,'size':3,'totalItemCount':3} and list(d['data']) == ['nodes','page']"
check "Node 欄位與順序同 node.go json tag" js "all(list(n) == $NODE_KEYS for n in d['data']['nodes'])"
check "上線節點的 ip＝managementIP、address＝管理IP:8082、blockDevices 空" js "[ (n['ip'], n['managementIP'], n['storageIP'], n['address'], n['blockDevices'], n['protocol'], n['isVirtualIpOwner'], n['role'], n['labels']) for n in d['data']['nodes'] if n['hostname']=='os-ctrl-1'][0] == ('192.0.2.11','192.0.2.11','10.0.0.11','192.0.2.11:8082',[],'http',True,'control-converged',{'isGpuEnabled':'false'})"
check "整數值的 float 編成整數、授權為 unlicense" js "'\"totalCores\":64' in raw and d['data']['nodes'][0]['license']['status'] == {'current':'unlicense','isExpiring':False}"

req GET "$API/nodes?roles=compute" "${AUTH[@]}"
check "roles=compute 篩出 2 台（不含 control-converged）" js "[n['hostname'] for n in d['data']['nodes']] == ['os-compute-1','os-compute-2'] and d['data']['page']['totalItemCount'] == 2"
req GET "$API/nodes?roles=compute&roles=control-converged" "${AUTH[@]}"
check "roles 用重複的 key 傳多個" js "len(d['data']['nodes']) == 3"
req GET "$API/nodes?keyword=CTRL" "${AUTH[@]}"
check "keyword 不分大小寫" js "[n['hostname'] for n in d['data']['nodes']] == ['os-ctrl-1']"
req GET "$API/nodes?keyword=192.0.2.21" "${AUTH[@]}"
check "keyword 可搜 IP（去掉 . 後比對）" js "[n['hostname'] for n in d['data']['nodes']] == ['os-compute-1']"

req GET "$API/nodes?pageNum=1&pageSize=2" "${AUTH[@]}"
check "分頁 pageNum=1&pageSize=2 → 2 筆、page 資訊" js "len(d['data']['nodes']) == 2 and d['data']['page'] == {'total':2,'number':1,'size':2,'totalItemCount':3}"
req GET "$API/nodes?pageNum=2&pageSize=2" "${AUTH[@]}"
check "第 2 頁 → 1 筆" js "len(d['data']['nodes']) == 1 and d['data']['page']['number'] == 2"
req GET "$API/nodes?pageNum=9&pageSize=2" "${AUTH[@]}"
check "超出範圍的頁 → 空陣列" js "d['data']['nodes'] == [] and d['data']['page']['total'] == 2"
req GET "$API/nodes?pageNum=1" "${AUTH[@]}"
check "只給 pageNum → 400" eval 'code_is 400 && js "d == {\"code\":400,\"msg\":\"pageSize should be provided if pageNum is provided\",\"status\":\"bad request\"}"'
req GET "$API/nodes?pageSize=1" "${AUTH[@]}"
check "只給 pageSize → 400" eval 'code_is 400 && js "d[\"msg\"] == \"pageNum should be provided if pageSize is provided\""'
req GET "$API/nodes?pageNum=0&pageSize=1" "${AUTH[@]}"
check "pageNum=0 → 400" eval 'code_is 400 && js "d[\"msg\"] == \"pageNum should be greater than 0 if pageSize is provided\""'
req GET "$API/nodes?pageNum=a&pageSize=1" "${AUTH[@]}"
check "pageNum 非整數 → 400" eval 'code_is 400 && js "d[\"msg\"] == \"pageNum should be an integer: a\""'
req GET "$API/nodes?watch=maybe" "${AUTH[@]}"
check "watch 不是布林 → 400" eval 'code_is 400 && js "d[\"msg\"].startswith(\"watch parameter is invalid\")"'

req GET "$API/nodes/os-compute-1" "${AUTH[@]}"
check "GET /nodes/{name} → 200、data 是單一 Node" eval 'code_is 200 && js "d[\"data\"][\"hostname\"] == \"os-compute-1\" and d[\"msg\"] == \"fetch node successfully\" and list(d[\"data\"]) == '"$NODE_KEYS"'"'
req GET "$API/nodes/nope" "${AUTH[@]}"
check "找不到節點 → 500（原始碼不是 404）" eval 'code_is 500 && js "d == {\"code\":500,\"msg\":\"node nope not found\",\"status\":\"internal server error\"}"'

req GET "$A/api/v1/datacenters/anything-goes/nodes" "${AUTH[@]}"
check "{dc} 不檢查：任意名稱也回 200" code_is 200

req GET "$API/nodes" -H 'X-Auth-Token: ks-static-token'
check "X-Auth-Token（ACCEPT_KEYSTONE_TOKENS）→ 200" code_is 200
req GET "$API/nodes" -H 'X-Auth-Token: not-a-token'
check "錯的 X-Auth-Token → 401 SAML" eval 'code_is 401 && js "d[\"msg\"].startswith(\"https://192.0.2.10:10443/\")"'
req GET "$API/nodes" -H "Authorization: Bearer ${TOKEN}x"
check "竄改過的 Bearer → 401" code_is 401
req GET "$API/nodes" -H "Authorization: bearer $TOKEN"
check "小寫 bearer → 401（前綴大小寫要完全相同）" code_is 401
req GET "$API/nodes" -H "Authorization: Bearer $REFRESH"
check "refresh token 不能當 access token → 401" code_is 401

req GET "$A/api/v1/nothing-here" "${AUTH[@]}"
check "不存在的路徑（已驗證）→ 404 純文字" eval 'code_is 404 && js "raw == \"404 page not found\""'
req GET "$A/api/v1/nothing-here"
check "不存在的路徑（未驗證）→ 401 SAML（gin 中介層套用在 404）" code_is 401
req GET "$A/api/v1/datacenters/"
check "結尾斜線 → 301 到沒有斜線的路徑（gin RedirectTrailingSlash）" eval 'code_is 301 && js "\"Location: /api/v1/datacenters\" in hdr.splitlines()"'
req GET "$A/saml/acs"
check "GET /saml/acs → 403（filterReq）" code_is 403
req POST "$A/saml/acs"
check "POST /saml/acs 沒有 assertion → 302 /home" eval 'code_is 302 && js "\"Location: /home\" in hdr.splitlines()"'

req GET "$A/_test/write_calls"
check "讀取路由都不算寫入：write_calls＝0" js "d['count'] == 0"
req POST "$API/nodes/os-compute-1/softReboot" "${AUTH[@]}"
check "softReboot 樁 → 202 accepted 外形" eval 'code_is 202 && js "d == {\"code\":202,\"msg\":\"the request of rebooting node is accepted and under processing\",\"status\":\"accepted\"}"'
req POST "$API/nodes/drain"
check "drain（未驗證）→ 401，但仍記為寫入嘗試" code_is 401
req POST "$A/api/v1/datacenters/fake-dc/rollingReboot" "${AUTH[@]}"
check "rollingReboot 樁 → 202" eval 'code_is 202 && js "d[\"msg\"] == \"rollout data center by soft reboot successfully\""'
req PATCH "$API/nodes/os-ctrl-1/devices/tasks" "${AUTH[@]}" -d '{}'
check "PATCH devices/tasks（靜態段優先於 :device）→ 200" eval 'code_is 200 && js "d == {\"code\":200,\"msg\":\"update node device task successfully\",\"status\":\"ok\"}"'
req DELETE "$API/nodes/os-ctrl-1/osds/3" "${AUTH[@]}"
check "DELETE osds/{id} → 202" code_is 202
req POST "$API/nodes/os-ctrl-1/ipmi/verify" "${AUTH[@]}" -d '{}'
check "ipmi/verify → 200 帶 board/product" eval 'code_is 200 && js "list(d[\"data\"]) == [\"board\",\"product\"]"'
req PUT "$A/api/v1/unknown-write" "${AUTH[@]}"
check "不認得的非 GET（已驗證）→ 404，也記為寫入嘗試" code_is 404
req GET "$A/_test/write_calls"
check "write_calls 記到 7 筆、含路由與驗證狀態" js "d['count'] == 7 and [c['route'] for c in d['calls']][:2] == ['/api/v1/datacenters/:DataCenter/nodes/:nodeName/softReboot','/api/v1/datacenters/:DataCenter/nodes/drain'] and d['calls'][1]['authenticated'] is False"
req GET "$A/_test/requests"
check "/_test/requests 有請求記錄、不含 token" js "d['count'] > 10 and '$TOKEN' not in raw"
req DELETE "$A/_test/write_calls"
req GET "$A/_test/write_calls"
check "DELETE /_test/write_calls 歸零" js "d['count'] == 0"
req GET "$A/_test/ca.pem"
check "/_test/ca.pem 取得測試 CA" js "raw.startswith('-----BEGIN CERTIFICATE-----')"
check "Python ssl 預設（嚴格）驗證通過" python3 -c "
import ssl, urllib.request
ctx = ssl.create_default_context(cafile='$WORK/tlsA/ca.pem')
urllib.request.urlopen('$A/live', context=ctx).read()"
if grep -q "$TOKEN" "$WORK/a.log"; then check "伺服器記錄沒有印出 token" false; else check "伺服器記錄沒有印出 token" true; fi

# =========================================================================
# 實例 B：HTTP、KEYSTONE_URL（假 Keystone）、redirect302、路徑前綴、自訂主機名稱
# =========================================================================
PK=$(free_port)
cat >"$WORK/keystone.py" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG = sys.argv[2]
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        with open(LOG, "a") as fh:
            fh.write(f"{self.path} subject={self.headers.get('X-Subject-Token')} auth={self.headers.get('X-Auth-Token')}\n")
        ok = self.path == "/v3/auth/tokens" and self.headers.get("X-Subject-Token") == "ks-live" \
            and self.headers.get("X-Auth-Token")
        body = b'{"token":{}}' if ok else b'{"error":{"code":404}}'
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
python3 "$WORK/keystone.py" "$PK" "$WORK/keystone.log" >/dev/null 2>&1 &
PIDS+=($!)

PB=$(free_port)
KEYSTONE_URL="http://127.0.0.1:$PK/v3" CUBECOS_UNAUTH_MODE=redirect302 CUBECOS_PATH_PREFIX=/cos-api \
CUBECOS_NODE_HOSTNAMES="ctl-a,cmp-a,cmp-b" python3 "$FAKE" --http --port "$PB" >"$WORK/b.log" 2>&1 &
PIDS+=($!)
TLS=()
B="http://127.0.0.1:$PB"
for _ in $(seq 1 100); do curl -s -o /dev/null "$B/cos-api/live" && break; sleep 0.1; done
echo "# 實例 B：$B/cos-api"

req GET "$B/cos-api/api/v1/datacenters/x/nodes" -H 'X-Auth-Token: ks-live'
check "KEYSTONE_URL 驗證通過 → 200、主機名稱被環境變數覆蓋" eval 'code_is 200 && js "[n[\"hostname\"] for n in d[\"data\"][\"nodes\"]] == [\"cmp-a\",\"cmp-b\",\"ctl-a\"]"'
check "Keystone 收到 GET /v3/auth/tokens，X-Subject-Token＝被驗的 token" grep -q '^/v3/auth/tokens subject=ks-live auth=ks-live$' "$WORK/keystone.log"
req GET "$B/cos-api/api/v1/datacenters/x/nodes" -H 'X-Auth-Token: ks-dead'
check "Keystone 拒絕 → redirect302 模式回 302 到 SAML IdP" eval 'code_is 302 && js "\"Location: https://192.0.2.10:10443/auth/realms/master/protocol/saml?SAMLRequest=\" in hdr"'
req GET "$B/api/v1/datacenters"
check "沒有路徑前綴 → 404" code_is 404
req GET "$B/cos-api/api/v1/datacenters"
check "有路徑前綴的 /datacenters → 200" code_is 200

# =========================================================================
# 實例 C：HTTP、html200、種子順序、token 1 秒到期、一台離線節點
# =========================================================================
python3 - "$HERE/seed.example.json" "$WORK/seed-c.json" <<'PY'
import json, sys
seed = json.load(open(sys.argv[1], encoding="utf-8"))
seed["nodes"][2]["status"] = "down"
json.dump(seed, open(sys.argv[2], "w", encoding="utf-8"))
PY
PC=$(free_port)
CUBECOS_UNAUTH_MODE=html200 CUBECOS_NODE_ORDER=seed CUBECOS_ACCESS_TTL=1 \
    python3 "$FAKE" --http --port "$PC" --seed "$WORK/seed-c.json" >"$WORK/c.log" 2>&1 &
PIDS+=($!)
C="http://127.0.0.1:$PC"
for _ in $(seq 1 100); do curl -s -o /dev/null "$C/live" && break; sleep 0.1; done
echo "# 實例 C：$C"
CAPI="$C/api/v1/datacenters/fake-dc"

req POST "$CAPI/tokens" -d '{"name":"admin","password":"Fake-CubeCOS-Pass1"}'
check "CUBECOS_ACCESS_TTL=1 → expires.access＝1" js "d['data']['expires']['access'] == 1"
CTOKEN=$(json_field "['data']['token']")
req GET "$CAPI/nodes?pageNum=1&pageSize=2" -H "Authorization: Bearer $CTOKEN"
check "先分頁再排序：種子順序第 1 頁是 ctrl＋compute-1，頁內再依 hostname 排" js "[n['hostname'] for n in d['data']['nodes']] == ['os-compute-1','os-ctrl-1']"
req GET "$CAPI/nodes/os-compute-2" -H "Authorization: Bearer $CTOKEN"
check "離線節點：ip＝provider IP、address 空、沒有 protocol／labels" js "(d['data']['status'], d['data']['ip'], d['data']['managementIP'], d['data']['address'], d['data']['id'], 'protocol' in d['data'], 'labels' in d['data'], d['data']['firmware']) == ('down','198.51.100.22','192.0.2.22','','',False,False,{})"
sleep 2
req GET "$CAPI/nodes" -H "Authorization: Bearer $CTOKEN"
check "token 到期 → html200 模式回 200 HTML 登入頁" eval 'code_is 200 && js "d is None and \"kc-form-login\" in raw and \"text/html\" in hdr"'

echo
echo "通過 $PASS，失敗 $FAIL"
[ "$FAIL" -eq 0 ]
