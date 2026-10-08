"""ISOinsight 整合：登入 ISOinsight 的 HTTP(S) 介面，定期讀取 DHCP 租約，整合進 IP／MAC／主機名稱與來源記錄。

職責拆開（規格 §13），網路請求、JSON 判讀與資料庫寫入不混在同一個函式：

- `config`：來源設定的驗證、密碼加解密、敏感資訊遮蔽
- `client`：GET／POST 登入、Cookie／Token、TLS、逾時與有限重試（只讀，不改來源設備的任何設定）
- `parser`：純資料函式，驗證與正規化租約
- `reconcile`：子網路配對、欄位優先序、冪等與來源證據合併
- `job`：鎖、測試連線、預覽、同步、排程、同步記錄

⚠️ **待真機驗證**：登入回應（Cookie 名稱、Token 欄位、成功／失敗判定）、Session 有效期、
GET／POST form／POST JSON 哪些可用、租約是否全量、有無分頁、時間所屬時區、IPv6。
這些都沒有寫死成「已驗證的原廠規格」：Token 欄位與 Header 由管理員明確設定，登入失敗只認
HTTP 狀態與設定的模式，不臆測 `success`、`code` 之類欄位的語意。

執行位置：jt-ipam 伺服器（主程式與 jt-ipam-sync 排程）。現有架構沒有「經代理執行任意 HTTP 整合」
的通用機制，所以不另外發明代理協定。
"""
