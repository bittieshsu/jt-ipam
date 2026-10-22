<script setup lang="ts">
/**
 * 方格矩陣。每格的狀態對應固定的顏色語意（同頁不改）：
 *   allocated／ok → 黃綠；reserved／warn／busy → 淡紫；anomaly／crit／overloaded → 珊瑚橘；
 *   free → 淺灰（可配置）；unscanned → 深灰；stale → 空心格；none／nodata → 虛線格（沒有資料，不是正常）
 * 03 到 06 頁另外的狀態（語意不變）：running／covered → 黃綠；partial／other／active → 淡紫；
 *   missing／gap／new → 珊瑚橘；stopped → 中灰；pending／unknown → 空心格；resolved → 深灰；quiet → 細框（沒有事件）
 * hollow：整塊資料逾時（例如掃描代理沒回報）→ 全部畫空心，不讓舊資料看起來像即時。
 * 資料更新時變動的格子短暫亮起（降低動畫時不閃）。
 */
import { inject, ref, watch, type Ref } from "vue";
import { changedIndexes, gridCols } from "@/utils/warroom";

const props = withDefaults(defineProps<{
  cells: string[]; cols: number; size: number; gap?: number; hollow?: boolean;
}>(), { gap: 6, hollow: false });

const reduceMotion = inject<Ref<boolean>>("wrReduceMotion", ref(false));
const flash = ref<Set<number>>(new Set());
let timer: ReturnType<typeof setTimeout> | null = null;
watch(() => props.cells, (next, prev) => {
  if (reduceMotion.value) return;
  const changed = changedIndexes(prev, next);
  if (!changed.size) return;
  flash.value = changed;
  if (timer) clearTimeout(timer);
  timer = setTimeout(() => { flash.value = new Set(); }, 1600);
});
</script>

<template>
  <div class="wr-matrix"
       :style="{ gridTemplateColumns: `repeat(${gridCols(cells.length, cols)}, ${size}px)`, gap: `${gap}px` }">
    <span v-for="(s, i) in cells" :key="i" class="wr-cell"
          :class="[`s-${s}`, { hollow, flash: flash.has(i) }]"
          :style="{ width: `${size}px`, height: `${size}px` }" />
  </div>
</template>

<style scoped>
.wr-matrix { display: grid; }
.wr-cell { display: block; box-sizing: border-box; border-radius: 2px; transition: filter .6s ease; }
.s-allocated, .s-ok, .s-running, .s-covered { background: var(--wr-accent); }
.s-reserved, .s-warn, .s-busy, .s-partial, .s-other, .s-active { background: var(--wr-lilac); }
.s-anomaly, .s-crit, .s-overloaded, .s-missing, .s-gap, .s-new { background: var(--wr-coral); }
.s-free { background: var(--wr-free); }
.s-stopped { background: var(--wr-stop); }
.s-unscanned, .s-resolved { background: var(--wr-deep); }
.s-stale, .s-pending, .s-unknown { background: transparent; border: 2px solid var(--wr-outline); }
.s-none, .s-nodata { background: transparent; border: 2px dashed var(--wr-deep-line); }
.s-quiet { background: transparent; border: 1px solid var(--wr-deep-line); }
.wr-cell.hollow { background: transparent !important; border: 2px solid var(--wr-outline); }
.wr-cell.flash { filter: brightness(1.6) saturate(1.2); }
</style>
