/**
 * 表格勾選欄：點到勾選框旁邊的空白也算勾選，而且不觸發整列的點擊（使用者 2026-10-20：IP 清單差一點點沒點到勾選框，
 * 就變成進到那個 IP 的頁面）。全站的 NDataTable 都適用，所以在文件層攔一次，不必每張表各自處理。
 *
 * 做法：捕獲階段看到點在 `td.n-data-table-td--selection`（或表頭的勾選格）、卻不在勾選框本身上 → 停止這次點擊往下傳
 * （整列的 onClick 收不到），改成點那個勾選框。點在勾選框本身上的照舊交給 naive-ui。
 */
const SEL_CELL = "td.n-data-table-td--selection, th.n-data-table-th--selection";
const CONTROL = ".n-checkbox, .n-radio";

export function onSelectionCellClick(e: MouseEvent): void {
  const target = e.target as Element | null;
  if (!target || typeof target.closest !== "function") return;
  const cell = target.closest(SEL_CELL);
  if (!cell || target.closest(CONTROL)) return;
  const control = cell.querySelector<HTMLElement>(CONTROL);
  if (!control || control.classList.contains("n-checkbox--disabled") || control.classList.contains("n-radio--disabled")) {
    e.stopPropagation();            // 勾不了也不要變成進頁面
    return;
  }
  e.stopPropagation();
  e.preventDefault();
  control.click();
}

export function installSelectionCellClick(doc: Document = document): void {
  doc.addEventListener("click", onSelectionCellClick, true);
}
