# CubeCOS API 測試靶（假的 Bigstack CubeCOS API）

給 jt-ipam 的 OpenStack 整合（Bigstack CubeCOS 延伸）做測試用的假伺服器。單一 Python 檔，只用標準函式庫
（Python 3.11 以上），不需要 pip，也不需要 Docker。

欄位名稱、狀態碼、回應外框與行為都照 cube-cos-api v3.1.0（commit `e9d4cd2`）的 Go 原始碼，程式碼註解裡每一項都標了
`檔案:行號`。原始碼沒有、只為了測試方便的行為，註解一律寫「非原始碼行為」。

## 安全

- 預設只綁 `127.0.0.1:18443`。
- 憑證是啟動時用 `openssl` 現產的測試 CA 與伺服器憑證，有效 30 天，放在暫存目錄（或 `--cert-dir`）。
- 預設帳號 `admin`，密碼 `Fake-CubeCOS-Pass1`，寫在 `seed.example.json` 裡。所有位址都是 RFC 5737 和 RFC 1918
  的測試位址，不含任何客戶資料。
- 不要拿正式的 CubeCOS 當這支程式的替代品，也不要把它開在對外網路上。

## 用法

```bash
python3 scripts/cubecos-test-target/fake_cubecos.py                      # HTTPS，127.0.0.1:18443，內建種子
python3 scripts/cubecos-test-target/fake_cubecos.py --seed my-seed.json --listen 192.0.2.50 --port 18443
python3 scripts/cubecos-test-target/fake_cubecos.py --http               # 純 HTTP
scripts/cubecos-test-target/selftest.sh                                  # 自我測試（啟動三個實例，跑完自己收掉）
```

啟動時會印出測試 CA 的路徑，也可以 `GET /_test/ca.pem` 取得，貼進 jt-ipam 的 CA 憑證欄位就能開著 TLS 驗證測。

### 參數與環境變數

| 參數 / 環境變數 | 預設 | 說明 |
| --- | --- | --- |
| `--seed` / `CUBECOS_SEED` | 內建（同 `seed.example.json`） | 種子 JSON：資料中心、帳號、token 有效時間、節點 |
| `--listen` / `CUBECOS_LISTEN` | `127.0.0.1` | 監聽位址 |
| `--port` / `CUBECOS_PORT` | `18443` | 監聽埠 |
| `--http` / `CUBECOS_HTTP=1` | 關 | 改用純 HTTP |
| `--cert-dir` / `CUBECOS_CERT_DIR` | 暫存目錄 | 憑證輸出目錄（`ca.pem`、`server.pem`、`server.key`） |
| `CUBECOS_TLS_SANS` | 無 | 伺服器憑證額外的 SAN，逗號分隔 |
| `CUBECOS_NODE_HOSTNAMES` | 無 | 依序覆蓋種子節點的主機名稱，例如 `os-ctrl-1,os-compute-1,os-compute-2` |
| `CUBECOS_ACCESS_TTL` | `7200` | access token 有效秒數（測重新登入時設小一點） |
| `CUBECOS_UNAUTH_MODE` | `saml401` | 未驗證時的回應：`saml401`（原始碼行為）、`redirect302`、`html200`（後兩者非原始碼行為） |
| `CUBECOS_NODE_ORDER` | `shuffle` | 分頁前的節點順序：`shuffle`（同原始碼，每次不同）、`seed`（照種子順序） |
| `CUBECOS_PATH_PREFIX` | 無 | 路徑前綴，例如 `/cos-api`（模擬前面有 nginx 轉送） |
| `KEYSTONE_URL` | 無 | 設了就把 `X-Auth-Token` 拿去 Keystone 驗，例如 `https://keystone:5000/v3` |
| `KEYSTONE_SERVICE_TOKEN` | 無 | 驗證時當 `X-Auth-Token` 的服務 token；沒設就用被驗的 token 自己 |
| `KEYSTONE_INSECURE=1`、`KEYSTONE_CA` | 關、無 | 連 Keystone 時略過 TLS 驗證，或指定 CA 檔 |
| `ACCEPT_KEYSTONE_TOKENS` | 無 | 沒設 `KEYSTONE_URL` 時，直接接受這些 `X-Auth-Token`（逗號分隔，單元測試用） |

## 端點

| 方法與路徑 | 驗證 | 結果 | 原始碼 |
| --- | --- | --- | --- |
| 任何方法 `/live` | 不用 | 200 `api is alive` | `internal/runtime/router.go:91,354-358` |
| `GET /api/v1/datacenters` | 不用 | 200，`data` 是只有一筆的陣列 | `internal/runtime/filter.go:30-32`、`handlers/datacenters/handlers.go:41-49` |
| `GET /api/v1/datacenters/{dc}` | 要 | 200，`data` 是單一資料中心 | `handlers/datacenters/handlers.go:51-57` |
| `POST /api/v1/datacenters/{dc}/tokens` | 不用 | 201，內文 `code` 是 200 | `handlers/tokens/handlers.go:25-52`、`bodies/resp.go:114-125` |
| `GET /api/v1/datacenters/{dc}/nodes` | 要 | 200，`data` 是 `{nodes, page}` | `handlers/nodes/handlers.go:149-174`、`helper.go:94-103` |
| `GET /api/v1/datacenters/{dc}/nodes/{nodeName}` | 要 | 200；找不到是 **500** `node X not found` | `handlers/nodes/handlers.go:176-201`、`internal/cubecos/nodes.go:161-172` |
| `GET /saml/acs` | 不用 | 403，沒有內容 | `internal/runtime/filter.go:14-21` |
| 其他 `/saml/*` | 不用 | 302 到 `/home` | `internal/auths/saml/saml.go:185-215` |

回應外框是 `{"code","data","msg","status"}`（gin.H 是 map，Go 會把鍵依字母排序），錯誤沒有 `data`。
`{dc}` 不檢查，填什麼都行。

### 寫入路由（樁）

`rollingReboot`、`softReboot`、`ipmi`（設定、verify、poweron/poweroff/powercycle、disconnect）、`nodes/drain`、
`gpuCards` PUT、`devices` 新增/刪除/修改/tasks、`osds` restart/刪除/修改/tasks 都有，回原始碼的成功外形
（202 accepted 或 200 ok），但**只是記錄，什麼都不做**。jt-ipam 絕對不該呼叫它們：

- 每次呼叫都會在 stderr 印出 `!!!!! 寫入嘗試`。
- 沒驗證的呼叫、打到不存在路徑的非 GET 請求也一樣會記。
- `GET /_test/write_calls` 回 `{"count": n, "calls": [...]}`；`DELETE /_test/write_calls` 歸零。
- `GET /_test/requests` 回所有請求的記錄（方法、路徑、驗證方式、狀態碼，不含 token）。

`/_test/*` 只存在於測試靶，不需要驗證，也不算寫入。

## 驗證

照 `verifyAuthToken`（`internal/runtime/router.go:373-410`）的順序：

1. `X-Auth-Token`：Keystone token。原始碼用 gophercloud `tokens.Get` 打 `GET {identity}/auth/tokens`，
   `X-Subject-Token` 帶被驗的 token，200 或 203 算有效；每個請求都驗一次，沒有快取。
2. `Authorization: Bearer <token>`：`POST .../tokens` 發的 token，前綴大小寫要完全相同。到期、被竄改、拿 refresh
   token 來用都不算有效。
3. 都不過：`conditionalSaml` 呼叫 `saml.AuthRequest`，結果是 **HTTP 401**、JSON 外框，`msg` 是 Keycloak 的 SAML
   導向網址（`https://<VIP>:10443/auth/realms/master/protocol/saml?SAMLRequest=...`），外加 `saml_<RelayState>`
   cookie（`internal/auths/saml/saml.go:239-262`）。v3.1.0 原始碼**不會**回 302。不存在的路徑沒驗證時也是這個 401。

帳密錯誤是 401 `401 Unauthorized: invalid_grant: Invalid user credentials`（Keycloak 透過 gocloak 回的錯誤字串）；
`name` 或 `password` 空白是 400 `user name is empty`、`user password is empty`。

## 接到開發用的 jt-ipam

- jt-ipam 的出站規則一律擋迴路位址。用 `--listen` 綁在開發機的私網位址（記得 `CUBECOS_TLS_SANS` 或讓程式自動把
  監聽位址加進 SAN），或另外設定 `OUTBOUND_ALLOW_CIDRS`。
- `cubecos_url` 填 `https://<監聽位址>:18443`，CA 憑證貼 `/_test/ca.pem` 的內容。
- 主機名稱要對上 OpenStack 測試靶 Nova 的 compute host 時，改種子或設 `CUBECOS_NODE_HOSTNAMES`。
- 測完看 `/_test/write_calls`，`count` 必須是 0。

## 原始碼裡要注意的地方

- 上線節點的 `ip` 是**管理網路 IP**（MDNS 註冊資料的 `ip`，`internal/runtime/parse.go:159`），跟 `managementIP`
  一樣；只有離線節點的 `ip` 才是 provider IP（`internal/cubecos/nodes.go:96-104`）。API 沒有穩定的 provider IP 欄位。
- `nodes` 清單是「先篩選、再分頁、最後只在這一頁裡排序」（`handlers/nodes/helper.go:95-98`），分頁前的順序來自 Go map
  走訪，每次不同。所以要分頁的話，跨頁可能重複或漏掉；一次拿全部（不帶 `pageNum`、`pageSize`）才可靠。
- 找不到節點回 500，不是 404。
- `blockDevices` 在清單和單一節點裡永遠是空陣列。

## 跟真的 CubeCOS 不同的地方

- token 是用啟動時隨機金鑰簽的 HS256 JWT，外形同 Keycloak 的 JWT，但只有這支程式驗得過。refresh token 沒有對應的
  更新端點（原始碼也沒有）。
- node 之間的內部 token（`Node` 標頭）需要 OIDC client secret，不模擬。
- `watch=true`（SSE）只檢查參數格式，照一般 JSON 回。
- `keyword` 用子字串比對近似 bleve 的萬用字元查詢。
- SAML 導向網址裡的 `SAMLRequest`、`Signature` 只求格式相同，內容是假的。
- 壞掉的 JSON 錯誤字串只求外形近似 json-iterator。
- `redirect302`、`html200` 兩種未驗證回應原始碼沒有，是模擬前面有代理改寫的情況，拿來測 jt-ipam 的容錯。
