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

## 5d. 系統匯出／匯入（跨機搬移）—— **只要動到它，每次發版都要整段跑**

- [ ] **單元（免 DB）**：`pytest tests/test_system_transfer.py -q` —— 加解密封裝（密語錯誤要回
  可讀訊息，不是 500）、四種機密表示法（欄位／集中／信封／設定 blob）都能來回、
  `registry.validate_registry()` 回空（每張表都分類過）、向下相容會丟掉不認得的欄位
- [ ] **含 DB**（`JTIPAM_TEST_DATABASE_URL` 指向 head）：匯出→匯入來回保留 UUID 與外鍵、
  機密在目標端的金鑰下解得開、`merge` 具冪等性（第二次全部 `updated`、不長出重複列）、
  `replace` 會先清空、`dry_run` 什麼都不寫
- [ ] **向下相容**：拿一份較舊／較少表的匯出檔匯入不會出錯；schema_version 不合只出警告不失敗
- [ ] **CLI**：`python -m app.cli.system_transfer export --scope … --out f.json --passphrase-stdin`
  → `import --file f.json --dry-run` → 實際 `import`；筆數正確，密語錯誤回非零
- [ ] **UI（管理 → 系統匯出／匯入）**：選範圍＋密語 → 產生 → 下載；在另一台上傳 → 分析
  （顯示來源版本、筆數、警告）→ 試跑預覽 → 套用（merge 與 replace 各一次）；非管理員 403／看不到選單
- [ ] **端到端搬移**：從 A 機匯出預設範圍，匯入乾淨的 B 機，然後在 B 登入確認子網路／IP／裝置／
  整合都在、某個整合真的連得上（機密已用新金鑰重新加密）、SSH 憑證可用、TOTP 仍可登入
- [ ] **安全**：下載／分析／套用都要 admin 且驗證作業歸屬；暫存檔 0600、目錄 0700；
  日誌與回應中不得出現明文機密或密語

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
- [ ] **可中止**：運算中「送出」變成「停止」，按下去會中止請求（連線一斷，LLM 伺服器也停止推論），
  畫面顯示已停止且回到可送出狀態
- [ ] **進度看得見**：連線中／模型思考中／執行哪個工具／整理資料／產生回答，各階段都有文字，
  並附第幾輪與已經過幾秒 —— **空轉的轉圈圈和當機長得一模一樣**
- [ ] **空回覆不可原樣送出**：模型沒產生文字時會再要求作答一次，仍為空則說明原因
  （長度上限／只輸出思考），不可顯示成「(沒有回應)」

## 5f. 瀏覽器主控台（SSH／BMC／PVE）—— **只要動到終端機就要跑**

- [ ] **網址可點**：長網址被 TUI 切成多列時（`printf '%s\n' "$URL" | fold -w $(tput cols)` 可重現），
  **滑鼠停在第二列**也認得整條網址；底部顯示的是完整目標
- [ ] **只開 http/https**、新分頁、不帶 opener（終端機文字由遠端主機控制）
- [ ] **選取複製**：跨列的網址複製出來是完整可用的；**一般多行文字必須原樣**（不可被改寫）
- [ ] **不誤接**：滿版的一行後面接另一段文字，不會被黏成一條假網址
- [ ] **SFTP 排序模式**：「資料夾優先」時，**升冪與降冪資料夾都在最前面**（把分組寫進比較函式
  會在降冪時翻掉，這是回歸重點）；「一起排」時只看排序欄位。依大小／修改時間排序也遵守同一模式。
  切換後存進使用者偏好，重新連線／換裝置仍記得
- [ ] 現成 spec：`frontend/e2e/terminal-links.spec.ts`（需 `E2E_SSH_ADDRESS_ID/USER/PASS`；
  另需該帳號 `can_ssh`、該 IP `ssh_enabled`，第一次連線要按「信任並連線」）

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

## 7. pfSense 整合（管理 → 整合 pfSense）

> pfSense（CE 2.8.x）端前置：安裝 **pfSense-pkg-RESTAPI**（pfrest.org），到 System → REST API →
> Settings 把 **「API Key」** 加進認證方式（預設只有 BasicAuth），再到 Keys 產一把金鑰。

- [ ] 新增實例：API URL ＋ X-API-Key，自簽憑證要**關掉驗證 TLS**；儲存（金鑰只進不出）
- [ ] **測試連線** → 成功並顯示 pfSense 版本
- [ ] **立即同步**（ARP＋別名＋規則開啟；若 LAN 的 DHCP 由別台負責則 **DHCP 關閉**）→ 回筆數；
  範圍內的 ARP IP 會被標上 `last_seen`（來源 `pfsense`）與 MAC；別名／規則筆數與實機相符
- [ ] **欄位名稱回歸**：ARP／DHCP 用的是 `ip_address`／`mac_address`（不是 `ip`／`mac`）；
  `hostname == "?"` 要視為空白
- [ ] **範圍安全**：設了 `scope_subnet_ids` 之後只會標到那些子網路裡的 IP（重疊網段用 `.limit(1)`）
- [ ] **規則／NAT 檢視**（眼睛按鈕）能列出同步到的規則與 NAT 筆數
- [ ] **Graylog DSV**（開啟 Expose DSV 並設好 token）：`GET /api/v1/lookup/pfsense/{id}/aliases?token=…`
  與 `…/rules?token=…` 回 CSV/TSV；**token 錯 → 401**；`expose_dsv` 關閉 → 404
- [ ] 刪除實例；`jt-ipam-sync` 每 ~5 分鐘會自己帶到已啟用的實例且不出錯

## 7b. VMware ESXi / vCenter 整合（管理 → 整合 VMware）—— **Beta**

> SOAP 端點固定是 `<url>/sdk`。同一套實作**同時**涵蓋單機 ESXi 與 vCenter —— 它們是同一組 VIM API，
> ContainerView 會吸收掉層級深度的差異。請用**唯讀**帳號：這個整合從不寫入。
> 免費／未授權的 ESXi 本來就只開放唯讀 API，剛好夠用。

- [ ] 新增實例：URL ＋ 帳號密碼，自簽憑證要**關掉驗證 TLS**；儲存（密碼只進不出）。
  編輯時密碼留空＝不變更
- [ ] **測試連線** → 逐步診斷：RetrieveServiceContent（產品與版本）、Login、
  RetrievePropertiesEx（VM 數）。密碼錯必須停在 **Login** 並顯示 VMware 自己的訊息，
  不可以是空泛的「伺服器錯誤」—— VMware 把認證失敗包成 HTTP 500 的 SOAP Fault
- [ ] **立即同步** → 回 VM 數；叢集清單看得到這個實例、型別 `vmware`；
  VM 帶名稱／電源狀態／vCPU／記憶體／所在主機
- [ ] **實機第一次跑要核對欄位**：拿幾台 VM 跟 vSphere 用戶端比對。關機的 VM 沒有 `guest.*`、
  沒裝 VMware Tools 的沒有 IP、範本沒有 `runtime.host` —— 這些都不可以讓同步中斷，應該只是回空
- [ ] **分頁**：VM 超過 200 台的 vCenter，筆數要與 vSphere 用戶端一致
  （continuation token 掉了會**安靜地**少掉後面全部）
- [ ] **IP 對應**：VMware Tools 回報且在範圍內的 IP 會連到既有位址；IPAM 沒有的位址**不建立**。
  重疊網段又沒設範圍時，有歧義的位址要跳過而不是用猜的
- [ ] **VM 被刪**：在 vSphere 刪掉一台 → 下一輪同步從清單移除
- [ ] **PVE 回歸（共用資料表）**：跑 ESXi 同步不可動到 Proxmox 的叢集／VM／介面，
  `legacy_vmid` 與 `kind=ct` 仍正確，進階 → 虛擬化（Proxmox VE）只列 PVE、虛擬化（VMware）只列 VMware；
  PVE VM 對到的裝置／IP 連結仍然有效
- [ ] **外部名稱過長（issue #25）**：VM 掛在名稱超過 64 字元的 NSX-T portgroup 上時，同步不會中斷，
  且網卡上顯示的是**完整名稱**而非截斷後的。ESXi 主機 FQDN 超過 128 字元寫進 `node` 亦同。
  第三方平台給的名稱長度，不是我們可以自己假設的。
- [ ] 刪除實例；`jt-ipam-sync` 每 ~5 分鐘會自己帶到已啟用的實例且不出錯

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

## 7g. 證據契約 —— **只要新增／修改任何「來源」就要跑**

這一節守的是：**新來源必須先回答「它的證據會不會過期」**。少了這道門的代價付過了 ——
ARP 被當成有時間概念的證據，讓一台關機數週的 VM 顯示 52 天全綠。

- [ ] **登記**：新來源在 `services/evidence.py` 宣告了 tier 與 aging；
  `pytest tests/test_evidence_contract.py` 綠（沒登記會被守門測試擋下）
- [ ] **分層正確**：被動學到的對應（ARP／FDB／DNS／DHCP／虛擬化設定）＝ `learned` 且
  `aging=False`；只有主動探測與第三方監控才可以是 `aging=True`
- [ ] **不可用字串比對判斷來源性質**：程式碼裡不該再出現 `"scanner" in status` 這類判斷，
  一律問 `evidence.is_aging()`（新來源才不會安靜地落進最寬鬆的分支）
- [ ] **上線判定**：管理 → 系統設定 → 上線判定，勾選項與預設值都由契約推導；
  不會過期的來源預設**不勾**
- [ ] **可用性長條圖**：只有 ARP 撐著的日子是灰色不是綠色；狀態往後延續時，
  那筆轉換宣稱的來源現在必須還在
- [ ] **優先序**：五個屬性（主機名稱／MAC／OS／裝置名稱／型號）改設定後即時生效、
  停用來源真的不參與；跑 `pytest -k "precedence or hostname or arp"` 全綠
- [ ] ⚠️ **快取**：優先序是模組級 60 秒快取。測試之間靠 `conftest` 的 `bust_all()` 清 ——
  快取若搬家，**確認那個 fixture 真的還清得到**（曾經安靜失效造成測試互相污染）

## 7h. IP 生命週期與冷卻期 —— **只要動到釋放、配發或冷卻設定就要跑**

- [ ] **釋放即冷卻**：刪除一個 IP 之後，`/addresses/cooldowns/{subnet_id}` 看得到它，
  且帶著前一手的主機名稱與 MAC
- [ ] **紀錄撐過刪除**：IP 記錄已經不在了，冷卻紀錄仍在（實務上「釋放」就是刪除）
- [ ] **配發跳過**：可用位址清單與自動配發都不會提供冷卻中的位址
- [ ] **手動建立被擋**：重建同一個位址回 409，訊息看得懂（含到期日與前一手），
  **不是** `[object Object]`
- [ ] **提前解除**：解除後可以配發，但紀錄仍在且留有解除者／時間／原因（不是刪掉）
- [ ] **停用**：天數設 0 → 行為與從前一致、不留紀錄
- [ ] **回收**：`jt-ipam-sync` 每輪會清掉早就過期的紀錄，但**到期後仍多留一段時間**
  （剛過期那幾天正是有人會問「上一手是誰」的時候）

## 7i. 事件規則 —— **只要動到規則、條件或事件分派就要跑**

- [ ] **條件不是運算式**：確認沒有任何形式的求值；正規表示式**不支援**（ReDoS）
- [ ] **認不得的運算子＝不放行**（放行才是危險的預設）
- [ ] **欄位路徑只走資料**：`data.x.y` 不可以變成屬性存取
- [ ] **AND 語意**：多個條件全部成立才命中；沒有條件＝只看事件名稱
- [ ] **壞規則不可拖垮其他規則**：形狀不對的規則被標記並跳過，其餘規則與原本的
  webhook 分派照常（**不可以安靜地什麼都不做**）
- [ ] **試跑沒有副作用**：試跑只回報命中與否與逐條結果，不送出通知也不打 webhook
- [ ] **webhook 動作走同一條路**：簽章與 SSRF 檢查不可被規則繞過

## 7j. 拓樸圖存取層（FDB）——**動到 FDB 推導或拓樸圖時**

> FDB 說的是「這個 MAC 出現在這台交換器的這個埠」。把它變成線有兩個古典陷阱，
> 而且兩個都會畫出一張「很有自信但是錯的」圖，不是一張明顯空白的圖。

- [ ] 埠上只有一個 MAC 的機器會出現存取層邊，並標出埠名。
- [ ] MAC 數超過門檻的埠（上行／trunk）**不產生**存取層邊 —— 後面的機器不會被畫成插在那個埠上。
- [ ] 埠上有好幾台已知機器時畫**虛線**（在此埠後面）而非實線；點該條線會顯示「直接連接：否」與此埠上的 MAC 數。
- [ ] 兩台交換器要互相看到對方、且兩個埠背後的 MAC 集合不重疊才連線。A—B—C 串接時**不可以出現 A—C**。
- [ ] 同一個 MAC 對到多台裝置（重疊網段）時完全不畫線。
- [ ] 取消勾選「存取層 (FDB)」後所有 l2／l2_uplink 邊消失，其餘圖形不受影響。
- [ ] 看不到連線某一端的部門帳號不會拿到那條邊（任何邊都不可以指向不在圖上的節點）。
- [ ] **視圖模式**：工具列可選 自動／以交換器為中心／只看存取層／只看子網路。自動模式在該範圍有
  FDB 資料時以交換器為中心，沒有就退回子網路版面；選「以交換器為中心」但沒有資料時同樣退回，
  不會畫出一個沒有中心的版面。
- [ ] **存取層 (FDB) 預設不勾**，因此預設畫面與 0.5.213 之前的子網路版面一致。
- [ ] 交換器為中心的版面：交換器在中間、它的機器在上方、子網路節點在交換器正下方，
  只屬於該網段的裝置再排在子網路下面。
- [ ] **「只看存取層」不畫沒有 FDB 資料的裝置**（在大多數裝置沒有 FDB 的環境上驗）。

## 8. 近期功能點檢

- [ ] **通知矩陣**（管理 → 通知發送設定）：事件 × （站內／Email）可切換；存檔後保留；
  事件依矩陣實際送出（IP 申請、憑證到期／派送／飄移、異常）
- [ ] **憑證派送 `files` profile**：只寫憑證檔案，不做 reload/restart
- [ ] **異常偵測頁**：頁籤、各表欄位選擇、`ip_address_id` 預設隱藏、MAC 變動看得到 IP／主機名稱
- [ ] **MCP 用戶端設定產生器**（LLM/AI）：按鈕產出 Claude Desktop／opencode／mcpo／通用片段
- [ ] **LLM 供應商改成 OpenAI 相容**（管理 → LLM/AI）：切換後出現資料外送警告與 API 金鑰欄；
  模型下拉從 `/v1/models` 重新載入（下拉是空的＝打錯路徑）；base URL 已結尾 `/v1` 不會被重複加；
  對話與語意搜尋都可用。切回 Ollama 會恢復 `/api/tags` 清單。
  `select value from system_settings where key='llm'` **不得出現明文金鑰**，只能有 `api_key_enc`；
  設定頁永遠不回傳金鑰本身
- [ ] **嵌入維度**（管理 → LLM/AI）：「檢查維度」會回報模型實際維度與欄位大小的比對。
  換過嵌入模型之後重新索引必須回 `failed: 0`；若回 `0 indexed`，失敗筆數與原因要看得見，
  不可以只給一個光禿禿的零。候選模型還必須對**不同的繁中描述產生不同向量**
  （純英文模型會把它們壓成一樣，看起來正常但排序其實是亂的）
- [ ] **在子網路裡新增位址**：建立表單有必填的 IP 欄位（issue #14）
- [ ] **依網卡 MAC 自動掛裝置**（管理 → 系統設定）：既有安裝預設關閉；**預覽**會回報筆數與
  逐項跳過原因且不改任何資料；啟用後下一輪同步會掛上，並對每個位址寫一筆 IP 異動記錄（含比對原因）。
  手動清掉某個裝置關聯後，確認下一輪**不會**又把它裝回去（這條規則是為了讓背景作業不跟人對著幹）

### 近期（v0.5.6x–0.5.7x）

- [ ] **BMC 帶外主控台**（IPMI SOL，Beta）：逐 IP 啟用（`bmc_enabled`，migration 0092）→
  IP 詳細資料與連線管理出現按鈕；連線時 cipher 自動退回（17→3）；憑證金庫「記住」會存
  （`protocol='bmc'`）且下次自動帶入；RBAC 與 SSH 相同（逐物件＋can_ssh）；
  session 開／關都寫稽核；**設定教學**視窗（表單／工具列／空白提示）打得開且有排錯說明
- [ ] **掃描代理 OS 偵測**（agent ≥ 1.7.0）：設備與 BMC 不再被猜錯 —— Debian 設備（SSH banner）→ `Debian`、
  走 SMB/Service-Info 的 Windows → `Windows`；只靠裝置型號猜出來的（NAS／OpenWrt／路由器）
  一律降成未知，不顯示
- [ ] **通知在地化**：切換介面語言（繁中 ⇄ English）→ 鈴鐺**與**通知頁都用當前語言呈現
  （IP 申請、異常、憑證、失聯 IP）；舊通知退回顯示當初存下來的文字
- [ ] **通知管道**（管理 → 通知發送設定）：Telegram／Slack／Teams／Nextcloud Talk／Zulip 各自
  可儲存（token／webhook 加密，「已設定，留空＝保留」）、逐管道的**測試**按鈕送得出去，
  且啟用的管道會跟 Email／站內一起收到矩陣觸發的事件（例如一筆 IP 申請）
- [ ] 表格頁的**匯出按鈕**有邊框（與「欄位」「重新整理」一致）
- [ ] **DHCP 伺服器／閘道 IP 標示**（migration 0090 `is_dhcp_server`）：OPNsense／pfSense 的
  DHCP 伺服器 IP 與閘道會被標記；IP 詳細資料看得到 DHCP 伺服器／閘道／在 DHCP 範圍內的標籤
- [ ] **LibreNMS 自動建立裝置 IP**（migration 0091 預設開啟）：只在 LibreNMS 有的裝置，
  其主 IP 會被建到對應（限定範圍內）的子網路；重疊而有歧義時跳過，不亂放
- [ ] **PVE 瀏覽器主控台**（VM 走 noVNC／CT 走 xterm，migration 0089）：PVE VM/CT 的 IP 逐筆開關；
  用 PVE 帳號連線；IP 詳細資料與連線管理上有橘色按鈕與 PVE 標籤

---

### 附錄：拋棄式測試資料庫指令（在 prod 主機上跑，**絕不要動 prod 資料庫**）

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
