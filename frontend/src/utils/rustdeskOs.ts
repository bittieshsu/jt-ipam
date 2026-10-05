/**
 * RustDesk 客戶端回報的作業系統字串：「windows / Windows 11 Pro - 11 (26200)」。
 * 畫面只要「Windows 11 Pro」：前面的平台名稱重複、後面的版本與組建號不需要（使用者 2026-10-05）。
 * 回傳 [主要文字, 平台]；沒有「 / 」時平台是空字串。
 */
export function rustdeskOs(raw: string | null | undefined): [string, string] {
  if (!raw) return ["", ""];
  const [plat, ...rest] = String(raw).split(" / ");
  const detail = (rest.join(" / ") || plat).replace(/\s+-\s+[\d.]+\s*\(\d+\)\s*$/, "").trim();
  return [detail, rest.length ? plat : ""];
}
