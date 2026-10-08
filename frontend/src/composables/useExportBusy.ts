import { ref } from "vue";

/**
 * 匯出按鈕的「處理中」狀態（使用者 2026-10-08：選了檔案格式、正在處理時按鈕要反灰並轉圈）。
 *
 * docx／xlsx 這類在瀏覽器裡產生的檔案是同步運算：直接做的話，按鈕的轉圈還沒畫出來就已經卡住主執行緒，
 * 使用者看到的是畫面凍住。所以先讓出一個畫面再開始做。處理中再選一次會被忽略，不會重複產檔。
 * 用法：按鈕 `:loading="exporting" :disabled="exporting"`、下拉選單 `:disabled="exporting"`，
 * 選取時呼叫 `run(() => 實際匯出(key))`。
 */
export function useExportBusy() {
  const exporting = ref(false);
  async function run(fn: () => unknown): Promise<void> {
    if (exporting.value) return;
    exporting.value = true;
    try {
      await nextPaint();
      await fn();
    } finally {
      exporting.value = false;
    }
  }
  return { exporting, run };
}

/** 等瀏覽器把目前的狀態畫出來（下一個畫面之後） */
export function nextPaint(): Promise<void> {
  return new Promise((resolve) => {
    const raf = typeof requestAnimationFrame === "function"
      ? requestAnimationFrame
      : (cb: () => void) => setTimeout(cb, 16);
    raf(() => setTimeout(resolve, 0));
  });
}
