/**
 * 戰情室偵測到新版就自己重新載入（10-23 使用者：「電視開著，ipam 有新版會自動改顯示新版嗎？」）。
 *
 * 一般畫面偵測到新版只跳「點此重新整理」，電視前沒有人會去點，所以戰情室改成自動：
 * - 每 3 分鐘比一次版本（與一般畫面共用 newVersionDeployed）。
 * - 有新版時**先確認新的網頁與後端 API 都正常回應**才重新載入：升級到一半（後端還在重啟）就載入的話，
 *   電視會停在瀏覽器的錯誤頁，沒有人會去按重新整理。沒準備好就每 30 秒再試。
 * - 重新載入前記下目前的主題、頁碼與暫停狀態，載入後接著播（同一個分頁的 sessionStorage，
 *   顯示權杖也在那裡，所以不會掉回登入頁）。
 */
import { newVersionDeployed } from "@/composables/useVersionCheck";

export const RESUME_KEY = "jtipam.warroom.resume";
const CHECK_MS = 180_000;
const RETRY_MS = 30_000;
const RESUME_MAX_AGE_MS = 10 * 60_000;

export interface ResumeState { key: string; page: number; paused: boolean }

export function saveResume(s: ResumeState): void {
  try { sessionStorage.setItem(RESUME_KEY, JSON.stringify({ ...s, at: Date.now() })); } catch { /* 封鎖儲存：就從頭播 */ }
}

/** 取一次就清掉；太舊的不用（避免很久以後手動開頁面跳到奇怪的位置） */
export function takeResume(now = Date.now()): ResumeState | null {
  try {
    const raw = sessionStorage.getItem(RESUME_KEY);
    sessionStorage.removeItem(RESUME_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as ResumeState & { at?: number };
    if (!v || typeof v.key !== "string" || typeof v.page !== "number") return null;
    if (!v.at || now - v.at > RESUME_MAX_AGE_MS) return null;
    return { key: v.key, page: Math.max(1, Math.floor(v.page)), paused: !!v.paused };
  } catch {
    return null;
  }
}

/** 新版的網頁與後端都準備好了嗎（重新載入之後畫得出來） */
export async function readyToReload(): Promise<boolean> {
  try {
    const page = await fetch(`/?t=${Date.now()}`, { cache: "no-store" });
    if (!page.ok || !(await page.text()).includes('id="app"')) return false;
    const api = await fetch("/api/v1/auth/realms", { cache: "no-store" });
    return api.ok;
  } catch {
    return false;
  }
}

/** 開始監看；回傳停止函式。`state` 在重新載入前取目前的播放位置。 */
export function startAutoReload(state: () => ResumeState, reload: () => void = () => window.location.reload()): () => void {
  let pending = false;
  let timer: number | null = null;
  const tick = async (): Promise<void> => {
    if (!pending) pending = (await newVersionDeployed()).changed;
    if (pending && (await readyToReload())) {
      saveResume(state());
      reload();
      return;
    }
    timer = window.setTimeout(tick, pending ? RETRY_MS : CHECK_MS);
  };
  void newVersionDeployed();                       // 先記下這個分頁看到的 build
  timer = window.setTimeout(tick, CHECK_MS);
  return () => { if (timer !== null) window.clearTimeout(timer); };
}
