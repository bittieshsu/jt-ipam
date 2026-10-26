/**
 * 戰情室按鍵：唯一的一份。鍵盤處理（WarRoom.vue onKey）、電視上按 H 的說明、戰情室設定頁的按鍵表都從這裡來
 * （10-22 設定頁漏了 H、電視說明漏了 Home 與 Esc，使用者：「以後要同步，這邊顯示跟電視牆要一樣」）。
 * 新增或改按鍵只改這裡；utils/__tests__/warroomKeys.test.ts 會檢查「列出來的都有作用、有作用的都有列出來」。
 */

export type KeyAction =
  | "topic_next" | "topic_prev" | "page_next" | "page_prev" | "first"
  | "number" | "pause" | "pin" | "help" | "close";

/** 顯示用的一列：keys 是鍵帽上的字；range 時 keys = [第一個, 最後一個]，中間畫「–」 */
export interface KeyRow { keys: string[]; label: string; range?: boolean }

const ACTIONS: Record<string, KeyAction> = {
  ArrowRight: "topic_next", ArrowLeft: "topic_prev",
  ArrowDown: "page_next", PageDown: "page_next",
  ArrowUp: "page_prev", PageUp: "page_prev",
  Home: "first",
  " ": "pause",
  p: "pin", P: "pin",                            // 釘選目前主題：照常換資料頁、不換主題（10-26 使用者）
  h: "help", H: "help",                          // 只用 H（10-22 使用者：「改 H 就好，不要用問號」）
  Escape: "close",
};

/** 有作用的鍵（數字鍵另外處理）：給測試檢查每一個都有列在說明裡 */
export const HANDLED_KEYS = Object.keys(ACTIONS);

export function keyAction(key: string): KeyAction | null {
  if (/^[1-9]$/.test(key)) return "number";
  return ACTIONS[key] ?? null;
}

/** label 是 i18n 鍵 */
export function warroomKeyRows(topics: number): KeyRow[] {
  return [
    { keys: ["1", String(Math.max(1, Math.min(9, topics)))], range: true, label: "warroom.keys_topic_num" },
    { keys: ["←", "→"], label: "warroom.keys_topic" },
    { keys: ["↑", "↓", "PgUp", "PgDn"], label: "warroom.keys_page" },
    { keys: ["Home"], label: "warroom.keys_home" },
    { keys: ["Space"], label: "warroom.keys_pause" },
    { keys: ["P"], label: "warroom.keys_pin" },
    { keys: ["H"], label: "warroom.keys_help" },
    { keys: ["Esc"], label: "warroom.keys_close" },
  ];
}
