/**
 * 終端機字級（A− / A+）：SSH 主控台與 PVE 的 xterm 共用同一份範圍與偏好
 * （10-22 使用者：「pve xterm 要跟 ssh 一樣可以調整字大字小」）。
 * 字級記在瀏覽器（換頁、重連、換主控台都沿用）；讀寫失敗（無痕模式等）就用預設、不影響操作。
 */
import { computed, ref } from "vue";

export const FONT_MIN = 9;
export const FONT_MAX = 24;
export const FONT_KEY = "jt.sshFontSize";      // 沿用 SSH 原本的鍵，既有使用者的設定不會不見
const DEFAULT = 13;

export function useTerminalFontSize(onChange?: (px: number) => void) {
  const fontSize = ref(DEFAULT);
  try {
    const v = Number(localStorage.getItem(FONT_KEY));
    if (v >= FONT_MIN && v <= FONT_MAX) fontSize.value = v;
  } catch { /* 讀不到就用預設 */ }
  function step(delta: number) {
    const next = Math.min(FONT_MAX, Math.max(FONT_MIN, fontSize.value + delta));
    if (next === fontSize.value) return;
    fontSize.value = next;
    try { localStorage.setItem(FONT_KEY, String(next)); } catch { /* 忽略 */ }
    onChange?.(next);
  }
  return {
    fontSize, step,
    atMin: computed(() => fontSize.value <= FONT_MIN),
    atMax: computed(() => fontSize.value >= FONT_MAX),
  };
}
