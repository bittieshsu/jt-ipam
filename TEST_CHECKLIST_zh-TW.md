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
- [ ] **舊版升級 —— 必跑，而且與上面那項是兩回事**：`scripts/test-upgrade.sh` 退出 0。
  全新安裝與升級幾乎不共用程式碼，通過全新安裝那道關卡對既有站台什麼都沒證明。要把它指向
  **即將發出去的那份**（`JT_IPAM_REPO=/path/to/candidate`），不要指向上一個已發布版本 ——
  否則測到的是你已經發出去的東西。它會在升級**前**寫一列資料並檢查它還在：升級把資料弄丟是
  最糟的失敗，而且不會讓任何指令回非零
- [ ] 對上一版的環境跑 `scripts/jt-ipam.sh upgrade`，必要時可回滾
- [ ] 這次若新增了目錄 / 套件 / 服務 / DB extension / env，確認**兩支腳本都已同步**
- [ ] **(A) 預設管理員帳密**：全新安裝結尾有印出 `admin` 帳號＋隨機密碼，且密碼存到 `/etc/jt-ipam/.admin-initial-password`（root 0600）；用該密碼能登入
- [ ] **(A) 重置密碼 CLI**：`python -m app.cli.bootstrap create-admin --username admin --password-stdin --force-update` 能重置既有 admin；README 中英都有此段
- [ ] **(B) 代理探測工具**：`agent/jt-ipam-agent-installer.sh` 裝完，主機上有 `nmap` / `nmblookup`(samba-common-bin) / `avahi-resolve`(avahi-utils)；代理 `available_probes` 回報含 os/netbios/mdns
- [ ] **(B) 安裝說明 UI**：掃描代理頁與子網路編輯對話框中，不可勾的探測旁有「安裝說明」彈出，內容顯示對應安裝指令
- [ ] **(C) 參考資料排程**：全新安裝與升級後 `systemctl list-timers` 都有 `jt-ipam-geoip-refresh`／`jt-ipam-oui-refresh`／
  `jt-ipam-recog-refresh` 三個；全新安裝後 OUI 表不是空的（安裝時會立刻抓一次）；`doctor` 三個都列出來
- [ ] **(C) Recog 指紋庫（選用）**：安裝／升級的輸出有「Recog: updated … fingerprints」；把主機的對外連線擋掉再升級，
  只能是警告、升級照常完成；`upgrade --recog-zip <recog-content-版本.zip>` 在離線時裝得起來；
  `python -m app.cli.recog status` 顯示版本

## 5c. headless 瀏覽器 smoke 測試

- [ ] 手機版側欄（`frontend/e2e/mobile-sidebar.spec.ts`，390×844）：收起時寬度 0、內容從最左邊開始；左上角按鈕叫出來、疊在內容上；
  點選功能後與點暗掉的地方都會收回；桌機維持原樣
- [ ] 手機上的機櫃圖（`frontend/e2e/mobile-rack.spec.ts`）：比螢幕寬時可以左右捲；「正面／背面」等工具列不凸出卡片
- [ ] **每一個畫面都用手機寬度走一遍**（`frontend/e2e/mobile-all-routes.spec.ts`，390px，路由從 router 現場解析）：
  整頁不可以左右捲、元素不可以被裁掉或跑出畫面（外層能左右捲的不算）、文字不可以被擠成一個字一行。
  設 `E2E_SHOT_DIR` 會逐頁逐畫面截圖，**人工看過一遍**（量測抓不到「排得醜但沒超出」）
- [ ] **表格欄寬可拖拉、頁籤列可按鈕捲動**（全站；`e2e/anomaly-identify-cols-tabs.spec.ts`）：拖標頭右緣改的是那一欄、
  寬度變化跟拖的距離相符（手寫表格也要，含標頭設了透明度的）；**拖之前的排版與原本一模一樣**（曾經補了最小寬度，
  讓沒有固定排版的表格在手機上把 IP 擠成直排 —— 手機全畫面巡檢要一起跑）。頁籤列放不下時才有箭頭、在哪一側還有東西
  才有那一側的箭頭，按得到最左與最右，箭頭不可擋住頁籤的點擊
- [ ] 手機上的四個回報（`frontend/e2e/mobile-overflow.spec.ts`）：側欄用手指滑得動、不會捲到後面的頁面；
  主控台狀態列換行不擠成直排；通知框不超出畫面；機櫃圖預設比例依畫面縮小、拉過後記住（跟桌機分開）
  ⚠️ iOS 的 100vh 比實際看得到的高，Playwright 模擬不出會伸縮的工具列 —— 側欄的修法要**請使用者在 iPhone 上確認**

- [ ] `cd frontend && pnpm exec playwright test smoke`（免後端，自起 vite preview）全綠
- [ ] 對已部署的站台（給 `E2E_BASE_URL` + `E2E_ADMIN_PASS`）跑 `pnpm test:e2e` 主路徑（登入/sections/audit）

## 5g. 伺服器寫在畫面上的訊息 —— **只要新增或改動錯誤訊息就要跑**

後端不可以送現成的句子。給人看的東西一律是 `{code, params, message}`
（`app/core/ui_error.py`），句子由前端用 `errors.<code>` 組。寫死在後端的中文句子，
在英文與日文介面上**照樣是中文**，而且什麼錯都不會報 —— 英文版上線以來一直是這樣，
沒有人發現。

- [ ] `pytest tests/test_ui_error_codes.py` —— 原始碼裡每個代碼在三個語系都有翻譯
  （測試掃原始碼；漏補是安靜的）
- [ ] 新代碼：逐一讀三個語系的句子，確認參數真的有插進去。翻譯漏了 `{reason}`
  等於**把診斷資訊拿掉** ——「pfSense 回報錯誤」而看不出是 DNS、被拒還是憑證
- [ ] 把介面切成英文與日文，實際觸發一次新的錯誤。用來**決定流程**（不只是顯示）的代碼
  要特別小心：攔截器會把 `detail` 攤平成字串，要從 `detail_code` 讀 —— Proxmox 的
  兩階段驗證就是這樣壞掉而沒有人發現
- [ ] 不要把中文詞當參數傳（`what="下載"`）：差異寫進代碼裡
  （`sftp_download_too_large`），否則英文句子中間會夾一個中文詞

## 5h. guacd 預編檔 —— **每次發版都要跑**（不只動到主控台的時候）

guacd 由我們自己編、每個作業系統版本一份：Debian 已經移除套件，Ubuntu 只有帶著可遠端執行程式碼漏洞的 1.3.0。
預編檔動態連結各發行版自己的函式庫，所以**出了新版 OS 就要多編一份** —— 否則客戶升級到新 OS 後 guacd 引擎就不能用。
使用者交代（2026-09-25）：每次發版都要上網查，有新版就跟著編。

- [ ] `scripts/guacd/check-new-os.sh` —— 向 endoflife.date 查 Debian（12 以上）與 Ubuntu（22.04 以上，
  非 LTS 在支援期內也算）目前支援中的版本，跟 `scripts/guacd/targets.txt` 比對。回傳 1 會列出缺哪些：
  加進 `targets.txt`。已經停止支援的會列成「可以退場」
- [ ] guacamole-server 上游：`scripts/guacd/source.env` 釘的版本之後有沒有新版或新 CVE？目前釘在
  `staging/1.6.1` 的 commit，因為 1.6.0 在 Ubuntu 26.04 畫第一個畫面就 segfault ——
  **1.6.1 正式發版後改用 Apache 官方 tarball，並核對官方公布的檢查碼**
- [ ] 有任何變動：`scripts/guacd/build.sh` 再 `scripts/guacd/verify.sh`（都要 docker；鏡像站用
  `APT_MIRROR`／`UBUNTU_MIRROR`，同其他關卡）。verify 會在乾淨容器只裝執行期套件，**並且真的連一次 RDP 靶** ——
  外掛載得到不算數（1.6.0 在 26.04 上外掛載得到，一畫第一個畫面就當掉）。它存在預編檔旁邊的截圖要看過
- [ ] 安裝腳本裝 guacd 的那一段也要在乾淨 OS 上走一次（guacd 是**必要元件**，安裝一定會裝，裝不起來安裝要停下來）：
  `scripts/test-fresh-install.sh debian:12`（走客戶的路：從 GitHub release 下載並核對）或
  `GUACD_TARBALL=<同 OS 的預編檔> scripts/test-fresh-install.sh debian:12`（還沒發佈的建置）
  —— 驗裝得起來、服務在跑、**只**在 127.0.0.1 回應、doctor 綠
- [ ] configure 要正確偵測 FreeRDP 3：FreeRDP 3 的目標若印出「freerdp structs have a context... no」，
  編譯腳本會刻意失敗（靠 CPPFLAGS 裡的 `-Wno-error` 防止，原因見 `in-container-build.sh` 的註解）
- [ ] 每個壓縮檔都要有 `LICENSE`、`NOTICE`、`SOURCE`（前兩個是 Apache-2.0 的要求；`SOURCE` 指向確切的原始碼與編譯腳本）。
  libvncclient 是 GPL-2+、由發行版提供 —— 絕不可以打包進去。也不可以把我們的版本稱作 Apache 官方發行版（ASF 商標）

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
- [ ] **SFTP 單檔上限（系統設定）**：預設 100 MB；改大後存得住、舊版頁面按儲存不會把它改回預設；
  超出 1～102400 MB 要提示並還原（**不可以**被輸入框自動夾到邊界後存下去）。改了上限要**自動**實測傳輸路徑，
  結果講出上下傳速度與「傳一個上限大小的檔案要多久」；路徑有問題要講出是哪一種（WebSocket 不通／1009 訊息太大／
  傳到一半被切／資料送不過去）。現成 spec：`e2e/sftp-limit-probe.spec.ts`（1009 用 routeWebSocket 模擬）
- [ ] **SFTP 大檔下載**：超過 64 MB 在 Chrome／Edge 會先問存到哪裡、邊收邊寫進磁碟（內容逐位元組一致、分段寫入）；
  不支援的瀏覽器退回收進記憶體（2 GB 以上直接講要換瀏覽器）；超過上限當場提示；下載中顯示進度。
  現成 spec：`e2e/sftp-stream-download.spec.ts`（需 `E2E_SFTP_ROOT`、`sftp-target.py` 起在 2223）
- [ ] **正式機再實測一次**：從外面（經過前端反向代理）打開系統設定，看傳輸路徑檢查的結果 —— 開發機的路徑沒有那一層
- [ ] 現成 spec：`frontend/e2e/terminal-links.spec.ts`（需 `E2E_SSH_ADDRESS_ID/USER/PASS`；
  另需該帳號 `can_ssh`、該 IP `ssh_enabled`，第一次連線要按「信任並連線」）

## 5e. 超大規模環境 —— **動到同步、清單頁、拓樸、匯出或查詢寫法時要跑**

GitHub issue #47（一台裝置三萬多個埠 → IN 超過 asyncpg 32767 參數上限）之後的規則：中等規模的資料測不出
「一次查詢放得下」這類假設。

- [ ] 守門測試綠：`tests/test_many_values_in.py`（同步路徑上不可以有 Python 清單的 `IN`、三萬個值要能過、
  整批寫入）、`tests/test_fk_indexes.py`（每個外鍵都有索引）、`tests/test_topology_scale.py`、
  `tests/test_librenms_arp_sync.py`（沒變動的一輪查詢數是常數）
- [ ] 灌大型站台：建 `jt_ipam_scale` → `alembic upgrade head` → `POSTGRES_DB=jt_ipam_scale python -m tests.seed_scale`
  （2,000 個 /24＋滿的 /16、14.5 萬 IP、2 萬裝置、20 萬埠（一台 4 萬）、29 萬 FDB、10 萬租約、50 萬異動）
- [ ] 後端接這個庫，每一支 GET 打一次：沒有 5xx、沒有超過數秒的；拓樸在兩萬台裝置時回「太大」而不是卡住後端
- [ ] 同步探測（假 API 回傳同規模資料）：LibreNMS 一輪在數分鐘內、沒有變動的一輪查詢數不跟筆數成正比
- [ ] 前端接這個庫：`e2e/all-routes.spec.ts`／`mobile-all-routes.spec.ts` 全綠；/16 子網路頁、4 萬埠裝置頁
  實際打開，主執行緒最長卡頓不超過約 1.5 秒（量 longtask，不要用看的）
- [ ] 新的清單、同步、匯出要回答：十萬個 IP、單台數萬個埠、十萬筆租約時會怎樣（參數上限、全部載入記憶體、
  逐筆查詢、一次畫完、沒有分頁）

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

- [ ] 新增整合：API URL ＋ X-API-Key，自簽憑證要**關掉驗證 TLS**；儲存（金鑰只進不出）
- [ ] **測試連線** → 成功並顯示 pfSense 版本
- [ ] **立即同步**（ARP＋別名＋規則開啟；若 LAN 的 DHCP 由別台負責則 **DHCP 關閉**）→ 回筆數；
  範圍內的 ARP IP 會被標上 `last_seen`（來源 `pfsense`）與 MAC；別名／規則筆數與實機相符
- [ ] **欄位名稱回歸**：ARP／DHCP 用的是 `ip_address`／`mac_address`（不是 `ip`／`mac`）；
  `hostname == "?"` 要視為空白
- [ ] **範圍安全**：設了 `scope_subnet_ids` 之後只會標到那些子網路裡的 IP（重疊網段用 `.limit(1)`）
- [ ] **規則／NAT 檢視**（眼睛按鈕）能列出同步到的規則與 NAT 筆數
- [ ] **Graylog DSV**（開啟 Expose DSV 並設好 token）：`GET /api/v1/lookup/pfsense/{id}/aliases?token=…`
  與 `…/rules?token=…` 回 CSV/TSV；**token 錯 → 401**；`expose_dsv` 關閉 → 404
- [ ] 刪除整合；`jt-ipam-sync` 每 ~5 分鐘會自己帶到已啟用的整合且不出錯

## 7b. VMware ESXi / vCenter 整合（管理 → 整合 VMware）—— **Beta**

> SOAP 端點固定是 `<url>/sdk`。同一套實作**同時**涵蓋單機 ESXi 與 vCenter —— 它們是同一組 VIM API，
> ContainerView 會吸收掉層級深度的差異。請用**唯讀**帳號：這個整合從不寫入。
> 免費／未授權的 ESXi 本來就只開放唯讀 API，剛好夠用。

- [ ] 新增整合：URL ＋ 帳號密碼，自簽憑證要**關掉驗證 TLS**；儲存（密碼只進不出）。
  編輯時密碼留空＝不變更
- [ ] **測試連線** → 逐步診斷：RetrieveServiceContent（產品與版本）、Login、
  RetrievePropertiesEx（VM 數）。密碼錯必須停在 **Login** 並顯示 VMware 自己的訊息，
  不可以是空泛的「伺服器錯誤」—— VMware 把認證失敗包成 HTTP 500 的 SOAP Fault
- [ ] **立即同步** → 回 VM 數；叢集清單看得到這個整合、型別 `vmware`；
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
- [ ] 刪除整合；`jt-ipam-sync` 每 ~5 分鐘會自己帶到已啟用的整合且不出錯

## 7m. guacd 主控台引擎 —— **只要動到主控台、guacd 或它的編譯就要跑**

guacd 是 RDP 與 VNC 的預設引擎（2026-09-27 起，已安裝的站台由遷移 0158 強制改過來），SSH 可以改用
（管理 → 系統設定，逐協定選）。換引擎不可以改變主控台被允許做的事。

- [ ] 預設值：全新安裝的設定頁 RDP、VNC 顯示「guacd（預設）」，SSH 顯示「內建（預設）」；舊站台升級後 RDP／VNC 變成 guacd
  （`frontend/e2e/rdp-engine.spec.ts`、`tests/test_console_engine_default.py`）
- [ ] guacd 是必要元件：「版本資訊 → 必要相依」列著它（版本、是否在執行）；aardwolf 在「選用相依」
- [ ] 彈出層在 AI 助手浮動按鈕之上：開在右下角的確認框／下拉選單，與按鈕重疊的地方也點得到（`frontend/e2e/chat-fab-overlays.spec.ts`）
- [ ] guacd 版本字串很長時（`… for Ubuntu 24.04 LTS (amd64)`），「必要相依」卡片仍然好讀：名稱欄不被擠壓、狀態在右邊、
  版本（去掉 OS 後綴）在名稱下方 —— 桌面與手機寬度都要看（`frontend/e2e/version-required-deps.spec.ts`）
- [ ] guacd 停掉時 RDP／VNC **仍連得上**（退回內建引擎，前提是這台有選用的 aardwolf），設定頁的 guacd 狀態是紅的、doctor 與系統診斷是失敗；
  連線中途不會因為 guacd 起落而卡住（引擎寫在票證裡，WebSocket 照票證）

- [ ] `frontend/e2e/console-guacd.spec.ts`，對本機 guacd 與三個測試靶跑（見檔頭：xrdp 容器 3389、
  `e2e/fixtures/vnc-target.py` 5999、sshd 2222）。它驗：畫面真的畫出來（量像素，不是只有 canvas）、
  按鍵與中文以 Guacamole 的 `key` 指令送出、Ctrl+Shift+V 會**先**送剪貼簿、再送 V
- [ ] 每個協定親眼看一次畫面（測試讀不到字）：RDP 打得出字、VNC 看得到目標、SSH 看得到提示字元，
  而且**中文是全形寬度**（又窄又小＝guacd 不在 UTF-8 locale 下跑；systemd 單元設了 `LANG=C.UTF-8`）
- [ ] SSH：已釘選的主機金鑰要能被 guacd 接受（靠我們的修補 `scripts/guacd/patches/0001` 讓 libssh2
  優先交涉釘選的那一種）；金鑰真的換了時，仍要回「主機金鑰不符」
- [ ] 帳密絕不經過瀏覽器：WebSocket 上只有一則 config，之後全是 Guacamole 指令；伺服器只放行
  key／mouse／size／clipboard／sync／nop…（`tests/test_guacd.py::test_relay_forwards_allowed_and_drops_the_rest`）
- [ ] 剪貼簿政策不因引擎改變：RDP 只有在「RDP 控制端貼上」開啟時才能貼、被控端內容不回傳；VNC 沒有；SSH 可複製可貼上
- [ ] 分頁切到背景超過 5 分鐘不會斷線（背景分頁的計時器會被節流成一分鐘一次；保活由伺服器送，不靠頁面）
- [ ] `sudo jt-ipam.sh doctor` 與「管理 → 系統診斷」看得到 guacd；停掉 `jt-ipam-guacd` 時兩邊都要變紅並附修法、
  說明目前改用內建引擎；內建引擎也不能用時（例如沒有 aardwolf），發票證要回看得懂的 503
- [ ] VNC 帳號：要帳號的伺服器（檔頭的 VeNCrypt 帳密靶 5998）帳號留空要回「請在「帳號」欄填入帳號」、填了要連得上；
  密碼錯要說「帳號或密碼錯誤」而不是「連不到主機」，真的連不到時才說連不到（只在 guacd 失敗**之後**才探 TCP ——
  TigerVNC 會把「連上就斷」算成一次認證失敗，連幾次就封鎖來源；測到一半全部失敗先看靶的日誌有沒有 `blacklisted`）
- [ ] 狀態列標出這次用的引擎（「引擎：guacd」等），RDP／VNC 不再有 Beta 標示
- [ ] 已知限制：SSH 終端機裡，一行中第一個輸入的中文字可能要等整行重畫（Ctrl+L）才顯示；指令內容本身是對的

## 7b3. 獨立的 Kea／ISC DHCP 伺服器（issue #45）—— **動到這兩個整合、代理的 dhcpd 回報或 DHCP 共用寫入層就要跑**

- [ ] **真的 Kea 往返**（可丟棄的容器即可：Ubuntu 24.04 套件是 Kea 2.4 走控制代理；ISC 官方套件庫的 Kea 3.0 可以直連）：
  測試連線回版本與連線方式（控制代理／直連）；同步寫進範圍（區間與 CIDR 兩種寫法、共用網路底下的子網路）、保留、租約
  （既有 IP 標「有租約」、MAC 來源是 kea_dhcp、主機名稱）；host_cmds 有沒有載入都要會；沒有 lease_cmds 時範圍照樣同步、
  頁面提示；密碼錯誤是失敗並帶 401 原因。⚠️ 後端的外連防護擋迴路位址：Kea 要綁在 docker 橋接介面（172.17.0.1），
  本機後端要開 OUTBOUND_ALLOW_PRIVATE
- [ ] **真的 isc-dhcp-server 往返**：發行版預設的 dhcpd.conf（滿是註解掉的範例）不可以被讀出東西；include 要跟到；
  `key` 區塊裡的 secret 絕對不可以出現在回報裡；用戶端要租約後，代理讀真的 dhcpd.leases 回報 → 固定分配標「固定分配」、
  租約標「有租約」；同一個位址後面的記錄蓋前面的
- [ ] 代理只有在伺服器指派了 ISC 來源時才讀檔（poll 回應的 `dhcpd`）；別的代理不能替不屬於它的來源回報（404）；一台代理只對應一個來源
- [ ] 讀不到檔（權限、路徑錯）時不清掉原本的資料，最後錯誤寫出檔名與原因；代理超過回報間隔 3 倍沒回報 → 來源標成失敗（健康告警）
- [ ] 刪除來源會收回它寫進共用表的範圍／保留／租約／主機名稱
- [ ] `e2e/dhcp-standalone.spec.ts`：Kea 測試連線失敗時看得到真正原因（不是前端 15 秒逾時）；ISC 讀檔狀態、被佔用的代理反灰

- [ ] **裝置匯入（issue #46，`e2e/device-import.spec.ts`、`tests/test_device_import.py`）**：清單匯出的檔案（中／英／日介面各一次）
  原樣匯回來不報錯；範本（含現有裝置）用「更新」模式匯回來零錯誤；地點／機櫃／單位寫名稱、只寫機櫃時推出地點、
  同名機櫃要求補地點；已存在的裝置略過／更新（空白不清值）；同一個檔案裡的 U 位重疊會被擋；有錯的列一筆都不寫；
  預覽不留任何變更；每台裝置都有稽核；.xlsx 也能匯；公式注入（= 開頭）在範本裡有加單引號、匯回來會拿掉

## 7c. 整合同步的韌性 —— **每個整合都適用，不只這次動到的那個**

實機本來就是「部分可讀」。防火牆回報「10 個端點中有 9 個可讀取」是常態而非異常：
韌體版本有差異，唯讀 API 帳號也很少能讀到每一項資源。絕對不可以發生的是**一支端點
讀不到就把其餘同步一起帶走**（v0.5.195：DHCP 租約路徑讀不到，導致 ARP、政策、NAT 與
位址物件通通不同步，而畫面上只有一行錯誤）。

- [ ] **區段隔離**：故意讓一支端點失敗（改成錯的路徑，或收掉那一項權限），確認其餘區段照常同步
- [ ] **部分失敗看得見**：整合會把失敗內容寫進 `last_error`；有失敗的那一輪絕不可以對使用者顯示成完全成功
- [ ] **不可跨整合連鎖中止**：單一整合失敗不能讓整輪同步停掉（寫 `last_error` 前要先 `session.rollback()`，
  否則下一次寫入會二次爆炸）
- [ ] **錯誤訊息要帶證據**：「回應不是 JSON」這種訊息在現場毫無用處。要附狀態碼、`content-type`
  與回應開頭約 120 字，並指出最可能的原因（例如裝置回的是網頁介面 → 該韌體沒有這支端點，
  或 API 帳號讀不到）
- [ ] **測試連線要反映真實**：逐端點診斷顯示的結果必須與同步實際拿到的一致 ——
  不可以對同步讀不到的東西打綠勾
- [ ] **上游刪掉的要跟著消失**（2026-09-26 稽核：16 個來源的主機名稱、DNS 記錄都從不清）：
  在上游刪掉一筆（DNS 記錄、租約、VM、代理、主機），同步一輪後 IP 的主機名稱與鏡像資料都要不見。
  主機名稱一律經 `HostnameRun`（`services/hostname_reports.py`）：看到就 `report`、這輪不確定的實體 `hold`、
  結束時 `finish(complete=…)` —— **complete 必須反映「這一輪真的完整讀到」**，不可以照抄心跳的 ok
- [ ] **讀不到不可以清**（反方向的缺陷）：讓端點逾時／回 403 一輪，既有的主機名稱、NAT、政策、VPN 通道、
  DHCP 範圍／固定分配都要原封不動，錯誤寫進 `last_error`。404（這台沒有那個功能）才算「讀到了、沒有」。
  「VDOM／vsys 清單讀不到而退回預設值」不是完整清單，整份取代的區段這一輪不可以動
- [ ] **多台同類不互刪**：兩台同廠牌各報各的，一台不再回報時不可以刪掉另一台還在報的
- [ ] **斷路器**：讓 API 回空清單一輪（權限被收），主機名稱不可以被整批清掉，`last_error` 要寫出原因；
  規則異動偵測不可以發「全部移除」
- [ ] **沒改的欄位不是手動編輯**：在 IP 編輯表單只改說明、按儲存，主機名稱來源與 MAC 來源都不可以變成手動
- [ ] **裝置連接埠跟著 LibreNMS 走**（2026-09-27：拔掉的雙埠網卡、USB 網卡，LibreNMS 已標成刪除，清單還列著）：
  拔一張網卡／拔掉 USB 網卡、等 LibreNMS 重新探索後同步或按「從來源匯入」—— 那些埠要從「連接埠／佈線」消失；
  自己建的、已接線的、有穿透對應的埠都保留；讀取失敗或讀到 0 個埠時一個都不刪。Docker 的 `veth…` 介面一律不匯入
  （`tests/test_device_ports_reconcile.py`）

## 7d. 從掃描代理執行探測 —— **只要動到工作佇列或代理就要跑**

讓伺服器把工作交給代理，等於讓那支代理可以應要求在客戶網路裡發送探測封包。
這個功能的安全性等於它最寬鬆的那道檢查。

- [ ] **種類白名單**：ping / tcp / traceroute / rdns / identify 以外一律拒絕 —— 後端要擋，
  **代理也要自己獨立擋**（後端被入侵時不得因此擴大範圍）
- [ ] **目標驗證**：shell 特殊字元、命令替換、參數注入（`-oProxyCommand=…`）都要拒絕；
  參數一律以陣列傳給子行程，永遠不經過 shell
- [ ] **上限有效**：目標數、埠數、每代理待辦數，以及次數／逾時的夾限
- [ ] **歸屬**：代理只能結束自己領到的工作
- [ ] **過期**：把代理停掉後，排隊中的工作要過期作廢而不是等代理回來才補跑 ——
  遲到幾分鐘的探測結果比沒有結果更糟
- [ ] **真實代理往返**：建立 → 領取 → 執行 → 回報 → 取回結果，且畫面要標明是哪個代理跑的
- [ ] **IP 詳細頁「探測」（identify）**：
  - 只有管理員看得到按鈕；唯讀帳號直接打 `POST／GET /addresses/{id}/identify` 要回 403
  - 目標只能是那筆 IP 記錄本身的位址：工具頁的代理探測送 `identify` 要被拒；代理收到主機名稱、
    多個目標、網段也要自己拒絕
  - 由該子網路指定的掃描代理執行；子網路沒有指定代理時講清楚（不是空白失敗）
  - 同一個 IP 同時只能有一個探測；每次發起都寫稽核（action=identify）
  - NSE 腳本清單寫死在代理裡（只讀資訊：banner／HTTP 標題／TLS 憑證／SSH 主機金鑰／SMB／RDP），
    後端送什麼都改不了；不含工控協定埠
  - 實機對 PVE 主機跑一次：類型要判成虛擬化主機、8006 要在連接埠清單裡、名稱不可出現憑證簽發者
    或萬用名稱；代理沒裝 nmap 時要顯示「只查了名稱」的提示
- [ ] **以位址探測（異常偵測清單的「探測」，IPAM 沒有記錄的位址）**（`e2e/anomaly-identify-cols-tabs.spec.ts`）：
  - 位址必須落在 IPAM 管理的子網路內（取最小的那一層）、由那個子網路的代理執行；管理網段外的位址回
    `identify_not_managed`、網路／廣播位址回 `identify_bad_target`，都不建立工作
  - 同一個 CIDR 的重疊網段由不同代理負責 → `identify_ambiguous`，不可以挑一個就掃
  - 已經登記的位址轉到那筆記錄的探測頁（歷次結果共用）；重複記錄不可標成「IPAM 沒有記錄」
  - 作業列掛位址、完成通知的連結回到 `/identify/ip/<位址>`；稽核帶子網路
- [ ] **探測＋Recog 指紋庫**（`backend/tests/test_recog.py`、`e2e/ip-identify.spec.ts`、`e2e/version-recog.spec.ts`）：
  - 匯入：每條指紋都要通過自己附的範例，否則剔除（3.2.0 約剔除 5 條）；zip 只讀 `xml/*.xml`、XXE 被擋、
    太小的一版（少於 1000 條）不可以蓋掉已安裝的
  - 摘要：OpenSSH 註解推出發行版、設備預設憑證推出類型／廠牌／型號、「不下結論」的條目不採用、nmap 已認出產品的埠
    不重複列、預設憑證上的名稱不列進「名稱」；沒裝 Recog 時摘要與以前完全相同，畫面提示沒有安裝
  - 實機拿正式機既有的探測結果比對加入前後：不可以有類型被改錯（NAS、PVE、郵件主機、IPMI）
  - 版本資訊的「選用資料庫」卡片：版本、指紋數、上次檢查；「立即檢查更新」寫稽核（target=recog_db_update）
  - 更新失敗（GitHub 連不到）：已安裝的一版不動，錯誤顯示在卡片上；三週沒成功更新時系統診斷警告

## 7d2. 掃描代理的負載 —— **只要動到代理的掃描迴圈、回報或負載判斷就要跑**

- [ ] **上線偵測不被重量探測拖住**：代理每個子網路做完上線偵測就立刻回報；反解／NetBIOS／mDNS／OS 指紋
  在背景跑，名稱查詢不等 OS 指紋。實機看 `journalctl -u jt-ipam-scan-agent`：每輪的「probes=… alive=…」
  幾秒到幾十秒內出現，`[heavy]` 另外跑
- [ ] 背景結果**不算上線證據**（`liveness=false`）：不更新最後出現時間、不自動新增 IP
- [ ] 每輪統計寫進 `scan_agents.last_cycle` 與 `scan_agent_cycles`（保留 7 天）；掃描代理頁「負載」欄與面板顯示得出來
- [ ] 超載通知：連續 3 輪才發、只發一次、恢復時再發一次；建議內容要能照做（移哪幾個子網路、哪個子網路特別慢、哪個被截斷）
- [ ] 不自動搬子網路：面板上的「移到別的代理」要管理員自己按，並提醒那台代理要在同一個網段

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
- [ ] **虛擬機（預設不勾）**：勾選後 VM 貼在所在主機正下方、與主機同屬一個網段框；
  取消勾選後完全消失。找不到主機或名稱對到多台裝置的 VM 不畫。已對映成裝置的 VM
  不會在圖上出現兩次。
- [ ] **稽核覆蓋**：`pytest tests/test_audit_coverage.py` 綠。新增會改資料的端點時，
  要嘛補稽核、要嘛寫進 `EXEMPT` 並附理由（不可以只是讓測試通過）。
- [ ] **機櫃圖對外嵌入**：系統設定啟用＋產生權杖後，開啟某一櫃的「對外嵌入」，複製網址
  用**未登入的瀏覽器**開啟要看得到圖；錯誤或空白權杖回 401；沒開放的機櫃與不存在的機櫃
  回**完全一樣**的 404；重新產生權杖後舊網址立刻失效。
- [ ] **每條線的依據**：點任一條線，詳情要顯示「依據」（有人登記／第三方監控回報／
  被動學到／名稱推測）。開啟「只看已登記」後只剩人為登記的線；在子網路視角下
  IP↔裝置的連結仍在，在存取層視角下可能整個清空（那是正確的，代表沒有人登記過）。
## 7k. 關聯欄位的邏輯 —— **動到任何「A 決定 B」的欄位時**

> 原則：**能從既有關聯推出來的，就不要叫使用者再講一次**；真正該擋的只有
> 「兩邊都填了卻互相矛盾」，那代表其中一個是錯的，替使用者挑一個等於在猜。
> 客戶回報過一次：選了機櫃還被要求選地點 —— 而機櫃的下拉本來就顯示成「地點 / 機櫃」。

- [ ] **裝置的機櫃 → 地點**：只選機櫃、不選地點可以存檔，存完地點是機櫃的地點。
  兩邊都選且不一致時要擋下並說明。機櫃自己沒有地點時不擋、也不編造一個。
  **兩個入口都要測**（裝置清單頁、裝置詳細資料的編輯視窗）——
  同一段邏輯有兩份實作時，通常只有一份被改到。
- [ ] **子網路 → 區段**：從某個區段底下新增子網路時，區段要自動帶入。
- [ ] **IP → 子網路**：從子網路頁新增 IP 時，子網路要自動帶入且不可改成別的網段。
- [ ] **VM → 叢集／實體主機**：VM 的所在節點來自虛擬化平台，不是讓人手選。
- [ ] **機櫃 U 位 → 機櫃高度**：U 位加上佔用 U 數不可超過該機櫃的高度；
  半 U（左／右）在同一個 U 上不可重疊。
- [ ] **掃描設定 → 掃描代理**：啟用掃描而沒有指定代理時要擋（這是真的缺資訊，不是可推導）。
- [ ] **憑證派送代理 → 憑證範圍**：代理只拿得到範圍內的憑證。
- [ ] 每加一個「選了 A 就必須填 B」的檢核前先自問：**B 是不是從 A 查得到？**
  查得到就用推的；查不到才擋。
- [ ] **相依清單與實際宣告一致**：`pytest tests/test_dependency_page.py` 綠。
  新增任何第三方套件時，除了 `pyproject.toml` / `package.json`，**版本資訊頁的清單也要加** ——
  那一頁是升級與稽核時用來核對「這台實際裝了什麼」的依據，漏了不會報錯、只會安靜地少一行。

### 7.x 主控台與檔案傳輸（WebSocket）—— 這一類缺陷全部人工走一次

這一整組來自 v0.5.222~229 的實機回報。共通點是**症狀都長得一樣（「連線已中斷」）、
原因卻各不相同**，所以不能只測「上傳成功」一條路徑。

- [ ] **拖一個資料夾進去**（只拖資料夾，或資料夾＋檔案混拖）：**整個資料夾連同巢狀內容**
  都要上傳到遠端、目錄結構一致，一起拖的檔案照常上傳，連線**不可以斷**。⚠️ 不可以用「大小 > 0」判斷是不是檔案 ——
  macOS 把資料夾回報成 **256 位元組**，這正是把整條連線打壞的那個判斷。
- [ ] **一次拖多個檔案**：每一個都要完整送達，逐一比對**位元組數與 md5**。
  只到一個、或到了但是 **0 位元組**，就是上傳迴圈在中途被打斷。
- [ ] **上傳中途改送指令**：客戶端宣告了大小卻沒送完就送下一個指令時，伺服器要
  **結束這次上傳、把那個指令照常執行、連線繼續可用**。指令不可以被吞掉。
- [ ] **宣告了大小卻完全不送**：要在逾時後回報錯誤並清掉半成品，**不可以無限期等待** ——
  等待中的那條協程會佔住 WebSocket 與 SSH 連線，使用者看到的是「整個頁面沒反應」。
- [ ] **一次上傳失敗之後還能繼續用**：失敗不該逼使用者重新連線。
- [ ] **失敗之後可以馬上重連**：重連的 ticket 請求不可以逾時。若要等很久才有反應，
  代表前一條連線還卡在事件迴圈裡沒放掉。
- [ ] **主控台閒置不可被切斷**：開啟 SSH／SFTP／RDP／VNC／noVNC 後**放著不動 3 分鐘**，
  回來要能直接操作。任何一個沒有心跳或保活的主控台，都會被中間的反向代理在
  **60 秒無流量**時切掉（常見預設值），使用者看到的是莫名其妙的「連線已中斷」。
  ⚠️ 目前 BMC 主控台**沒有**心跳（純轉發，注入資料會污染 SOL），已知缺口。
- [ ] **主控台傳一個「大到會傳很久」的檔案**（例如 50 MB，或在慢速線路上傳 5 MB）：
  要能傳完。**這一項不能只在區域網路裡測** —— uvicorn 預設 20 秒收不到 pong 就切斷連線，
  而 pong 會排在上傳資料後面，只有真實的慢速上行才會踩到（v0.5.231 修）。
  瀏覽器的網路節流工具**測不出來**，它不會讓 pong 被排在後面。
- [ ] **守門測試要綠**：`pytest tests/test_sftp_upload_stall.py tests/test_ws_wait_timeouts.py`。
  它們擋的是「收資料的迴圈沒有逾時」「用 `receive_bytes()` 對文字框會 KeyError」
  「開檔後忘了送 `put_ready`」這三件會讓上傳整個失效的事。
- [ ] ⚠️ **改動上傳區塊之後一定要真的傳一次檔案**。0.5.225 改寫時整行弄丟 `put_ready`，
  上傳完全不會開始，而症狀跟原本要修的 bug **一模一樣** ——
  很容易被當成「還沒修好」而不是「修壞了」。型別檢查與單元測試都看不到這種。

## 8. 近期功能點檢

- [ ] **OCS 卡片顯示 OCS 自己的硬體**（裝置明細）：製造商／型號／序號取自 OCS，不是別的來源填的裝置欄位
  （LibreNMS 建立的 Windows 裝置曾顯示「windows／Intel x64」）；有主機板與 BIOS；系統序號是出廠佔位
  （「0123456789」）時改顯示主機板序號並標「（主機板）」；主要零件列出處理器（核心／執行緒）、記憶體
  （總量＋模組）、實體磁碟（不含 zram／loop）、顯示卡（lspci 與驅動回報的同一張合併）。升級後第一次同步前，
  卡片要講「下一次同步後出現」（`e2e/ocs-device-card.spec.ts`、`tests/test_ocs_hardware.py`）
- [ ] **未裝 Agent 的 IP 可依狀態篩選**（Wazuh 與 OCS 兩頁）：狀態欄是 IP 清單同一顆燈號、篩選用同一套規則；
  選項只列資料裡有的狀態；匯出的子網路／區段／單位／狀態都有值（以前是空白）（`e2e/missing-agent-scope-filter.spec.ts`）
- [ ] **連接埠／佈線的 MAC 欄顯示廠商**：MAC 下方一行，與 IP 清單相同（`e2e/device-ports-mac-vendor.spec.ts`）

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
