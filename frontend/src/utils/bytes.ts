/** 位元組 → 人看的大小（1024 進位，單位寫 KB／MB／GB：與 du、df 的習慣一致） */
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = Math.max(0, n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const digits = i === 0 || v >= 100 ? 0 : v >= 10 ? 1 : 2;
  return `${v.toFixed(digits).replace(/\.0+$|(\.\d*[1-9])0+$/, "$1")} ${units[i]}`;
}
