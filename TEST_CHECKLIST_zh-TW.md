# jt-ipam 升版測試清單

> 英文版見 [TEST_CHECKLIST.md](TEST_CHECKLIST.md)。

> 規矩：**每次 bump `frontend/package.json` 的 version 之前，先把這份清單跑過一輪，全綠才升版。**
> CI 目前沒跑驗證，所以靠這份手動把關。紅的先修，不要帶病升版。

升版流程：跑清單 → 全綠 → 改 version → 部署（backend rsync + alembic + restart；frontend build）。

---

## 1. 靜態檢查（dev 機，免 DB，最快）

- [ ] 後端可被 import：`cd backend && set -a; source <env>; set +a; .venv/bin/python -c "import app.main"`
- [ ] 後端 pytest 收集無 error（DB 測試會 skip）：`.venv/bin/pytest -q`
- [ ] 前端型別：`cd frontend && npx vue-tsc --noEmit`（必須零錯誤）
- [ ] 前端 build：`npm run build`（成功產生 dist）
- [ ] i18n：這次新增的 key 在 `zh-TW.json` 與 `en-US.json` 都有；無寫死中文漏網

## 2. 資料庫 / Migration（用拋棄式 test DB，勿碰正式資料）

- [ ] 全新 DB 從 0001 升到 head 無誤：對 `jt_ipam_test` 跑 `alembic upgrade head`
- [ ] 這次新增的 migration 有 `downgrade()` 且能 `alembic downgrade -1` 再 `upgrade head` 來回一次
- [ ] 沒有「model 改了但忘了 migration」：升完 head 後 app 啟動不報 asyncpg「column does not exist」
- [ ] **約束變更**：migration 若移除或新增 UNIQUE 約束，必須逐一檢查依賴該約束的查詢。
  對可能重複的欄位用 `scalar_one_or_none()`，只要出現第二筆就是 500（v0.5.194 的
  `users.email`）；無條件寫入該欄位的程式也會開始撞 IntegrityError

## 3. 後端整合測試（test DB + pytest，全面）

- [ ] 設 `JTIPAM_TEST_DATABASE_URL` 後 `.venv/bin/pytest -q` 全綠（e2e CRUD / auth / 各模組）
- [ ] 認證：登入、refresh、TOTP、權限（require_admin 的端點未授權回 401/403）
- [ ] 核心 CRUD：sections / subnets / addresses / devices / customers / locations / racks
- [ ] 稽核鏈：寫入操作有 audit、鏈完整性驗證過

## 3b. 認證領域與帳號識別

登入橫跨本機 / LDAP / RADIUS / OIDC / SAML，而同一個人本來就可能在多個領域各有帳號。
這一區的缺陷傳到使用者手上都長成「我登不進去」，真正的原因藏在 traceback 裡。

- [ ] **每一個已啟用的領域**都實際登入一次；密碼錯誤回 401 且訊息一致（不可用來窮舉帳號），
  真正原因只寫在伺服器日誌
- [ ] **同一個人、兩個領域**：共用同一 email 的本機帳號與 LDAP／SSO 帳號都能各自登入，
  且不會覆蓋彼此的資料（v0.5.194：共用 email 撞上 UNIQUE 索引，在 LDAP 驗證**已經通過之後**才回 500）
- [ ] **自動建立帳號**：第一次外部登入建立帳號、第二次更新；任何唯一欄位衝突都要優雅退讓，
  不可讓整個登入失敗
- [ ] 連續失敗後鎖定、解鎖可用；已停用的帳號被拒絕
- [ ] 以 email（而非帳號）登入時，每個領域都只會對應到一個帳號

## 4. 關鍵 API smoke（部署後對 prod 打，唯讀為主）

- [ ] `GET /api/v1/health`（或 `/notifications`）200
- [ ] `GET /api/v1/subnets`、`/addresses`、`/devices`、`/locations`、`/racks` 200
- [ ] 這次動到的端點：手動打一次成功路徑 + 一個失敗路徑（驗證 4xx 正確）

## 5. OWASP Top 10:2025 逐項自我檢核（這次動到的模組）

- [ ] A01 權限：新端點有沒有正確 require_admin / 物件層級授權？
- [ ] A03 注入 / 輸入驗證：Pydantic StrictModel、檔案上傳驗 magic bytes + 限大小 + 禁危險類型（如 SVG）
- [ ] A08 完整性：上傳/外部資料有驗證；路徑無 traversal（上傳/下載檔案路徑解析後仍在白名單目錄內）
- [ ] 機密：無把 secret/token 寫進 log 或回應

## 5b. 部署腳本流程（拋棄式環境，**勿在 dev/prod 跑 install**）

- [ ] **全新安裝**：乾淨 LXC/VM 跑 `scripts/install-debian.sh`，裝完服務起得來、能登入
- [ ] **舊版升級**：對上一版的環境跑 `scripts/jt-ipam-upgrade.sh`，升完正常、必要時可回滾
- [ ] 這次若新增了目錄 / 套件 / 服務 / DB extension / env，確認**兩支腳本都已同步**
- [ ] **(A) 預設管理員帳密**：全新安裝結尾有印出 `admin` 帳號＋隨機密碼，且密碼存到 `/etc/jt-ipam/.admin-initial-password`（root 0600）；用該密碼能登入
- [ ] **(A) 重置密碼 CLI**：`python -m app.cli.bootstrap create-admin --username admin --password-stdin --force-update` 能重置既有 admin；README 中英都有此段
- [ ] **(B) 代理探測工具**：`agent/jt-ipam-agent-installer.sh` 裝完，主機上有 `nmap` / `nmblookup`(samba-common-bin) / `avahi-resolve`(avahi-utils)；代理 `available_probes` 回報含 os/netbios/mdns
- [ ] **(B) 安裝說明 UI**：掃描代理頁與子網路編輯對話框中，不可勾的探測旁有「安裝說明」彈出，內容顯示對應安裝指令

## 5c. headless 瀏覽器 smoke 測試

- [ ] `cd frontend && pnpm exec playwright test smoke`（免後端，自起 vite preview）全綠
- [ ] 對已部署實例（給 `E2E_BASE_URL` + `E2E_ADMIN_PASS`）跑 `pnpm test:e2e` 主路徑（登入/sections/audit）

## 5e. AI 對話 / MCP 工具 —— **每次動到工具、提示詞或它們讀的資料都要跑**

錯誤的 AI 答案看起來不像錯的：裡面每個數字都是真的，只是算在錯的集合上。單元測試會過，
因為每支工具都確實回了「被問到的東西」——缺陷在於**模型能問到什麼**。

- [ ] **範圍**：每支回傳逐物件資料的工具，都用指名單一子網路／機櫃／機房的問題問一次，
  確認答案只含該範圍。要防的回歸：「198.51.100.0/24 裡哪些主機沒裝 Wazuh 代理」被用全站資料
  回答，因為那支工具根本沒有子網路參數（v0.5.194）
- [ ] **schema 要露出範圍參數**：工具說明明確要求「問題指定範圍就必須帶」，回傳含 `scope`
  讓答案能說明涵蓋範圍
- [ ] **不可靜默截斷**：每支清單工具都要同時回 `count`（範圍內總數）與 `returned`；
  問一個結果超過 `limit` 的問題，確認答案講明這是部分清單，而不是把一頁當成全部
- [ ] **權限分層**：新增／異動的工具要落在正確層級（異動／管理／全域讀取／逐物件），
  且 `allowed_tool_names()` 會對不能呼叫的帳號隱藏它。要用受限帳號**實際走 AI 對話**驗證，
  不能只看單元測試
- [ ] **唯讀就要真的唯讀**：判讀／巡檢類工具不寫入、不發通知、不 commit
- [ ] **提示詞注入**：攻擊者可控的文字（mDNS 主機名稱、防火牆規則描述）仍被定界與截長，
  對抗式測試仍然通過
- [ ] **事實來自工具，不是心算**：使用率／剩餘／筆數一律呼叫工具取得，不可讓模型自己用 CIDR 推算

## 6. 主要頁面手動點檢（部署後瀏覽器）

- [ ] 登入 / 登出 / 主題切換（淺/深/自動）
- [ ] 子網路：列表、樹狀、IP 清單（含閒置區間列跨欄位）、編輯
- [ ] 裝置 / 機櫃：排序（IP 自然序）、操作鈕高度一致、機房平面圖上傳+拖拉定位+點選
- [ ] 拓樸圖：節點/連線、VPN 對接連線、圖例
- [ ] 掃描代理 / 同步作業：頁面正常、無 console error

---

### 附：拋棄式 test DB 指令（在 prod 主機，**不碰正式 DB**）

```bash
set -a; source /etc/jt-ipam/backend.env; set +a
sudo -u postgres psql -c "DROP DATABASE IF EXISTS jt_ipam_test;"
sudo -u postgres psql -c "CREATE DATABASE jt_ipam_test OWNER ${POSTGRES_USER} ENCODING UTF8 TEMPLATE template0;"
sudo -u postgres psql -d jt_ipam_test -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
cd /opt/jt-ipam/backend
POSTGRES_DB=jt_ipam_test .venv/bin/alembic upgrade head
JTIPAM_TEST_DATABASE_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/jt_ipam_test" .venv/bin/pytest -q
sudo -u postgres psql -c "DROP DATABASE IF EXISTS jt_ipam_test;"
```

## 7c. 整合同步的韌性 —— **每個整合都適用，不只這次動到的那個**

實機本來就是「部分可讀」。防火牆回報「10 個端點中有 9 個可讀取」是常態而非異常：
韌體版本有差異，唯讀 API 帳號也很少能讀到每一項資源。絕對不可以發生的是**一支端點
讀不到就把其餘同步一起帶走**（v0.5.195：DHCP 租約路徑讀不到，導致 ARP、政策、NAT 與
位址物件通通不同步，而畫面上只有一行錯誤）。

- [ ] **區段隔離**：故意讓一支端點失敗（改成錯的路徑，或收掉那一項權限），確認其餘區段照常同步
- [ ] **部分失敗看得見**：實例會把失敗內容寫進 `last_error`；有失敗的那一輪絕不可以對使用者顯示成完全成功
- [ ] **不可跨實例連鎖中止**：單一實例失敗不能讓整輪同步停掉（寫 `last_error` 前要先 `session.rollback()`，
  否則下一次寫入會二次爆炸）
- [ ] **錯誤訊息要帶證據**：「回應不是 JSON」這種訊息在現場毫無用處。要附狀態碼、`content-type`
  與回應開頭約 120 字，並指出最可能的原因（例如裝置回的是網頁介面 → 該韌體沒有這支端點，
  或 API 帳號讀不到）
- [ ] **測試連線要反映真實**：逐端點診斷顯示的結果必須與同步實際拿到的一致 ——
  不可以對同步讀不到的東西打綠勾

## 7d. 從掃描代理執行探測 —— **只要動到工作佇列或代理就要跑**

讓伺服器把工作交給代理，等於讓那支代理可以應要求在客戶網路裡發送探測封包。
這個功能的安全性等於它最寬鬆的那道檢查。

- [ ] **種類白名單**：ping / tcp / traceroute / rdns 以外一律拒絕 —— 後端要擋，
  **代理也要自己獨立擋**（後端被入侵時不得因此擴大範圍）
- [ ] **目標驗證**：shell 特殊字元、命令替換、參數注入（`-oProxyCommand=…`）都要拒絕；
  參數一律以陣列傳給子行程，永遠不經過 shell
- [ ] **上限有效**：目標數、埠數、每代理待辦數，以及次數／逾時的夾限
- [ ] **歸屬**：代理只能結束自己領到的工作
- [ ] **過期**：把代理停掉後，排隊中的工作要過期作廢而不是等代理回來才補跑 ——
  遲到幾分鐘的探測結果比沒有結果更糟
- [ ] **真實代理往返**：建立 → 領取 → 執行 → 回報 → 取回結果，且畫面要標明是哪個代理跑的

## 7e. 稽核鏈的錨定 —— **只要動到稽核寫入、錨定或同步排程就要跑**

這一段要驗的是「鏈本身抓不到的那件事」。只驗鏈是不夠的。

- [ ] **尾端截斷**：錨定後刪掉最後幾筆 → 必須報 `anchored_row_missing`；
  同一情境下單獨跑 `verify_chain` 會回「完整」，這正是錨定存在的理由
- [ ] **內容竄改**：改被錨定那筆的雜湊 → `anchored_hash_changed`
- [ ] **總數變少**：刪中間任一筆 → `count_shrank` 或 `chain_broken`
- [ ] **增量**：第二次驗證要從上次錨定處接續，不是整條重走
- [ ] **錨定檔**：逐行附加（不是覆寫）、權限 0600、壞掉一行不影響讀取；
  同一份內容要進 journald（檔案被刪時仍留副本）
- [ ] **告警**：驗證失敗時所有管理員收到 severity=error 通知，且訊息指明是哪一種

## 7f. Zabbix 整合 —— **只要動到 Zabbix 同步或涵蓋缺口就要跑**

- [ ] **網址三種寫法**：`https://host`、`https://host/zabbix`、完整 `api_jsonrpc.php` 都要能連
- [ ] **兩種認證**：API token 與帳號密碼各測一次；Read 回應不得帶出任何機密
- [ ] **只標既有 IP、不新建**：Zabbix 有、IPAM 沒有的主機不可自動建 IP
- [ ] **限定範圍**：設了 `scope_subnet_ids` 後，重疊網段的同 IP 不會被標到別的單位；
  查詢要用 `limit(1)`（`scalar_one_or_none` 會炸掉整輪）
- [ ] **主機名稱收斂**：兩台 Zabbix 主機指向同一 IP 時不得每輪互相覆寫（看異動記錄不該洗版）
- [ ] **涵蓋缺口**：帶子網路範圍問就只回那些網段；空範圍回空而不是退化成全域
