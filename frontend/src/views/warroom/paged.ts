/**
 * 同主題換資料頁：只有會分頁的那一區換、播動畫，標題、指標與其他區塊不動
 * （10-22 使用者：「按上下時只是切換右邊待處理異常區，那麼過場時只要那區有動畫更換就好，不要整頁都換」）。
 * 方向跟按鍵一致：往下一頁從下方進來、往上一頁從上方進來。整頁的轉場只在換主題時播（WarRoom.vue）。
 *
 * 用法：<Transition :name="pageTrans" mode="out-in"><div :key="data.page" class="…">…</div></Transition>
 */
import { computed, inject, ref, type Ref } from "vue";

export function usePageTransition() {
  const dir = inject<Ref<"fwd" | "back">>("wrPageDir", ref("fwd"));
  const reduce = inject<Ref<boolean>>("wrReduceMotion", ref(false));
  return computed(() => (reduce.value ? "wr-none" : `wr-pg-${dir.value}`));
}
