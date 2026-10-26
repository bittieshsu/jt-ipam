/**
 * 12 AI 情報的逐字播放（設計稿 v1.2 第 7 節）：純函式，畫面（WrAiIntel.vue）照這裡算出來的節奏播。
 *
 * - 依 Unicode 字素（grapheme cluster）逐字顯示：組合字、emoji 不會被切壞
 * - IP、CIDR、MAC 這類識別碼是一整塊：排版時不拆行（逐字顯示時仍一個字一個字出現，但整塊預先留好寬度）
 * - 速度：中文約每秒 15 字、英數約每秒 36 字；慢 / 標準 / 快＝0.7 / 1 / 1.4 倍
 * - 節奏：每段依種子產生平滑的快慢變化（速度 0.8–1.2 倍，落在規格的 0.75–1.25 之內，
 *   中文就剛好是每秒 12–18 字），不是每個字亂跳
 * - 停頓：逗號 120–220ms、句號 350–650ms、段落之間 1.2–2.2 秒；播完依字數多停 4–12 秒讓人讀完
 */

export type Speed = "slow" | "normal" | "fast";
const SPEED: Record<Speed, number> = { slow: 0.7, normal: 1, fast: 1.4 };

/** 字素切分（不支援 Intl.Segmenter 的舊瀏覽器退回以碼位切） */
export function graphemes(text: string): string[] {
  const Seg = (Intl as unknown as { Segmenter?: new (l?: string, o?: { granularity: string }) => { segment(s: string): Iterable<{ segment: string }> } }).Segmenter;
  if (Seg) return Array.from(new Seg(undefined, { granularity: "grapheme" }).segment(text), (s) => s.segment);
  return Array.from(text);
}

/** 不可拆行的識別碼：IPv4（可帶 /遮罩）、IPv6、MAC */
const ATOMIC = /((?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-f:])|(?:\d{1,3}\.){3}(?:\d{1,3}|x)(?:\/\d{1,2})?|[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7}(?:\/\d{1,3})?)/gi;

export interface Token { text: string; atomic: boolean }

/** 把一則播報切成「一般文字」與「識別碼」兩種片段（識別碼在畫面上包成 nowrap） */
export function tokens(text: string): Token[] {
  const out: Token[] = [];
  let last = 0;
  for (const m of text.matchAll(ATOMIC)) {
    const i = m.index ?? 0;
    if (i > last) out.push({ text: text.slice(last, i), atomic: false });
    out.push({ text: m[0], atomic: true });
    last = i + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), atomic: false });
  return out;
}

/** 字串雜湊當種子（同一則每次播的節奏一樣） */
export function seedOf(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return (h >>> 0) / 4294967296;
}

/** 平滑的速度倍率（0.8–1.2）：兩個不同週期的正弦疊起來，相鄰的字差異很小 */
export function rhythm(i: number, seed: number): number {
  const v = 0.6 * Math.sin(i * 0.31 + seed * 6.283) + 0.4 * Math.sin(i * 0.087 + seed * 12.566);
  return 1 + 0.2 * Math.max(-1, Math.min(1, v));
}

const WIDE = /[⺀-鿿가-힯豈-﫿＀-￯　-〿]/;
const COMMA = /^[，、,;；：:]$/;
const STOP = /^[。．.!?！？]$/;

/** 第 i 個字出現前要等多久（毫秒） */
export function charDelay(g: string, i: number, opts: { speed: Speed; rhythm: boolean; seed: number }): number {
  const base = WIDE.test(g) ? 1000 / 15 : g === " " ? 18 : 1000 / 36;
  const r = opts.rhythm ? rhythm(i, opts.seed) : 1;
  return base / r / SPEED[opts.speed];
}

/** 這個字之後的停頓（逗號、句號）；其他字是 0 */
export function pauseAfter(g: string, i: number, seed: number): number {
  const u = (Math.sin(i * 12.9898 + seed * 78.233) + 1) / 2;   // 0–1，同一則固定
  if (COMMA.test(g)) return 120 + u * 100;
  if (STOP.test(g)) return 350 + u * 300;
  return 0;
}

/** 一則播完到下一則之間：讀完的時間（4–12 秒、依字數）＋段落停頓（1.2–2.2 秒） */
export function afterItem(text: string, seed: number): number {
  const n = graphemes(text).length;
  const read = Math.min(12000, Math.max(4000, 4000 + n * 70));
  return read + 1200 + seed * 1000;
}
