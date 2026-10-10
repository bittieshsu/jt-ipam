#!/usr/bin/env python3
"""產生合規對照文件：docs/COMPLIANCE.md（英）、COMPLIANCE_zh-TW.md、COMPLIANCE_ja.md 與官網的 docs/compliance.html。

內容只放在這支腳本裡（下面的 CONTENT），四份輸出由它產生，三種語言才不會各改各的。
原則（使用者 2026-10-09）：**做不到的不寫**。每一項都要能在程式或測試裡找到依據；新增或修改項目時，
先確認實作與測試，再改這裡。

用法：
    python3 scripts/gen-compliance-docs.py          # 重新產生
    python3 scripts/gen-compliance-docs.py --check  # 只檢查輸出是否與內容一致（測試用）

官網頁面只改 <!-- generated:start --> 與 <!-- generated:end --> 之間；頁首、樣式、頁尾第一次從 adoption.html 取用，
之後保留原樣。
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
LANGS = ("zh", "en", "ja")
MD_FILES = {"en": DOCS / "COMPLIANCE.md", "zh": DOCS / "COMPLIANCE_zh-TW.md", "ja": DOCS / "COMPLIANCE_ja.md"}
HTML_FILE = DOCS / "compliance.html"


def T(zh: str, en: str, ja: str) -> dict[str, str]:
    return {"zh": zh, "en": en, "ja": ja}


TITLE = T("合規對照：ISO/IEC 27001 與 ISO/IEC 42001", "Compliance mapping: ISO/IEC 27001 and ISO/IEC 42001",
          "コンプライアンス対応：ISO/IEC 27001 と ISO/IEC 42001")
SHORT = T("合規對照", "Compliance", "コンプライアンス")
DESC = T("jt-ipam 已具備、可作為佐證的資訊安全與 AI 管理控制，以及導入組織使用後要自己做的事。",
         "The information security and AI management controls jt-ipam already provides as evidence, and what the adopting organisation has to do itself.",
         "jt-ipam が備え、根拠として使える情報セキュリティと AI 管理の管理策、および導入組織が自ら行うこと。")

INTRO = [
    T("jt-ipam 本身不是一張認證，用了它也不代表通過 ISO/IEC 27001 或 ISO/IEC 42001。這份文件分兩部分：jt-ipam 已經做到、可以拿來當佐證的控制（每一項都附上可以自行驗證的測試或設定位置），以及導入組織使用 jt-ipam 之後要自己負責的事。",
      "jt-ipam is not a certification, and using it does not mean passing ISO/IEC 27001 or ISO/IEC 42001. This document has two parts: the controls jt-ipam already implements and that can serve as evidence (each with a test or setting you can check yourself), and what the adopting organisation is responsible for after deploying jt-ipam.",
      "jt-ipam 自体は認証ではなく、使用したからといって ISO/IEC 27001 や ISO/IEC 42001 に適合するわけではありません。この文書は 2 部構成です。jt-ipam がすでに実装し、根拠として使える管理策（それぞれ自分で確認できるテストや設定の場所付き）と、導入組織が jt-ipam を使った後に自ら責任を持つことです。"),
    T("這裡只列出目前程式確實具備的功能，依 main 分支撰寫（1.0.5 之後的改動見 CHANGELOG 的 Unreleased），之後的變動以 CHANGELOG 為準。正式盤點時請固定版本或 commit，並在自己的環境實際驗收。",
      "Only capabilities the code actually has are listed. It was written for the main branch (changes after 1.0.5 are under Unreleased in the CHANGELOG); later changes are in the CHANGELOG. For a formal assessment, pin a version or commit and verify in your own environment.",
      "現在のコードに実際にある機能だけを記載しています。main ブランチに基づいて作成しており（1.0.5 以降の変更は CHANGELOG の Unreleased を参照）、以降の変更は CHANGELOG を参照してください。正式な棚卸しではバージョンまたは commit を固定し、自社環境で実際に検証してください。"),
]

H_CTRL = T("面向", "Area", "項目")
H_HOW = T("jt-ipam 的做法", "What jt-ipam does", "jt-ipam の実装")
H_EVID = T("驗證方式（測試、設定）", "How to verify (tests, settings)", "確認方法（テスト・設定）")

ISO27001 = [
    (T("登入、雙因素驗證與工作階段", "Sign-in, two-factor authentication and sessions", "ログイン・二要素認証・セッション"),
     T("本機與 LDAP 帳號可啟用 TOTP 雙因素驗證；管理員可要求管理員或所有人使用（還沒設定的人登入時先完成設定，不會被鎖在外面；被要求的人不能自行停用），SSO 可選擇是否一併套用。啟用時產生 10 組只能用一次的復原碼（只存雜湊），管理員可以重設某人的雙因素驗證（唯一的管理員可從主機的指令列重設）；同一組驗證碼不能用第二次。密碼或驗證碼連續錯 5 次鎖定 15 分鐘並寫進稽核；登入、驗證碼與換發權杖每個來源 IP 每分鐘最多 10 次。登入建立伺服器端工作階段：更新權杖只放在 HttpOnly Cookie、每次換發都換新，已經換掉的又被使用就撤銷整個工作階段並通知管理員與本人；存取權杖 15 分鐘到期並綁定工作階段，登出、強制登出、停用帳號與管理員重設密碼都立即生效，改密碼會登出其他裝置；使用者可以檢視並登出自己的裝置。每個請求都重新讀取帳號狀態與權限。",
       "Local and LDAP accounts can turn on TOTP two-factor authentication; administrators can require it for administrators or for everyone (people who have not set it up do so at sign-in and are not locked out; people who are required cannot turn it off), optionally including SSO. Turning it on creates 10 single-use recovery codes (stored hashed), and administrators can reset someone's two-factor authentication (the only administrator can be reset from the host's command line); the same code cannot be used twice. Five wrong passwords or codes in a row lock the account for 15 minutes and are audited; sign-in, codes and token refresh are limited to 10 per minute per source IP. Signing in creates a server-side session: the refresh token lives only in an HttpOnly cookie and changes on every refresh, and using a replaced one again revokes the whole session and notifies administrators and the user; access tokens expire after 15 minutes and are bound to the session, so signing out, being signed out by an administrator, deactivation and an administrator password reset take effect immediately, and changing your password signs out your other devices; users can see and sign out their own devices. Every request re-reads the account status and permissions.",
       "ローカルと LDAP のアカウントは TOTP の二要素認証を有効にできます。管理者は管理者または全員に必須とすることができ（未設定のユーザーはログイン時に設定を済ませるため締め出されず、必須のユーザーは自分で無効にできません）、SSO にも適用するかを選べます。有効にすると 1 回限りのリカバリーコードを 10 個生成し（ハッシュのみ保存）、管理者は特定ユーザーの二要素認証をリセットできます（管理者が 1 人だけならホストのコマンドラインからリセット）。同じコードは 2 回使えません。パスワードまたはコードを 5 回続けて間違えると 15 分間ロックされ監査に記録されます。ログイン・コード・トークン更新は送信元 IP ごとに毎分 10 回までです。ログインするとサーバー側のセッションを作成します。リフレッシュトークンは HttpOnly Cookie にのみ置き、更新のたびに入れ替わります。入れ替え済みのものが再び使われるとセッション全体を取り消し、管理者と本人に通知します。アクセストークンは 15 分で期限切れになりセッションに紐付くため、ログアウト・強制ログアウト・アカウントの無効化・管理者によるパスワードのリセットは即時に有効で、パスワードを変更すると他のデバイスはログアウトされます。ユーザーは自分のデバイスを確認しログアウトできます。リクエストごとにアカウントの状態と権限を読み直します。"),
     "`backend/tests/test_mfa_policy.py`, `test_session_revocation.py`, `test_security_alerts.py`, `test_rate_limit_switch.py`; `frontend/src/api/__tests__/tokenStorage.test.ts`"),
    (T("物件權限與跨單位隔離", "Object permissions and tenant isolation", "オブジェクト権限と組織間の分離"),
     T("依單位、區段、子網路、IP、裝置、機櫃、地點授權，可往下繼承。搜尋、IP 關聯、拓樸圖、AI 對話的工具、MCP 工具與 REST 用同一套可見範圍；重疊網段依子網路比對，不會對到其他單位的同一個位址；看不到的物件回應不存在（404）。只被授權部分物件的帳號在拓樸圖上只看得到自己範圍內的裝置與子網路，VPN、虛擬機等全域資料只給有全域讀取權限的帳號。",
       "Permissions are granted per unit, section, subnet, IP, device, rack and location, and inherit downwards. Search, IP relations, the topology graph, AI chat tools, MCP tools and REST share the same visibility; overlapping subnets are matched per subnet, so the same address in another unit is never reached; objects you cannot see answer as not found (404). Accounts granted only some objects see only their own devices and subnets on the topology graph; global data such as VPNs and virtual machines is shown only to accounts with global read.",
       "組織・セクション・サブネット・IP・機器・ラック・拠点ごとに権限を付与でき、下位に継承されます。検索・IP の関連・トポロジー図・AI チャットのツール・MCP ツール・REST は同じ閲覧範囲を使います。重複するネットワークはサブネット単位で照合するため、別組織の同じアドレスに届くことはありません。見えないオブジェクトは存在しない（404）と応答します。一部のオブジェクトだけを許可されたアカウントは、トポロジー図で自分の範囲の機器とサブネットだけが見え、VPN や仮想マシンなどのグローバルなデータは全体の読み取り権限を持つアカウントだけに表示されます。"),
     "`test_mcp_tools_match_rest_permissions.py`, `test_rbac_enforcement.py`, `test_rbac_detail_idor.py`, `test_search_overlapping.py`, `test_semantic_search_scope.py`, `test_topology_scope.py`"),
    (T("API 權杖與 MCP", "API tokens and MCP", "API トークンと MCP"),
     T("個人 API 權杖有效期 1 到 365 天（預設 90 天），可隨時撤銷並留下稽核，資料庫只存雜湊；唯讀權杖不能呼叫會異動資料的方法；每把權杖每分鐘最多 600 次（REST 與 MCP 合計）；帳號停用時權杖一併撤銷。外部 MCP 預設關閉，使用同一套權杖，或可輪替的唯讀 MCP 金鑰（有效 30 到 365 天）。API 權杖、MCP 金鑰與公開端點權杖在到期前 14、7、1 天與當天通知擁有者或管理員。",
       "Personal API tokens last 1 to 365 days (90 by default), can be revoked at any time with an audit record, and only their hash is stored; read-only tokens cannot call methods that change data; each token is limited to 600 requests a minute (REST and MCP combined); deactivating an account revokes its tokens. External MCP is off by default and uses the same tokens or a rotatable read-only MCP key (valid for 30 to 365 days). API tokens, the MCP key and public endpoint tokens notify their owner or administrators 14, 7 and 1 days before expiry and on the day.",
       "個人の API トークンの有効期間は 1〜365 日（既定 90 日）で、いつでも取り消せて監査に記録され、データベースにはハッシュだけを保存します。読み取り専用トークンはデータを変更するメソッドを呼べません。トークンごとに毎分 600 回まで（REST と MCP の合計）で、アカウントを無効化するとトークンも取り消されます。外部 MCP は既定で無効で、同じトークンか、ローテーションできる読み取り専用 MCP キー（有効期間 30〜365 日）を使います。API トークン・MCP キー・公開エンドポイントのトークンは、期限の 14・7・1 日前と当日に所有者または管理者へ通知します。"),
     "`test_api_token_scope.py`, `test_api_token_mcp_hardening.py`, `test_session_revocation.py`, `test_mcp_transport.py`"),
    (T("機密資料加密", "Secrets at rest", "機密情報の暗号化"),
     T("整合的密碼、API 金鑰、公開端點的權杖等以 AES-256-GCM 加密存放，每一處加解密都綁定用途（密文搬到別的欄位就解不開，守門測試逐一檢查），SSH 帳密使用每筆獨立的資料金鑰；加密金鑰在主機的 backend.env，不放在資料庫。設定頁不直接回傳權杖與金鑰，要檢視時另外取回並留稽核。系統匯出檔以使用者設定的密碼加密（scrypt 加 AES-256-GCM）。",
       "Integration passwords, API keys, public endpoint tokens and similar secrets are stored encrypted with AES-256-GCM, every encryption is bound to its purpose (ciphertext moved to another field fails to decrypt, and a guard test checks every call), and SSH credentials use a separate data key per record; the encryption key lives in backend.env on the host, not in the database. Settings pages do not return tokens and keys; viewing one fetches it separately and is audited. System export files are encrypted with a password you set (scrypt plus AES-256-GCM).",
       "連携のパスワード・API キー・公開エンドポイントのトークンなどは AES-256-GCM で暗号化して保存し、すべての暗号化を用途に紐付けています（暗号文を別の項目に移すと復号できず、ガードテストで 1 つずつ確認）。SSH の資格情報はレコードごとに別のデータキーを使います。暗号鍵はホストの backend.env にあり、データベースには置きません。設定ページはトークンやキーを返さず、表示するときは別途取得して監査に記録します。システムのエクスポートファイルは設定したパスワードで暗号化します（scrypt と AES-256-GCM）。"),
     "`test_security.py`, `test_secret_aad.py`, `test_encryption_key_format.py`, `test_public_endpoint_tokens.py`, `test_system_transfer.py`"),
    (T("傳輸加密", "Encryption in transit", "通信の暗号化"),
     T("網頁與 API 強制 HTTPS（nginx 反向代理或後端直接提供 TLS，二擇一；對外網址不是 https 時後端不會啟動），nginx 設定為 TLS 1.2 與 1.3，並送出 HSTS。Graylog DSV 查表的明文 8088 埠要在設定頁明確開啟才服務（新安裝預設關閉），可限制允許的來源位址；網址上的權杖不寫進 nginx 存取記錄。",
       "The web UI and API require HTTPS (nginx reverse proxy or TLS served by the backend; the backend refuses to start when the public URL is not https); the nginx configuration uses TLS 1.2 and 1.3, and HSTS is sent. The plain-HTTP port 8088 for Graylog DSV lookups answers only after it is turned on in settings (off for new installs), allowed source addresses can be restricted, and tokens in URLs are not written to the nginx access log.",
       "Web 画面と API は HTTPS が必須です（nginx のリバースプロキシかバックエンドが直接 TLS を提供、いずれか一方。公開 URL が https でなければバックエンドは起動しません）。nginx の設定は TLS 1.2 と 1.3 で、HSTS を送ります。Graylog DSV ルックアップ用の平文 8088 番ポートは設定で明示的に有効にしたときだけ応答し（新規インストールでは無効）、許可する送信元を制限できます。URL のトークンは nginx のアクセスログに書き込みません。"),
     "`app/core/config.py`, `deploy/nginx/jt-ipam.conf`, `test_public_endpoint_tokens.py`"),
    (T("出站連線防護", "Outbound connection protection", "外向き接続の保護"),
     T("HTTP 類整合在建立連線的當下檢查目標位址並連到檢查過的位址，擋下本機、雲端中繼資料位址與 DNS rebinding；轉址時重新檢查，而且不把認證資訊帶到別的主機。其他協定（SSH、LDAP、SMTP、RADIUS、WinRM、DNS、syslog 轉送）連線前解析並檢查所有位址，擋雲端中繼資料、link-local 與多播；遠端主控台的目標另外不能是本機位址（守門測試檢查每一處連線都經過檢查）。",
       "HTTP-based integrations check the destination at connect time and connect to the checked address, blocking loopback, cloud metadata addresses and DNS rebinding; redirects are checked again and credentials are never carried to another host. Other protocols (SSH, LDAP, SMTP, RADIUS, WinRM, DNS, syslog forwarding) resolve and check every address before connecting, blocking cloud metadata, link-local and multicast; remote console targets also cannot be loopback (a guard test checks that every connection goes through the check).",
       "HTTP 系の連携は接続の時点で宛先を確認し、確認したアドレスに接続して、ローカル・クラウドのメタデータアドレス・DNS リバインディングを防ぎます。リダイレクト時も再確認し、認証情報を別のホストへ持ち込みません。その他のプロトコル（SSH・LDAP・SMTP・RADIUS・WinRM・DNS・syslog 転送）は接続前にすべてのアドレスを解決して確認し、クラウドのメタデータ・リンクローカル・マルチキャストを防ぎます。リモートコンソールの宛先はさらにローカルアドレスも不可です（ガードテストですべての接続が確認を通ることを検査）。"),
     "`test_safe_http_guard.py`, `test_netdiag_http_guard.py`, `test_net_guard.py`"),
    (T("稽核記錄", "Audit trail", "監査ログ"),
     T("會異動資料的端點都要寫稽核，下載匯出檔與檢視金鑰、權杖也要寫（兩道守門測試逐一檢查，例外要寫明理由）；瀏覽器產生的表格與報告匯出由瀏覽器回報一筆。稽核記錄以雜湊鏈串接，資料庫層拒絕修改、刪除與清空；安裝與升級把稽核表交給不能登入的資料庫角色，應用程式帳號只能讀取與新增，無法停用這些保護。定期驗證並把鏈尾錨定到資料庫以外（檔案與系統日誌）；設定轉送到 Graylog 時，每筆記錄帶著自己與前一筆的雜湊，錨定點也一併送出。",
       "Every endpoint that changes data must write an audit record, and so must downloads of exports and views of keys and tokens (two guard tests check each one, and exceptions must state a reason); table and report exports generated in the browser are reported by the browser. Audit records form a hash chain and the database rejects updates, deletes and truncation; install and upgrade give the audit table to a database role that cannot sign in, so the app's own database account can only read and append and cannot disable these protections. The chain is verified regularly and its tail anchored outside the database (a file and the system journal); with forwarding to Graylog, every record carries its own hash and the previous one, and the anchors are forwarded too.",
       "データを変更するエンドポイントは監査への記録が必須で、エクスポートのダウンロードとキーやトークンの表示も同様です（2 つのガードテストで 1 つずつ確認し、例外には理由の記載が必要）。ブラウザで生成する表やレポートのエクスポートはブラウザが報告します。監査ログはハッシュチェーンでつながり、データベースは変更・削除・TRUNCATE を拒否します。インストールとアップグレードで監査テーブルをログインできないデータベースロールに渡すため、アプリのデータベースアカウントは読み取りと追加しかできず、これらの保護を無効にできません。定期的に検証し、チェーンの末尾をデータベースの外（ファイルとシステムジャーナル）にアンカーします。Graylog への転送を設定すると、各レコードが自分と直前のハッシュを持ち、アンカーも一緒に転送されます。"),
     "`test_audit_coverage.py`, `test_audit_read_coverage.py`, `test_audit_immutability.py`, `test_audit_hardening.py`, `test_audit_anchor.py`, `test_audit_chain_order.py`; `scripts/sql/audit-harden.sql`"),
    (T("遠端主控台", "Remote console", "リモートコンソール"),
     T("看得到 IP 不等於能連線：該 IP 要開啟對應的主控台功能，而且使用者是管理員、對子網路有寫入權，或被明確授予主控台權限；目標不能是本機、link-local 或保留位址。連線使用 60 秒內有效的單次票證（綁定使用者、IP 與工作階段），連線期間每 30 秒重新檢查：帳號被停用、被強制登出、登出那個工作階段或被收回權限時立即中斷並寫稽核。SFTP 的開啟、上傳、下載、改名、刪除都寫稽核，每個主控台連線結束時記錄連線時間。",
       "Seeing an IP does not mean being able to connect: the console must be enabled on that IP, and the user must be an admin, have write access to the subnet, or be granted console permission explicitly; the target cannot be a loopback, link-local or reserved address. Connections use a single-use ticket valid for 60 seconds and bound to the user, IP and session, and open connections are re-checked every 30 seconds: when the account is deactivated or signed out by an administrator, the session that opened it ends, or the permission is withdrawn, the connection closes and is audited. SFTP open, upload, download, rename and delete are audited, and every console session records its duration when it closes.",
       "IP が見えることは接続できることを意味しません。その IP でコンソールを有効にし、さらにユーザーが管理者・サブネットへの書き込み権限を持つ・コンソール権限を明示的に付与されている、のいずれかが必要で、宛先はローカル・リンクローカル・予約済みアドレスにできません。接続には 60 秒以内有効な 1 回限りのチケット（ユーザー・IP・セッションに紐付け）を使い、接続中は 30 秒ごとに再確認します。アカウントの無効化・強制ログアウト・そのセッションのログアウト・権限の取り消しがあると直ちに切断し、監査に記録します。SFTP のオープン・アップロード・ダウンロード・名前変更・削除は監査に記録され、各コンソールの終了時に接続時間を記録します。"),
     "`test_ticket_take_once.py`, `test_console_guard.py`, `test_net_guard.py`, `test_sftp_permissions.py`, `test_console_session_duration.py`"),
    (T("憑證與私鑰派送", "Certificates and private keys", "証明書と秘密鍵の配布"),
     T("每個憑證代理只拿得到指派給它的憑證；代理每次下載、管理員每次匯出私鑰都寫稽核；代理金鑰可輪替、停用或刪除。",
       "Each certificate agent can fetch only the certificates assigned to it; every agent download and every admin export of a private key is audited; agent keys can be rotated, disabled or deleted.",
       "各証明書エージェントは割り当てられた証明書だけを取得できます。エージェントのダウンロードと管理者による秘密鍵のエクスポートは毎回監査に記録されます。エージェントのキーはローテーション・無効化・削除ができます。"),
     "`test_cert_agents_api.py`"),
    (T("掃描代理", "Scan agents", "スキャンエージェント"),
     T("代理只會收到指派給它的子網路去掃描，回報也只在指派的子網路內比對（沒有指派任何子網路的代理不能更新任何 IP）；每個代理一把金鑰（只存雜湊），可輪替、停用或刪除；掃描、憑證與 RustDesk 代理的端點逐代理限流；同一台代理的回報排隊處理，單次回報筆數有上限。",
       "An agent receives only the subnets assigned to it for scanning, and its reports are matched only within those subnets (an agent with no assigned subnets cannot update any IP); each agent has its own key (only the hash is stored) that can be rotated, disabled or deleted; the scan, certificate and RustDesk agent endpoints are rate limited per agent; reports from one agent are processed one at a time and the size of a report is capped.",
       "エージェントには割り当てられたサブネットだけがスキャン対象として渡され、報告もそのサブネット内でだけ照合します（サブネットが割り当てられていないエージェントはどの IP も更新できません）。エージェントごとに 1 つのキー（ハッシュのみ保存）があり、ローテーション・無効化・削除ができます。スキャン・証明書・RustDesk エージェントのエンドポイントはエージェントごとにレート制限され、同じエージェントの報告は順番に処理され、1 回の報告件数には上限があります。"),
     "`test_scan_agent_scope.py`, `test_scan_agent_report_concurrency.py`, `test_scan_agent_load.py`"),
    (T("資料來源與時間", "Data provenance", "データの出所と時刻"),
     T("每個 IP 的主機名稱、MAC、ARP 觀測都記錄來源與觀測時間，欄位異動有異動記錄（誰、哪個來源、舊值與新值）；手動輸入的值依來源優先序或釘選保留（預設手動最優先），不會被自動同步覆蓋；異常偵測標出疑似讀壞的資料與可信度。",
       "Host names, MACs and ARP observations of each IP keep their source and observation time, and field changes are logged (who, which source, old and new value); manually entered values are kept by the source order or a pin (manual ranks first by default) and are not overwritten by automatic sync; anomaly detection marks likely misread data and gives a confidence.",
       "各 IP のホスト名・MAC・ARP の観測は出所と観測時刻を記録し、項目の変更は変更履歴（誰が・どの出所・旧値と新値）に残ります。手動入力の値は出所の優先順位または固定で保持され（既定では手動が最優先）、自動同期で上書きされません。異常検出は読み取り破損の疑いがあるデータと信頼度を示します。"),
     "`test_hostname_sources.py`, `test_ip_edit_manual_sources.py`, `test_evidence_contract.py`, `test_anomaly_arp_quality.py`"),
    (T("匯出與對外嵌入", "Exports and embedding", "エクスポートと外部埋め込み"),
     T("表格匯出只包含使用者看得到的資料，每次匯出都留稽核（伺服器產生的由伺服器記，瀏覽器產生的由瀏覽器回報）；系統匯出限管理員，並以密碼加密；機櫃圖對外嵌入需要系統權杖，而且要逐一開啟每個機櫃；機櫃嵌入與 Graylog DSV 的權杖會到期（30 到 365 天）。",
       "Table exports contain only data the user can see, and every export is audited (by the server for files it generates, reported by the browser for files generated there); system export is admin-only and password-encrypted; embedding a rack diagram elsewhere needs a system token and must be enabled per rack; the rack embed and Graylog DSV tokens expire (30 to 365 days).",
       "表のエクスポートにはユーザーが見られるデータだけが含まれ、エクスポートは毎回監査に記録されます（サーバーが生成するものはサーバーが、ブラウザで生成するものはブラウザが報告）。システムのエクスポートは管理者限定でパスワードで暗号化されます。ラック図の外部埋め込みにはシステムトークンが必要で、ラックごとに有効化します。ラック埋め込みと Graylog DSV のトークンには期限があります（30〜365 日）。"),
     "`test_audit_read_coverage.py`, `frontend/src/utils/__tests__/saveFileOnly.test.ts`, `test_rack_embed.py`, `test_public_endpoint_tokens.py`, `test_system_transfer.py`"),
    (T("備份與還原", "Backup and restore", "バックアップと復元"),
     T("每天自動備份資料庫、設定檔（含加密金鑰）、TLS 憑證與上傳的檔案，保留 14 天；設定備份密碼後，每天的備份整包加密成一個檔案（scrypt 加 AES-256-GCM 分塊加密，截斷、調換或修改都解不開），明文刪除，解密工具只需要 Python 與 cryptography。系統診斷顯示最近一次備份的結果與是否加密；還原步驟寫在安裝維運手冊。",
       "The database, configuration (including the encryption key), TLS certificates and uploaded files are backed up daily and kept for 14 days; with a backup passphrase set, each day's backup is encrypted into one file (scrypt plus chunked AES-256-GCM, so a truncated, reordered or modified file fails to decrypt) and the plain copy is removed, and the decrypt tool needs only Python and cryptography. The system diagnostics show the result of the last backup and whether it was encrypted; restore steps are in the installation and operations guide.",
       "データベース・設定ファイル（暗号鍵を含む）・TLS 証明書・アップロードファイルを毎日自動でバックアップし、14 日間保持します。バックアップのパスフレーズを設定すると、毎日のバックアップを 1 つのファイルに暗号化し（scrypt と AES-256-GCM のチャンク暗号化。切り詰め・入れ替え・改ざんがあると復号できません）、平文は削除します。復号ツールは Python と cryptography だけで動きます。システム診断に直近のバックアップ結果と暗号化の有無が表示されます。復元手順はインストール・運用ガイドにあります。"),
     "`test_backup_script.py`, `test_backup_encryption.py`, `test_self_check_backup.py`, `docs/INSTALL.md`"),
    (T("安全開發與弱點管理", "Secure development and vulnerability management", "セキュアな開発と脆弱性管理"),
     T("每次推送由 CI 執行 bandit 安全規則、pip-audit、pnpm audit，以及對著實際 nginx 設定與正式建置前端的 OWASP ZAP 基準掃描（基準檔以外的任何警示都讓 CI 失敗）；GitHub 程式碼掃描（CodeQL）的警示逐項判讀；發版前另跑登入後的 ZAP 掃描、在乾淨系統實測全新安裝與升級；每次改動對應的檢查項目與守門測試寫在測試清單。",
       "On every push CI runs bandit security rules, pip-audit, pnpm audit and an OWASP ZAP baseline scan of the production frontend build behind the shipped nginx configuration (any alert outside the baseline file fails CI); GitHub code scanning (CodeQL) alerts are triaged one by one; before a release an authenticated ZAP scan runs and fresh installs and upgrades are tested on clean systems; the checks and guard tests for each change are recorded in the test checklist.",
       "プッシュのたびに CI が bandit のセキュリティルール・pip-audit・pnpm audit と、出荷する nginx 設定の後ろで本番ビルドのフロントエンドに対する OWASP ZAP のベースラインスキャンを実行します（ベースラインファイル以外のアラートは CI を失敗させます）。GitHub のコードスキャン（CodeQL）のアラートは 1 件ずつ判定します。リリース前にはログイン後の ZAP スキャンを実行し、クリーンな環境で新規インストールとアップグレードを検証します。変更ごとの確認項目とガードテストはテストチェックリストに記録しています。"),
     "`.github/workflows/ci.yml`, `deploy/zap-baseline.conf`, `TEST_CHECKLIST.md`, `scripts/test-fresh-install.sh`, `scripts/test-upgrade.sh`"),
]

H_FEAT = T("AI 功能", "AI feature", "AI 機能")
H_USE = T("用途", "Purpose", "用途")
H_DEF = T("預設", "Default", "既定")
ON_LLM = T("啟用 LLM 後可用", "Available once the LLM is enabled", "LLM を有効にすると利用可")
OFF = T("關閉", "Off", "無効")
AI_INVENTORY = [
    (T("AI 對話", "AI chat", "AI チャット"),
     T("用自然語言查詢 IPAM，模型透過工具取得資料", "Query the IPAM in natural language; the model reads data through tools", "自然言語で IPAM を照会し、モデルはツールでデータを取得"), ON_LLM),
    (T("語意搜尋", "Semantic search", "セマンティック検索"),
     T("以嵌入模型做相似度搜尋", "Similarity search with an embedding model", "埋め込みモデルによる類似検索"), ON_LLM),
    (T("IP 調查判讀、IP 研判卡", "IP investigation narrative, IP triage card", "IP 調査の解説・IP トリアージカード"),
     T("把已查到的事實整理成說明", "Turn facts already collected into an explanation", "収集済みの事実を説明にまとめる"), ON_LLM),
    (T("防火牆規則異動判讀", "Firewall rule change review", "ファイアウォールルール変更の解説"),
     T("解釋程式比對出的規則差異", "Explain the rule differences found by the program", "プログラムが比較したルールの差分を解説"), ON_LLM),
    (T("IP 變更評估的 AI 建議", "AI suggestions in IP change assessment", "IP 変更評価の AI 提案"),
     T("依評估結果提出待辦建議", "Suggest to-dos from the assessment result", "評価結果から作業項目を提案"), ON_LLM),
    (T("AI 巡檢", "AI review", "AI 点検"),
     T("排程檢視資料並列出發現", "Review data on a schedule and list findings", "スケジュールでデータを確認し発見事項を一覧化"), OFF),
    (T("外部 MCP", "External MCP", "外部 MCP"),
     T("讓外部 AI 用戶端呼叫 jt-ipam 的工具", "Let external AI clients call jt-ipam tools", "外部の AI クライアントが jt-ipam のツールを呼び出す"), OFF),
]
AI_INV_NOTE = T("模型端點可以是自架的 Ollama，或 OpenAI 相容的 API（由管理員選擇）。LLM 未啟用時，以上功能都不會呼叫模型；AI 巡檢與外部 MCP 另有各自的開關。異常偵測、規則異動偵測與 IP 變更評估的結果由程式查詢算出，不是模型產生的。",
                "The model endpoint can be a self-hosted Ollama or an OpenAI-compatible API (chosen by an admin). While the LLM is disabled none of the features above call a model; AI review and external MCP have their own switches. The results of anomaly detection, rule change detection and IP change assessment are computed by queries, not produced by a model.",
                "モデルのエンドポイントは自前の Ollama か OpenAI 互換 API（管理者が選択）です。LLM が無効の間、上記の機能はモデルを呼び出しません。AI 点検と外部 MCP には個別のスイッチがあります。異常検出・ルール変更検出・IP 変更評価の結果はプログラムの照会で算出され、モデルが生成したものではありません。")

ISO42001 = [
    (T("AI 權限一致", "Consistent AI permissions", "AI の権限の一貫性"),
     T("AI 對話與 MCP 的工具在後端逐一授權，用的是與 REST 相同的可見範圍；會異動資料的工具限管理員。",
       "AI chat and MCP tools are authorised on the server one by one, using the same visibility as REST; tools that change data are admin-only.",
       "AI チャットと MCP のツールはサーバー側で 1 つずつ認可され、REST と同じ閲覧範囲を使います。データを変更するツールは管理者限定です。"),
     "`test_mcp_tools_match_rest_permissions.py`, `test_mcp_admin_tools.py`, `test_mcp_rbac_scope.py`"),
    (T("異動要人確認", "Changes need a person to confirm", "変更には人の確認が必要"),
     T("AI 對話裡會改資料的工具不會直接執行，要使用者按下確認才執行並寫稽核；MCP 的寫入也寫稽核。",
       "In AI chat, tools that change data never run directly: they run only after the user confirms, and are audited; MCP writes are audited too.",
       "AI チャットでデータを変更するツールは直接実行されず、ユーザーが確認して初めて実行され監査に記録されます。MCP の書き込みも監査に記録されます。"),
     "`test_mcp_url_and_audit.py`, `test_ai_inline_toolcalls.py`"),
    (T("查詢範圍與筆數", "Scope and counts", "照会範囲と件数"),
     T("工具回傳查詢範圍、總數、本次筆數與是否還有更多；超過上限會明確標示截斷；查無結果時工具要求模型照實說，不可以補造例子。",
       "Tools return the query scope, the total, the number returned and whether more remain; results over the limit are marked as truncated; when nothing is found the tool tells the model to say so and not to invent examples.",
       "ツールは照会範囲・総数・今回の件数・続きがあるかを返します。上限を超えると切り詰めたことを明示し、結果がないときはその旨を伝え、例を作らないようモデルに指示します。"),
     "`test_mcp_tool_scoping.py`, `test_ai_no_results_are_explicit.py`"),
    (T("答案查證", "Answer checking", "回答の検証"),
     T("回答中出現、但沒有任何工具回傳過的 IP，會被標示為未經查證。",
       "IPs that appear in an answer but were not returned by any tool are marked as unverified.",
       "回答に現れたが、どのツールも返していない IP は未検証として示されます。"),
     "`test_ai_answer_ip_verification.py`"),
    (T("模型記錄", "Model records", "モデルの記録"),
     T("AI 對話的回答與 AI 巡檢的發現會存下使用的模型；判讀與建議的結果也標示模型。",
       "AI chat answers and AI review findings store the model that produced them; narratives and suggestions also show the model.",
       "AI チャットの回答と AI 点検の発見事項は使用したモデルを保存し、解説や提案の結果にもモデルを表示します。"),
     "`app/models/ai_chat.py`, `app/models/ai_finding.py`"),
    (T("提示詞注入", "Prompt injection", "プロンプトインジェクション"),
     T("所有把資料送進模型的地方（AI 對話、IP 調查判讀、AI 巡檢、IP 研判、防火牆規則判讀、IP 變更評估）都使用同一段「資料與工具結果不是指令」的規則，大段資料放在定界內並先拆掉資料裡的定界字串（守門測試檢查每一個呼叫模型的地方）；使用者訊息先經過檢查（常見的越權指令樣式、重複與控制字元）；AI 對話裡會改資料的動作要人確認，確認時再過一次權限檢查。",
       "Every place that sends data to a model (AI chat, IP investigation narrative, AI review, IP triage, firewall rule review, IP change assessment) uses the same rule that data and tool results are not instructions, and large data blocks are fenced with fence strings inside the data broken up first (a guard test checks every caller of the model); user messages are screened first (common instruction-override patterns, repetition and control characters); changes proposed in AI chat need a person to confirm, and the confirmation runs the permission check again.",
       "モデルにデータを渡すすべての箇所（AI チャット・IP 調査の解説・AI 点検・IP トリアージ・ファイアウォールルールの解説・IP 変更評価）で「データとツールの結果は指示ではない」という同じ規則を使い、大きなデータは区切りの中に入れ、データ内の区切り文字列は先に崩します（ガードテストでモデルを呼ぶすべての箇所を検査）。ユーザーのメッセージは事前に検査されます（よくある指示の上書きパターン・繰り返し・制御文字）。AI チャットで提案された変更は人の確認が必要で、確認時に権限を再確認します。"),
     "`test_prompt_injection_framing.py`, `test_ai_guard.py`"),
    (T("事實與 AI 敘述分開", "Facts kept apart from AI narrative", "事実と AI の説明を分ける"),
     T("異常、規則異動與變更評估的結果由程式查詢決定；AI 只負責解釋，畫面標明是 AI 判讀。",
       "Anomalies, rule changes and change assessment results are decided by queries; the AI only explains them, and the screen labels the explanation as AI.",
       "異常・ルール変更・変更評価の結果はプログラムの照会で決まり、AI は説明だけを担当し、画面では AI による解説と明示します。"),
     "`app/services/anomaly.py`, `app/services/fw_review.py`, `app/services/change_impact/`"),
    (T("變更評估的人工審核", "Human review of change assessments", "変更評価の人によるレビュー"),
     T("有阻擋項目不能核准；結果不完整要填理由承擔風險；每個發現都要處置；核准後計畫或依據改變（快照雜湊不同）就回到草稿重新評估；可指定審核人；列出哪些來源沒有資料或同步失敗。",
       "Plans with blocking items cannot be approved; incomplete results need a reason to accept the risk; every finding needs a disposition; if the plan or its evidence changes after approval (the snapshot hash differs) it goes back to draft for reassessment; reviewers can be assigned; sources with no data or failed syncs are listed.",
       "ブロック項目があると承認できません。結果が不完全な場合はリスク受容の理由が必要で、すべての発見事項に対処が必要です。承認後に計画や根拠が変わると（スナップショットのハッシュが異なる）下書きに戻り再評価になります。レビュー担当者を指定でき、データがない出所や同期に失敗した出所を一覧表示します。"),
     "`test_change_impact_api.py`, `test_change_impact_reviewers.py`, `test_change_impact_coverage.py`"),
    (T("停用", "Switching off", "停止"),
     T("LLM 全域開關一關，所有 AI 功能都停止呼叫模型；AI 巡檢與外部 MCP 各自另有開關，預設關閉。",
       "Turning off the global LLM switch stops every AI feature from calling a model; AI review and external MCP each have their own switch and are off by default.",
       "LLM の全体スイッチを切ると、すべての AI 機能がモデルを呼び出さなくなります。AI 点検と外部 MCP には個別のスイッチがあり、既定では無効です。"),
     "`app/services/system_config.py`"),
]

RESP_27001 = [
    T("帳號：在使用者管理頁的「登入安全」把雙因素驗證設成管理員必須或所有人必須，並定期確認例外。",
      "Accounts: set two-factor authentication to required for administrators or for everyone under Sign-in security on the users page, and review exceptions regularly.",
      "アカウント：ユーザー管理ページの「ログインのセキュリティ」で 二要素認証を管理者必須または全員必須にし、例外を定期的に確認します。"),
    T("離職與調職：停用帳號（工作階段、API 權杖與開著的主控台會一併失效），或用「強制登出」結束所有登入；調整權限與群組成員。",
      "Leavers and movers: deactivate the account (its sessions, API tokens and open consoles stop working with it), or use Sign out everywhere to end all sign-ins; adjust permissions and group memberships.",
      "退職と異動：アカウントを無効化する（セッション・API トークン・開いているコンソールも同時に無効になります）か、「強制ログアウト」ですべてのログインを終了し、権限とグループのメンバーを調整します。"),
    T("存取審查：定期檢視權限指派、群組成員、API 權杖、主控台授權與登入中的工作階段，必要時對照稽核記錄。",
      "Access reviews: regularly review permission grants, group members, API tokens, console rights and active sessions, using the audit log where needed.",
      "アクセスレビュー：権限の付与・グループのメンバー・API トークン・コンソール権限・ログイン中のセッションを定期的に見直し、必要に応じて監査ログと照合します。"),
    T("整合帳號：每個整合使用專用的最小權限帳號，多數整合只需要唯讀；定期輪替密碼與金鑰；到期通知出現時及時更換權杖。",
      "Integration accounts: give each integration its own least-privilege account (most only need read access); rotate passwords and keys regularly; replace tokens when expiry notices arrive.",
      "連携用アカウント：連携ごとに最小権限の専用アカウントを使い（多くは読み取り専用で十分）、パスワードとキーを定期的にローテーションし、期限の通知が届いたらトークンを交換します。"),
    T("金鑰與備份：設定備份加密密碼並把密碼保存在 jt-ipam 主機以外；把備份放到異地儲存，定期做還原演練（含解密），並訂出 RPO 與 RTO。",
      "Keys and backups: set a backup encryption passphrase and keep it somewhere other than the jt-ipam host; store backups off-site, rehearse restores (including decryption) regularly, and set an RPO and RTO.",
      "鍵とバックアップ：バックアップの暗号化パスフレーズを設定し、jt-ipam のホスト以外に保管します。バックアップを外部に保存し、復号を含む復元訓練を定期的に行い、RPO と RTO を定めます。"),
    T("稽核證據：把稽核記錄轉送到獨立、不可改寫的 Graylog 或 SIEM 儲存，定期查看稽核鏈的驗證結果；還原備份或使用外部資料庫時執行 harden-audit。",
      "Audit evidence: forward audit records to independent, write-once Graylog or SIEM storage and review chain verification results regularly; run harden-audit after restoring a backup or when using an external database.",
      "監査の証拠：監査ログを独立した書き換え不可の Graylog または SIEM のストレージへ転送し、チェーンの検証結果を定期的に確認します。バックアップの復元後や外部データベースを使う場合は harden-audit を実行します。"),
    T("網路：只開放必要的連入埠；把 jt-ipam 放在管理網段；Graylog DSV 盡量走 HTTPS，必須用明文 8088 時限制允許的來源位址。",
      "Network: open only the inbound ports you need; keep jt-ipam in a management network; use HTTPS for Graylog DSV where possible, and restrict allowed sources when plain port 8088 is required.",
      "ネットワーク：必要な着信ポートだけを開け、jt-ipam を管理用ネットワークに置きます。Graylog DSV はできるだけ HTTPS を使い、平文の 8088 番が必要な場合は許可する送信元を制限します。"),
    T("掃描代理與憑證代理：只指派需要的子網路與憑證；不再使用的代理停用或刪除，定期輪替代理金鑰。",
      "Scan and certificate agents: assign only the subnets and certificates they need; disable or delete unused agents and rotate agent keys regularly.",
      "スキャンエージェントと証明書エージェント：必要なサブネットと証明書だけを割り当て、使わなくなったエージェントは無効化または削除し、エージェントのキーを定期的にローテーションします。"),
    T("更新：追蹤新版本並依 CHANGELOG 升級（升級腳本會先備份）。",
      "Updates: follow new releases and upgrade according to the CHANGELOG (the upgrade script backs up first).",
      "更新：新しいバージョンを追い、CHANGELOG に沿ってアップグレードします（アップグレードスクリプトは先にバックアップします）。"),
    T("管理制度：風險評鑑與處理、適用性聲明、教育訓練、內部稽核與管理審查由導入組織負責。",
      "Management system: risk assessment and treatment, the statement of applicability, training, internal audits and management reviews are the adopting organisation's responsibility.",
      "マネジメントシステム：リスクアセスメントとリスク対応・適用宣言書・教育訓練・内部監査・マネジメントレビューは導入組織の責任です。"),
]

RESP_42001 = [
    T("AI 政策與清冊：決定要開哪些 AI 功能、由誰負責，記錄模型端點與版本。",
      "AI policy and inventory: decide which AI features to turn on and who owns them, and record the model endpoint and version.",
      "AI ポリシーと一覧：どの AI 機能を有効にし、誰が責任を持つかを決め、モデルのエンドポイントとバージョンを記録します。"),
    T("資料流：選擇自架模型或外部 API；若開放外部 MCP，外部 AI 用戶端拿到的資料可能再交給它自己的雲端模型處理，這條資料流要另外評估與核准。",
      "Data flows: choose a self-hosted model or an external API; if you open external MCP, the data an external AI client receives may be passed on to its own cloud model, so assess and approve that flow separately.",
      "データの流れ：自前のモデルか外部 API かを選びます。外部 MCP を開放すると、外部の AI クライアントが受け取ったデータがそのクラウドモデルに渡される可能性があるため、この流れは別途評価・承認します。"),
    T("模型變更：換模型或調整參數前，用一組固定問題做回歸測試（工具使用、查詢範圍、查無結果時的回答）。",
      "Model changes: before switching models or changing parameters, run a fixed set of questions as a regression test (tool use, query scope, answers when nothing is found).",
      "モデルの変更：モデルの切り替えやパラメーター変更の前に、固定の質問セットで回帰テストを行います（ツールの使い方・照会範囲・結果がないときの回答）。"),
    T("人工覆核：AI 的回答與巡檢發現是建議，不是結論；網路變更走 IP 變更評估與審核流程。",
      "Human review: AI answers and review findings are suggestions, not conclusions; network changes go through IP change assessment and its review workflow.",
      "人によるレビュー：AI の回答と点検の発見事項は提案であり結論ではありません。ネットワークの変更は IP 変更評価とレビューの手順に従います。"),
    T("影響評估：評估錯誤判讀對人與組織的影響，例如誤把設備歸給某位使用者、錯誤的維護建議造成服務中斷。",
      "Impact assessment: assess how wrong conclusions affect people and the organisation, such as attributing a device to the wrong user or a wrong maintenance suggestion causing an outage.",
      "影響評価：誤った判断が人と組織に与える影響を評価します。例えば機器を誤ったユーザーに結び付ける、誤った保守の提案でサービスが停止する、などです。"),
    T("事件處理：訂出回報 AI 錯誤與暫停 AI 功能的程序（LLM 全域開關）。",
      "Incidents: define how AI mistakes are reported and how AI features are paused (the global LLM switch).",
      "インシデント対応：AI の誤りの報告手順と、AI 機能を一時停止する手順（LLM の全体スイッチ）を定めます。"),
    T("管理制度：適用性聲明、教育訓練、內部稽核與管理審查由導入組織負責。",
      "Management system: the statement of applicability, training, internal audits and management reviews are the adopting organisation's responsibility.",
      "マネジメントシステム：適用宣言書・教育訓練・内部監査・マネジメントレビューは導入組織の責任です。"),
]

H_CASE = T("驗收案例", "Acceptance case", "受け入れ確認")
H_PASS = T("合格判定", "Pass criterion", "合格条件")
CASES = [
    (T("A、B 單位使用相同的私有網段", "Units A and B use the same private network", "組織 A と B が同じプライベートネットワークを使う"),
     T("A 透過搜尋、IP 關聯、AI 對話與 MCP 都拿不到 B 的資料", "A cannot reach B's data through search, IP relations, AI chat or MCP", "A は検索・IP の関連・AI チャット・MCP のいずれでも B のデータを取得できない"),
     "`test_search_overlapping.py`, `test_mcp_rbac_scope.py`"),
    (T("唯讀權杖或一般帳號要求修改 IP", "A read-only token or ordinary account asks to change an IP", "読み取り専用トークンまたは一般アカウントが IP の変更を求める"),
     T("後端拒絕，資料沒有變更", "The server refuses and nothing changes", "サーバーが拒否し、データは変更されない"),
     "`test_api_token_scope.py`, `test_mcp_admin_tools.py`"),
    (T("查詢結果超過單次上限", "A query returns more than the limit", "照会結果が 1 回の上限を超える"),
     T("回傳總數與本次筆數，不把部分清單當成全部", "The total and the number returned are given; a partial list is not presented as complete", "総数と今回の件数を返し、一部の一覧を全体として扱わない"),
     "`test_mcp_tool_scoping.py`"),
    (T("查無資料", "Nothing is found", "データが見つからない"),
     T("AI 照實說沒有，不編造位址或設備", "The AI says so and does not invent addresses or devices", "AI はその旨を伝え、アドレスや機器を作らない"),
     "`test_ai_no_results_are_explicit.py`"),
    (T("核准後依據改變", "Evidence changes after approval", "承認後に根拠が変わる"),
     T("舊核准不能直接套用，要重新評估", "The earlier approval cannot be applied; the plan must be reassessed", "以前の承認はそのまま使えず、再評価が必要"),
     "`test_change_impact_api.py`"),
    (T("撤銷 API 權杖或停用帳號", "An API token is revoked or an account deactivated", "API トークンの取り消しまたはアカウントの無効化"),
     T("下一個 REST、API 權杖或 MCP 請求就被拒絕", "The next REST, API token or MCP request is refused", "次の REST・API トークン・MCP のリクエストから拒否される"),
     "`test_api_token_scope.py`, `app/api/v1/dependencies.py`"),
    (T("修改或刪除稽核記錄", "An audit record is modified or deleted", "監査ログを変更または削除する"),
     T("資料庫拒絕；鏈尾錨定能發現尾端被截掉", "The database refuses; the tail anchor reveals a truncated tail", "データベースが拒否し、末尾のアンカーで末尾の切り詰めを検出できる"),
     "`test_audit_immutability.py`, `test_audit_anchor.py`"),
    (T("重複使用主控台票證", "A console ticket is reused", "コンソールのチケットを再利用する"),
     T("第二次使用被拒絕", "The second use is refused", "2 回目の使用は拒否される"),
     "`test_ticket_take_once.py`"),
    (T("管理員要求所有人使用雙因素驗證", "Administrators require two-factor authentication for everyone", "管理者が全員に二要素認証を必須にする"),
     T("沒設定的人登入時先設定並拿到復原碼，不能自行停用；復原碼只能用一次", "People without it set it up at sign-in and receive recovery codes, and cannot turn it off; each recovery code works once", "未設定のユーザーはログイン時に設定してリカバリーコードを受け取り、自分で無効にできない。リカバリーコードは 1 回限り"),
     "`test_mfa_policy.py`"),
    (T("登出、強制登出或停用帳號", "Sign out, Sign out everywhere or deactivation", "ログアウト・強制ログアウト・アカウントの無効化"),
     T("存取權杖與更新權杖立即失效；開著的主控台 30 秒內中斷", "Access and refresh tokens stop working immediately; open consoles close within 30 seconds", "アクセストークンとリフレッシュトークンは即時に無効になり、開いているコンソールは 30 秒以内に切断される"),
     "`test_session_revocation.py`, `test_console_guard.py`"),
    (T("已經換掉的更新權杖又被使用", "A replaced refresh token is used again", "入れ替え済みのリフレッシュトークンが再び使われる"),
     T("整個工作階段撤銷，通知管理員與本人", "The whole session is revoked and administrators and the user are notified", "セッション全体が取り消され、管理者と本人に通知される"),
     "`test_session_revocation.py`"),
    (T("用應用程式的資料庫帳號停用稽核表的保護或清空稽核表", "The app's database account tries to disable the audit table protection or truncate it", "アプリのデータベースアカウントで監査テーブルの保護を無効化または TRUNCATE する"),
     T("資料庫拒絕", "The database refuses", "データベースが拒否する"),
     "`test_audit_hardening.py`, `scripts/sql/audit-harden.sql`"),
    (T("加密備份被截斷、調換或修改", "An encrypted backup is truncated, reordered or modified", "暗号化バックアップが切り詰め・入れ替え・改ざんされる"),
     T("解密失敗，不會還原出被動過的資料", "Decryption fails and no tampered data is restored", "復号に失敗し、改ざんされたデータは復元されない"),
     "`test_backup_encryption.py`"),
    (T("對 127.0.0.1 或 169.254.169.254 的 IP 開主控台", "A console is opened to an IP of 127.0.0.1 or 169.254.169.254", "127.0.0.1 または 169.254.169.254 の IP にコンソールを開く"),
     T("被拒絕並說明原因", "It is refused with the reason", "理由を示して拒否される"),
     "`test_net_guard.py`"),
    (T("主機名稱裡夾帶給模型的指令", "A host name carries instructions for the model", "ホスト名にモデル宛ての指示が含まれる"),
     T("提示詞把它標成資料並放在定界內，資料拆不掉定界", "The prompt marks it as data inside a fence that the data cannot close", "プロンプトはそれをデータとして区切りの中に入れ、データが区切りを閉じられない"),
     "`test_prompt_injection_framing.py`"),
    (T("憑證代理要求別的憑證", "A certificate agent asks for another certificate", "証明書エージェントが別の証明書を求める"),
     T("回應不存在，沒有內容外流", "It is answered as not found and nothing leaks", "存在しないと応答し、内容は漏れない"),
     "`test_cert_agents_api.py`"),
]
CASES_NOTE = T("右欄的測試在每次發版前執行；導入組織可以在自己的環境用同樣的情境驗收。",
               "The tests on the right run before every release; adopting organisations can run the same scenarios in their own environment.",
               "右欄のテストはリリースのたびに実行されます。導入組織は同じシナリオで自社環境の受け入れ確認ができます。")

# ─────────────────────── 對應的條文與控制項 ───────────────────────
# 控制項編號：ISO/IEC 27001:2022 附錄 A（與 ISO/IEC 27002:2022 相同）、ISO/IEC 42001:2023 附錄 A。
# 下面兩個集合是附錄 A 的完整編號（93 項、38 項），用來擋打錯或不存在的編號；名稱只列有用到的。
ANNEX_27001 = frozenset([f"A.5.{i}" for i in range(1, 38)] + [f"A.6.{i}" for i in range(1, 9)]
                        + [f"A.7.{i}" for i in range(1, 15)] + [f"A.8.{i}" for i in range(1, 35)])
ANNEX_42001 = frozenset(["A.2.2", "A.2.3", "A.2.4", "A.3.2", "A.3.3"] + [f"A.4.{i}" for i in range(2, 7)]
                        + [f"A.5.{i}" for i in range(2, 6)] + ["A.6.1.2", "A.6.1.3"]
                        + [f"A.6.2.{i}" for i in range(2, 9)] + [f"A.7.{i}" for i in range(2, 7)]
                        + [f"A.8.{i}" for i in range(2, 6)] + [f"A.9.{i}" for i in range(2, 5)]
                        + [f"A.10.{i}" for i in range(2, 5)])
assert len(ANNEX_27001) == 93 and len(ANNEX_42001) == 38

NAMES_27001 = {
    "6.1.2": T("資訊安全風險評鑑", "Information security risk assessment", "情報セキュリティリスクアセスメント"),
    "6.1.3": T("資訊安全風險處理（含適用性聲明）", "Information security risk treatment (including the statement of applicability)",
               "情報セキュリティリスク対応（適用宣言書を含む）"),
    "7.2": T("適任性", "Competence", "力量"),
    "7.3": T("認知", "Awareness", "認識"),
    "9.2": T("內部稽核", "Internal audit", "内部監査"),
    "9.3": T("管理階層審查", "Management review", "マネジメントレビュー"),
    "A.5.9": T("資訊及其他相關聯資產之清冊", "Inventory of information and other associated assets", "情報及びその他の関連資産の目録"),
    "A.5.14": T("資訊傳送", "Information transfer", "情報転送"),
    "A.5.15": T("存取控制", "Access control", "アクセス制御"),
    "A.5.16": T("身分管理", "Identity management", "識別情報の管理"),
    "A.5.17": T("鑑別資訊", "Authentication information", "認証情報"),
    "A.5.18": T("存取權限", "Access rights", "アクセス権"),
    "A.5.28": T("證據之蒐集", "Collection of evidence", "証拠の収集"),
    "A.5.30": T("營運持續之 ICT 備妥性", "ICT readiness for business continuity", "事業継続のための ICT の備え"),
    "A.5.33": T("紀錄之保護", "Protection of records", "記録の保護"),
    "A.6.5": T("聘用終止或變更後之責任", "Responsibilities after termination or change of employment", "雇用の終了又は変更後の責任"),
    "A.8.2": T("特殊存取權限", "Privileged access rights", "特権的アクセス権"),
    "A.8.3": T("資訊存取限制", "Information access restriction", "情報へのアクセス制限"),
    "A.8.5": T("安全鑑別", "Secure authentication", "セキュリティを保った認証"),
    "A.8.8": T("技術脆弱性之管理", "Management of technical vulnerabilities", "技術的ぜい弱性の管理"),
    "A.8.9": T("組態管理", "Configuration management", "構成管理"),
    "A.8.13": T("資訊備份", "Information backup", "情報のバックアップ"),
    "A.8.15": T("存錄（日誌）", "Logging", "ログ取得"),
    "A.8.16": T("監視活動", "Monitoring activities", "監視活動"),
    "A.8.20": T("網路安全", "Networks security", "ネットワークセキュリティ"),
    "A.8.22": T("網路區隔", "Segregation of networks", "ネットワークの分離"),
    "A.8.24": T("密碼技術之使用", "Use of cryptography", "暗号の利用"),
    "A.8.25": T("安全開發生命週期", "Secure development life cycle", "セキュリティに配慮した開発のライフサイクル"),
    "A.8.26": T("應用程式安全要求事項", "Application security requirements", "アプリケーションのセキュリティの要求事項"),
    "A.8.28": T("安全程式設計", "Secure coding", "セキュリティに配慮したコーディング"),
    "A.8.29": T("開發及驗收中之安全測試", "Security testing in development and acceptance", "開発及び受入れにおけるセキュリティ試験"),
    "A.8.32": T("變更管理", "Change management", "変更管理"),
}
NAMES_42001 = {
    "5.2": T("AI 政策", "AI policy", "AI 方針"),
    "6.1.3": T("AI 風險處理（含適用性聲明）", "AI risk treatment (including the statement of applicability)",
               "AI リスク対応（適用宣言書を含む）"),
    "6.1.4": T("AI 系統衝擊評鑑", "AI system impact assessment", "AI システム影響評価"),
    "7.2": T("適任性", "Competence", "力量"),
    "7.3": T("認知", "Awareness", "認識"),
    "8.4": T("AI 系統衝擊評鑑（運作）", "AI system impact assessment (operation)", "AI システム影響評価（運用）"),
    "9.2": T("內部稽核", "Internal audit", "内部監査"),
    "9.3": T("管理階層審查", "Management review", "マネジメントレビュー"),
    "A.2.2": T("AI 政策", "AI policy", "AI 方針"),
    "A.3.2": T("AI 角色與責任", "AI roles and responsibilities", "AI の役割及び責任"),
    "A.3.3": T("關切事項之通報", "Reporting of concerns", "懸念の報告"),
    "A.4.2": T("資源之文件化", "Resource documentation", "資源の文書化"),
    "A.5.2": T("AI 系統衝擊評鑑過程", "AI system impact assessment process", "AI システム影響評価プロセス"),
    "A.5.4": T("評鑑 AI 系統對個人或群體之衝擊", "Assessing AI system impact on individuals or groups of individuals",
               "個人又は個人の集団への AI システムの影響の評価"),
    "A.6.2.2": T("AI 系統要求事項與規格", "AI system requirements and specification", "AI システムの要求事項及び仕様"),
    "A.6.2.4": T("AI 系統之驗證與確認", "AI system verification and validation", "AI システムの検証及び妥当性確認"),
    "A.6.2.5": T("AI 系統部署", "AI system deployment", "AI システムの展開"),
    "A.6.2.6": T("AI 系統運作與監視", "AI system operation and monitoring", "AI システムの運用及び監視"),
    "A.6.2.8": T("AI 系統事件日誌之紀錄", "AI system recording of event logs", "AI システムのイベントログの記録"),
    "A.7.4": T("AI 系統資料之品質", "Quality of data for AI systems", "AI システムのためのデータの品質"),
    "A.8.2": T("系統文件及提供使用者之資訊", "System documentation and information for users", "システムの文書及び利用者への情報"),
    "A.8.4": T("事故之溝通", "Communication of incidents", "インシデントの伝達"),
    "A.9.2": T("負責任使用 AI 系統之過程", "Processes for responsible use of AI systems", "AI システムの責任ある利用のためのプロセス"),
    "A.9.4": T("AI 系統之預期用途", "Intended use of the AI system", "AI システムの意図した利用"),
    "A.10.3": T("供應者", "Suppliers", "供給者"),
}
# 依「面向」的英文名稱對應（jt-ipam 能提供佐證的控制項；是否達成由導入組織判定）
REFS_27001 = {
    "Sign-in, two-factor authentication and sessions": ["A.5.16", "A.5.17", "A.8.5"],
    "Object permissions and tenant isolation": ["A.5.15", "A.5.18", "A.8.2", "A.8.3"],
    "API tokens and MCP": ["A.5.17", "A.8.3", "A.8.5"],
    "Secrets at rest": ["A.5.17", "A.8.24"],
    "Encryption in transit": ["A.5.14", "A.8.24"],
    "Outbound connection protection": ["A.8.20", "A.8.26"],
    "Audit trail": ["A.5.28", "A.5.33", "A.8.15", "A.8.16"],
    "Remote console": ["A.5.15", "A.5.18", "A.8.2"],
    "Certificates and private keys": ["A.8.3", "A.8.24"],
    "Scan agents": ["A.5.16", "A.8.3"],
    "Data provenance": ["A.5.9", "A.8.9"],
    "Exports and embedding": ["A.5.14", "A.8.3"],
    "Backup and restore": ["A.8.13", "A.8.24"],
    "Secure development and vulnerability management": ["A.8.8", "A.8.25", "A.8.28", "A.8.29"],
}
REFS_42001 = {
    "Consistent AI permissions": ["A.9.2"],
    "Changes need a person to confirm": ["A.6.2.8", "A.9.2"],
    "Scope and counts": ["A.7.4", "A.8.2"],
    "Answer checking": ["A.6.2.6", "A.8.2"],
    "Model records": ["A.4.2", "A.6.2.8"],
    "Prompt injection": ["A.6.2.2", "A.6.2.4"],
    "Facts kept apart from AI narrative": ["A.8.2", "A.9.4"],
    "Human review of change assessments": ["A.9.2"],
    "Switching off": ["A.6.2.6", "A.9.2"],
}
AI_INV_REFS = ["A.4.2", "A.8.2"]
# 導入組織要做的事：與 RESP_27001／RESP_42001 同順序
RESP_REFS_27001 = [["A.5.17", "A.8.5"], ["A.5.18", "A.6.5"], ["A.5.18", "A.8.2"], ["A.5.17", "A.8.2"],
                   ["A.5.30", "A.8.13"], ["A.5.28", "A.8.15"], ["A.8.20", "A.8.22"], ["A.5.18"], ["A.8.8", "A.8.32"],
                   ["6.1.2", "6.1.3", "7.2", "7.3", "9.2", "9.3"]]
RESP_REFS_42001 = [["5.2", "A.2.2", "A.3.2", "A.4.2"], ["A.10.3"], ["A.6.2.4", "A.6.2.5"], ["A.9.2"],
                   ["6.1.4", "8.4", "A.5.2", "A.5.4"], ["A.3.3", "A.8.4"], ["6.1.3", "7.2", "7.3", "9.2", "9.3"]]

H_REF = T("對應控制項", "Related controls", "関連する管理策")
H_CODE = T("編號", "No.", "番号")
H_NAME = T("名稱", "Title", "名称")
H_BY_JT = T("jt-ipam 提供佐證的面向", "Where jt-ipam provides evidence", "jt-ipam が根拠を示す項目")
H_BY_ORG = T("導入組織要做的事", "What the organisation does", "導入組織が行うこと")
REF_NOTE_27001 = T(
    "編號依 ISO/IEC 27001:2022 附錄 A（與 ISO/IEC 27002:2022 相同），不帶 A. 的是本文條文；中文與日文名稱為參考譯名，以標準原文為準。"
    "jt-ipam 提供的是技術面的佐證，控制項是否達成由導入組織依風險評鑑與適用性聲明判定。附錄 A 共 93 項，jt-ipam 能提供佐證的有 {n} 項；下表另列出「導入組織要做的事」對應的條文與控制項，表中沒有的控制項由導入組織以其他方式處理。",
    "Numbers follow ISO/IEC 27001:2022 Annex A (the same as ISO/IEC 27002:2022); numbers without A. are clauses of the main text. "
    "jt-ipam provides technical evidence; whether a control is met is decided by the adopting organisation through its risk assessment "
    "and statement of applicability. Annex A has 93 controls; jt-ipam provides evidence for {n} of them. The table also lists the clauses and controls behind the organisation's responsibilities further down; controls not in the table are handled by the organisation by other means.",
    "番号は ISO/IEC 27001:2022 附属書 A（ISO/IEC 27002:2022 と同じ）に従い、A. の付かない番号は本文の箇条です。日本語と中国語の名称は参考訳で、規格の原文が優先します。"
    "jt-ipam が示すのは技術面の根拠で、管理策を満たすかどうかは導入組織がリスクアセスメントと適用宣言書に基づいて判断します。附属書 A の 93 項目のうち、jt-ipam が根拠を示せるのは {n} 項目です。下表には「導入組織が責任を持つこと」に対応する箇条と管理策も載せており、表にない管理策は導入組織が別の方法で対応します。")
REF_NOTE_42001 = T(
    "編號依 ISO/IEC 42001:2023 附錄 A，不帶 A. 的是本文條文；中文與日文名稱為參考譯名，以標準原文為準。附錄 A 共 38 項，jt-ipam 能提供佐證的有 {n} 項；下表另列出「導入組織要做的事」對應的條文與控制項。",
    "Numbers follow ISO/IEC 42001:2023 Annex A; numbers without A. are clauses of the main text. Annex A has 38 controls; jt-ipam provides evidence for {n} of them. The table also lists the clauses and controls behind "
    "the organisation's responsibilities further down.",
    "番号は ISO/IEC 42001:2023 附属書 A に従い、A. の付かない番号は本文の箇条です。日本語と中国語の名称は参考訳で、規格の原文が優先します。附属書 A の 38 項目のうち、jt-ipam が根拠を示せるのは {n} 項目です。下表には「導入組織が責任を持つこと」に対応する箇条と管理策も載せています。")


def _check_refs() -> None:
    """編號一定要是附錄 A 真的有的、名稱要有、每一列都要有對應（產生時就擋下來）。"""
    for rows, refs, names, annex in ((ISO27001, REFS_27001, NAMES_27001, ANNEX_27001),
                                     (ISO42001, REFS_42001, NAMES_42001, ANNEX_42001)):
        assert {a["en"] for a, _b, _e in rows} == set(refs), "每一列都要有對應的控制項（REFS_*）"
        for codes in refs.values():
            assert codes and all(c.startswith("A.") for c in codes), codes
    for codes, names, annex in [(c, NAMES_27001, ANNEX_27001) for c in [*REFS_27001.values(), *RESP_REFS_27001]] + \
                               [(c, NAMES_42001, ANNEX_42001) for c in [*REFS_42001.values(), *RESP_REFS_42001, AI_INV_REFS]]:
        for c in codes:
            assert c in names, f"缺名稱：{c}"
            assert (c in annex) if c.startswith("A.") else re.fullmatch(r"\d+(\.\d+)+", c), f"不存在的編號：{c}"
    assert len(RESP_REFS_27001) == len(RESP_27001) and len(RESP_REFS_42001) == len(RESP_42001)


def _sort_key(code: str) -> tuple[int, ...]:
    return tuple([1 if code.startswith("A.") else 0] + [int(x) for x in code.removeprefix("A.").split(".")])


def _codes(codes: list[str]) -> dict[str, str]:
    return T("、".join(codes), ", ".join(codes), "、".join(codes))


def _label(t: dict[str, str]) -> dict[str, str]:
    """「帳號：……」→「帳號」"""
    return {k: re.split(r"[：:]", v, maxsplit=1)[0] for k, v in t.items()}


def _index(rows, refs, resp, resp_refs, names, extra=()) -> list[tuple[str, dict[str, str], dict[str, str], dict[str, str]]]:
    by_jt: dict[str, list[dict[str, str]]] = {}
    for a, codes in [(a, refs[a["en"]]) for a, _b, _e in rows] + list(extra):
        for c in codes:
            by_jt.setdefault(c, []).append(a)
    by_org: dict[str, list[dict[str, str]]] = {}
    for item, codes in zip(resp, resp_refs):
        for c in codes:
            by_org.setdefault(c, []).append(_label(item))
    out = []
    for c in sorted(set(by_jt) | set(by_org), key=_sort_key):
        join = lambda xs, sep: sep.join(x for x in xs)  # noqa: E731
        jt = {L: join([x[L] for x in by_jt.get(c, [])], ", " if L == "en" else "、") for L in LANGS}
        org = {L: join([x[L] for x in by_org.get(c, [])], ", " if L == "en" else "、") for L in LANGS}
        out.append((c, names[c], jt, org))
    return out


def _n_annex(refs, extra=()) -> int:
    return len({c for codes in [*refs.values(), *extra] for c in codes})


def _resp(item: dict[str, str], codes: list[str]) -> dict[str, str]:
    return {L: item[L] + (f" ({', '.join(codes)})" if L == "en" else f"（{'、'.join(codes)}）") for L in LANGS}


def _with_n(t: dict[str, str], n: int) -> dict[str, str]:
    return {L: v.replace("{n}", str(n)) for L, v in t.items()}


INDEX_27001 = lambda: _index(ISO27001, REFS_27001, RESP_27001, RESP_REFS_27001, NAMES_27001)  # noqa: E731
INDEX_42001 = lambda: _index(ISO42001, REFS_42001, RESP_42001, RESP_REFS_42001, NAMES_42001,  # noqa: E731
                             extra=[(SECTIONS["ai-inventory"], AI_INV_REFS)])


TEMPLATE_COLS = T("項目編號 | 標準與管理面向 | 適用功能 | 狀態 | 程式碼或設定位置 | 驗收方式 | 測試證據 | 缺口 | 負責人 | 完成期限",
                  "Item ID | Standard and area | Feature | Status | Code or setting location | How verified | Test evidence | Gap | Owner | Due date",
                  "項目番号 | 規格と管理項目 | 対象機能 | 状態 | コードまたは設定の場所 | 確認方法 | テストの証拠 | 不足 | 担当者 | 期限")

SECTIONS = {
    "iso27001": T("ISO/IEC 27001：jt-ipam 提供的資訊安全控制", "ISO/IEC 27001: information security controls jt-ipam provides",
                  "ISO/IEC 27001：jt-ipam が提供する情報セキュリティの管理策"),
    "iso42001": T("ISO/IEC 42001：AI 功能與控制", "ISO/IEC 42001: AI features and controls", "ISO/IEC 42001：AI 機能と管理策"),
    "ai-inventory": T("AI 功能清冊", "AI feature inventory", "AI 機能の一覧"),
    "ai-controls": T("AI 控制", "AI controls", "AI の管理策"),
    "iso27001-index": T("對應的條文與控制項（ISO/IEC 27001）", "Clauses and controls (ISO/IEC 27001)", "対応する箇条と管理策（ISO/IEC 27001）"),
    "iso42001-index": T("對應的條文與控制項（ISO/IEC 42001）", "Clauses and controls (ISO/IEC 42001)", "対応する箇条と管理策（ISO/IEC 42001）"),
    "customer": T("導入組織要負責的事", "What the adopting organisation is responsible for", "導入組織が責任を持つこと"),
    "customer-27001": T("資訊安全（ISO/IEC 27001）", "Information security (ISO/IEC 27001)", "情報セキュリティ（ISO/IEC 27001）"),
    "customer-42001": T("AI 管理（ISO/IEC 42001）", "AI management (ISO/IEC 42001)", "AI マネジメント（ISO/IEC 42001）"),
    "acceptance": T("建議的驗收案例", "Suggested acceptance cases", "推奨する受け入れ確認"),
    "worksheet": T("盤點表欄位", "Assessment worksheet columns", "棚卸し表の列"),
}
WORKSHEET_NOTE = T("盤點時可以沿用以下欄位，一列一個控制項目：", "When assessing, these columns work well, one control per row:",
                   "棚卸しでは次の列を使えます。1 行に 1 つの管理策です：")


# ─────────────────────────── Markdown ───────────────────────────

def _md_cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def md(lang: str) -> str:
    L = lang
    out = [f"# {TITLE[L]}", ""]
    out += ["> " + T("本檔由 scripts/gen-compliance-docs.py 產生，請改那支腳本再重新產生。",
                     "Generated by scripts/gen-compliance-docs.py; edit that script and regenerate.",
                     "このファイルは scripts/gen-compliance-docs.py で生成しています。スクリプトを編集して再生成してください。")[L], ""]
    for p in INTRO:
        out += [p[L], ""]
    out += [f"## {SECTIONS['iso27001'][L]}", "", f"| {H_CTRL[L]} | {H_REF[L]} | {H_HOW[L]} | {H_EVID[L]} |", "|---|---|---|---|"]
    out += [f"| {_md_cell(a[L])} | {_codes(REFS_27001[a['en']])[L]} | {_md_cell(b[L])} | {_md_cell(e)} |" for a, b, e in ISO27001]
    out += ["", f"### {SECTIONS['iso27001-index'][L]}", "", _with_n(REF_NOTE_27001, _n_annex(REFS_27001))[L], "",
            f"| {H_CODE[L]} | {H_NAME[L]} | {H_BY_JT[L]} | {H_BY_ORG[L]} |", "|---|---|---|---|"]
    out += [f"| {c} | {_md_cell(n[L])} | {_md_cell(j[L])} | {_md_cell(o[L])} |" for c, n, j, o in INDEX_27001()]
    out += ["", f"## {SECTIONS['iso42001'][L]}", "", f"### {SECTIONS['ai-inventory'][L]}", "",
            f"| {H_FEAT[L]} | {H_USE[L]} | {H_DEF[L]} |", "|---|---|---|"]
    out += [f"| {_md_cell(a[L])} | {_md_cell(b[L])} | {_md_cell(c[L])} |" for a, b, c in AI_INVENTORY]
    out += ["", AI_INV_NOTE[L], "", f"### {SECTIONS['ai-controls'][L]}", "",
            f"| {H_CTRL[L]} | {H_REF[L]} | {H_HOW[L]} | {H_EVID[L]} |", "|---|---|---|---|"]
    out += [f"| {_md_cell(a[L])} | {_codes(REFS_42001[a['en']])[L]} | {_md_cell(b[L])} | {_md_cell(e)} |" for a, b, e in ISO42001]
    out += ["", f"### {SECTIONS['iso42001-index'][L]}", "",
            _with_n(REF_NOTE_42001, _n_annex(REFS_42001, [AI_INV_REFS]))[L], "",
            f"| {H_CODE[L]} | {H_NAME[L]} | {H_BY_JT[L]} | {H_BY_ORG[L]} |", "|---|---|---|---|"]
    out += [f"| {c} | {_md_cell(n[L])} | {_md_cell(j[L])} | {_md_cell(o[L])} |" for c, n, j, o in INDEX_42001()]
    out += ["", f"## {SECTIONS['customer'][L]}", "", f"### {SECTIONS['customer-27001'][L]}", ""]
    out += [f"- {_resp(x, r)[L]}" for x, r in zip(RESP_27001, RESP_REFS_27001)]
    out += ["", f"### {SECTIONS['customer-42001'][L]}", ""]
    out += [f"- {_resp(x, r)[L]}" for x, r in zip(RESP_42001, RESP_REFS_42001)]
    out += ["", f"## {SECTIONS['acceptance'][L]}", "", f"| {H_CASE[L]} | {H_PASS[L]} | {H_EVID[L]} |", "|---|---|---|"]
    out += [f"| {_md_cell(a[L])} | {_md_cell(b[L])} | {_md_cell(e)} |" for a, b, e in CASES]
    out += ["", CASES_NOTE[L], "", f"## {SECTIONS['worksheet'][L]}", "", WORKSHEET_NOTE[L], "", f"`{TEMPLATE_COLS[L]}`", ""]
    return "\n".join(out)


# ─────────────────────────── HTML ───────────────────────────

class Refs(tuple):
    """表格裡的「對應控制項」：HTML 一個編號一行（Markdown 用 _codes 以逗號連接）。"""


def _code(s: str) -> str:
    """`a`、`b` → <code>a</code>、<code>b</code>（其餘跳脫）。"""
    parts = re.split(r"(`[^`]+`)", s)
    return "".join(f"<code>{html.escape(p[1:-1])}</code>" if p.startswith("`") else html.escape(p) for p in parts)


def _tri(t: dict[str, str], *, code: bool = False) -> str:
    f = _code if code else html.escape
    return "".join(f'<span class="{lang}">{f(t[lang])}</span>' for lang in LANGS)


def _table(heads: tuple[dict[str, str], ...], rows: list[tuple[str, ...]], cls: str = "ct") -> str:
    th = "".join(f"<th>{_tri(h)}</th>" for h in heads)
    body = []
    for row in rows:
        cells = []
        for c in row:
            if isinstance(c, Refs):
                cells.append(f'<td class="refs">{html.escape(chr(10).join(c))}</td>')
                continue
            cells.append(f"<td>{_tri(c) if isinstance(c, dict) else _code(c)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'    <div class="tbl"><table class="{cls}">\n      <thead><tr>{th}</tr></thead>\n      <tbody>\n        '
            + "\n        ".join(body) + "\n      </tbody>\n    </table></div>")


def _h2(key: str) -> str:
    return f'    <h2 id="{key}">{_tri(SECTIONS[key])}</h2>'


def _h3(key: str) -> str:
    return f'    <h3 class="sub-h" id="{key}">{_tri(SECTIONS[key])}</h3>'


def html_body() -> str:
    toc_keys = ("iso27001", "iso42001", "customer", "acceptance", "worksheet")
    toc = "\n".join(f'    <li><a href="#{k}">{_tri(SECTIONS[k])}</a></li>' for k in toc_keys)
    parts = [
        f"  <h1>{_tri(TITLE)}</h1>",
        *[f'  <p class="sub">{_tri(p)}</p>' for p in INTRO],
        '  <ul class="toc">', toc, "  </ul>",
        '  <section class="blk">', _h2("iso27001"),
        _table((H_CTRL, H_REF, H_HOW, H_EVID), [(a, Refs(REFS_27001[a["en"]]), b, e) for a, b, e in ISO27001]),
        _h3("iso27001-index"), f'    <p class="note">{_tri(_with_n(REF_NOTE_27001, _n_annex(REFS_27001)))}</p>',
        _table((H_CODE, H_NAME, H_BY_JT, H_BY_ORG), INDEX_27001(), cls="ct idx"), "  </section>",
        '  <section class="blk">', _h2("iso42001"), _h3("ai-inventory"),
        _table((H_FEAT, H_USE, H_DEF), AI_INVENTORY),
        f'    <p class="note">{_tri(AI_INV_NOTE)}</p>', _h3("ai-controls"),
        _table((H_CTRL, H_REF, H_HOW, H_EVID), [(a, Refs(REFS_42001[a["en"]]), b, e) for a, b, e in ISO42001]),
        _h3("iso42001-index"),
        f'    <p class="note">{_tri(_with_n(REF_NOTE_42001, _n_annex(REFS_42001, [AI_INV_REFS])))}</p>',
        _table((H_CODE, H_NAME, H_BY_JT, H_BY_ORG), INDEX_42001(), cls="ct idx"), "  </section>",
        '  <section class="blk">', _h2("customer"), _h3("customer-27001"),
        '    <div class="box"><ul>' + "".join(f"<li>{_tri(_resp(x, r))}</li>" for x, r in zip(RESP_27001, RESP_REFS_27001)) + "</ul></div>",
        _h3("customer-42001"),
        '    <div class="box"><ul>' + "".join(f"<li>{_tri(_resp(x, r))}</li>" for x, r in zip(RESP_42001, RESP_REFS_42001)) + "</ul></div>",
        "  </section>",
        '  <section class="blk">', _h2("acceptance"), _table((H_CASE, H_PASS, H_EVID), CASES),
        f'    <p class="note">{_tri(CASES_NOTE)}</p>', "  </section>",
        '  <section class="blk">', _h2("worksheet"),
        f'    <div class="box"><p>{_tri(WORKSHEET_NOTE)}</p><p><code class="cols">{_tri(TEMPLATE_COLS)}</code></p></div>',
        "  </section>",
    ]
    return "\n".join(parts)


_EXTRA_CSS = """  /* 合規對照的表格：三欄，最後一欄是測試與設定位置（等寬字、小一號） */
  .tbl{overflow-x:auto;margin:6px 0 4px}
  table.ct{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;
    overflow:hidden;font-size:14px}
  table.ct th{background:#f4f8f5;color:var(--ink);text-align:left;padding:10px 12px;font-size:13px;white-space:nowrap}
  table.ct td{padding:10px 12px;border-top:1px solid var(--line);vertical-align:top}
  table.ct td:first-child{font-weight:600;color:var(--ink);white-space:nowrap}
  table.ct td:last-child{font-size:12.5px;color:var(--muted);word-break:break-word;min-width:180px}
  table.ct code,.cols{font:12.5px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  p.note{color:var(--muted);font-size:14px;margin:8px 0 0}
  /* 對應控制項欄：編號不折行；索引表的最後一欄（組織的責任）維持一般字型 */
  table.ct td:first-child{white-space:normal;width:17%;min-width:9em}
  table.ct td.refs{white-space:pre-line;width:6.5em;font:12.5px/1.7 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  table.idx td:first-child{font:600 13px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  table.idx td:last-child{font-size:14px;color:var(--ink)}
  @media (max-width:720px){ table.ct td:first-child{white-space:normal} }
"""


def _page_from_adoption() -> str:
    src = (DOCS / "adoption.html").read_text(encoding="utf-8")
    head_end = src.index('<div class="wrap">\n  <h1>')
    foot_start = src.index("\n  <footer>")
    head, foot = src[:head_end], src[foot_start:]
    head = head.replace("<title>jt-ipam: Adoption roadmap</title>", f"<title>jt-ipam: {html.escape(SHORT['en'])}</title>")
    head = re.sub(r'<meta name="description" content="[^"]*" />',
                  f'<meta name="description" content="{html.escape(DESC["en"])}" />', head, count=1)
    head = head.replace('<a href="adoption.html" aria-current="page">', '<a href="adoption.html">')
    head = head.replace("</style>", _EXTRA_CSS + "</style>", 1)
    # 導覽列：在 API 後面加上本頁（標成目前頁）
    head = head.replace('      <a href="api.html">API</a>\n',
                        '      <a href="api.html">API</a>\n      <a href="compliance.html" aria-current="page">'
                        + _tri(SHORT) + "</a>\n", 1)
    meta = ('{"zh": ["jt-ipam：' + SHORT["zh"] + '", "' + DESC["zh"] + '"], "en": ["jt-ipam: ' + SHORT["en"] + '", "'
            + DESC["en"] + '"], "ja": ["jt-ipam：' + SHORT["ja"] + '", "' + DESC["ja"] + '"]}')
    foot = re.sub(r"var META = \{.*?\};", lambda _m: f"var META = {meta};", foot, count=1, flags=re.S)
    return head + '<div class="wrap">\n<!-- generated:start -->\n<!-- generated:end -->' + foot


def render_html(current: str | None) -> str:
    # 每次都從 adoption.html 重建整頁：以前沿用既有檔案只換內容區，改了 _EXTRA_CSS 卻永遠不會進到頁面
    del current
    page = _page_from_adoption()
    a = page.index("<!-- generated:start -->") + len("<!-- generated:start -->")
    b = page.index("<!-- generated:end -->")
    return page[:a] + "\n" + html_body() + "\n" + page[b:]


def main() -> int:
    _check_refs()
    check = "--check" in sys.argv
    current = HTML_FILE.read_text(encoding="utf-8") if HTML_FILE.exists() else None
    outputs = {**{p: md(lang) for lang, p in MD_FILES.items()}, HTML_FILE: render_html(current)}
    stale = [p for p, text in outputs.items() if not p.exists() or p.read_text(encoding="utf-8") != text]
    if check:
        if stale:
            print("過期（請執行 python3 scripts/gen-compliance-docs.py）：" + ", ".join(str(p.relative_to(ROOT)) for p in stale))
            return 1
        return 0
    for p, text in outputs.items():
        p.write_text(text, encoding="utf-8")
    print("已產生：" + ", ".join(str(p.relative_to(ROOT)) for p in outputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
