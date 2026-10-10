/**
 * 存取權杖放哪裡（2026-10-09 起）。
 *
 * - 更新權杖：只在後端設的 HttpOnly Cookie（`jt_refresh`，路徑 /api/v1/auth），JavaScript 讀不到
 * - 存取權杖：這個分頁的 sessionStorage（15 分鐘到期；登出或被撤銷時伺服器端立即失效）。
 *   新分頁一開始沒有 → 用 Cookie 換一把（`ensureSession`），不用重新登入
 *
 * 以前兩者都在 localStorage：任何 XSS 都能把 14 天有效的更新權杖拿走。舊版留下的會在載入時清掉。
 */
const KEY = "access_token";

try {
  localStorage.removeItem("access_token");
  localStorage.removeItem("refresh_token");
} catch { /* 私密視窗等存取不到時略過 */ }

export function getAccessToken(): string | null {
  try { return sessionStorage.getItem(KEY); } catch { return null; }
}

export function setAccessToken(token: string): void {
  try { sessionStorage.setItem(KEY, token); } catch { /* ignore */ }
}

export function clearAccessToken(): void {
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}

/** 用 Cookie 的認證端點（換發、登出）要帶的標頭（後端的 CSRF 第二道防護）。 */
export const AUTH_COOKIE_HEADERS = { "X-Requested-With": "jt-ipam" } as const;
