import { nextTick, onBeforeUnmount, onMounted, ref, watch, type Ref } from "vue";

/**
 * 滿版的圖（IP 變更評估關係圖、IP 拓樸圖）往下佔滿視窗（使用者 2026-10-08：圖下方還留一片空白）。
 *
 * 空白有兩個來源：主版面內容區固定留 88px 底部（給右下角 AI 對話浮動按鈕，免得清單最後一列被蓋住），
 * 以及圖的高度是用 `calc(100vh - 某個估計值)` 猜的 —— 圖例折不折行、視窗多大都會讓它猜錯。
 * 所以：
 * - 有滿版圖的頁面打開時，主版面底部留白縮成 16px（`fillPageCount`，MainLayout 讀）
 * - 高度用實際量到的版面算：「工具列到頁面底部」剛好等於可視高度
 */
export const fillPageCount = ref(0);

/** 真正在捲動的那一層的可視高度：往上找會捲的祖先（找不到就是整頁），再扣掉黏在上方的頂列 */
function visibleHeight(el: HTMLElement): number {
  let scroller: HTMLElement | null = null;
  for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
    const oy = getComputedStyle(p).overflowY;
    if ((oy === "auto" || oy === "scroll") && p.scrollHeight > p.clientHeight + 1) { scroller = p; break; }
  }
  const rect = scroller ? scroller.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  let top = rect.top;
  for (const h of Array.from(document.querySelectorAll<HTMLElement>(".n-layout-header, .topbar"))) {
    const pos = getComputedStyle(h).position;
    if (pos === "sticky" || pos === "fixed") top = Math.max(top, h.getBoundingClientRect().bottom);
  }
  return Math.min(rect.bottom, window.innerHeight) - top;
}

/**
 * 算高度。量不準的時候回 null（呼叫端就不更新，等下一次量）：
 * - 還沒排版（分頁藏著、正在切換）：量到的位置全是 0，外框底部卻可能在畫面上方很遠，
 *   算出來是「可視高度＋一千多」（e2e 2026-10-08 抓到 1720px：圖比畫面高，評估目標跑到畫面外點不到）
 * - 外框底部在圖的底部之上：版面正在變，量到的是兩個不同時間點的東西
 * 算出來也不會超過可視高度（但小視窗仍保有最低高度）。
 */
export function fillHeight(p: { visible: number; above: number; below: number; min: number; laidOut: boolean }): number | null {
  if (!p.laidOut || p.below < 0) return null;
  const h = Math.floor(Math.min(p.visible - p.above - p.below, p.visible));
  return Math.max(p.min, h);
}

export function useFillHeight(target: Ref<HTMLElement | null>, anchor: Ref<HTMLElement | null>,
                              opts: { min?: number } = {}) {
  const height = ref<number | null>(null);
  const min = opts.min ?? 480;
  let ro: ResizeObserver | null = null;

  function measure() {
    const el = target.value, a = anchor.value;
    if (!el || !a) return;
    const r = el.getBoundingClientRect();
    const card = (el.closest(".n-card") as HTMLElement | null) ?? el;
    const content = el.closest(".n-layout-scroll-container") as HTMLElement | null;
    const pad = content ? parseFloat(getComputedStyle(content).paddingBottom) || 0 : 0;
    // 圖下方到頁面底部的固定距離：卡片內距＋版面底部留白（與圖本身多高無關）
    const below = card.getBoundingClientRect().bottom - r.bottom + pad;
    const above = r.top - a.getBoundingClientRect().top;     // 工具列頂端到圖頂端
    const laidOut = el.isConnected && el.getClientRects().length > 0 && a.getClientRects().length > 0;
    const h = fillHeight({ visible: visibleHeight(el), above, below, min, laidOut });
    if (h !== null) height.value = h;
  }
  // 版面改了（主版面留白變了、圖例折行、明細欄開關）要重量；排到下一幀，等瀏覽器先排好版
  function schedule() { requestAnimationFrame(() => requestAnimationFrame(measure)); }

  onMounted(() => {
    fillPageCount.value += 1;
    void nextTick(schedule);
    window.addEventListener("resize", schedule);
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(schedule);
      if (anchor.value) ro.observe(anchor.value);
    }
  });
  // 圖常常是資料載入後才出現（v-if）：元素出現時才開始量、才開始觀察工具列折行
  watch(anchor, (el, old) => {
    if (old) ro?.unobserve(old);
    if (el) { ro?.observe(el); schedule(); }
  });
  onBeforeUnmount(() => {
    fillPageCount.value = Math.max(0, fillPageCount.value - 1);
    window.removeEventListener("resize", schedule);
    ro?.disconnect();
  });
  return { height, measure: schedule };
}
