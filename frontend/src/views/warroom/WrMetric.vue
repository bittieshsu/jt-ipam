<script setup lang="ts">
/** 大指標：細字重的大數字＋標籤。value 為 null＝沒有權限，顯示小字「無權限」而不是 0
 *  （以前是一條大「—」，比標籤高、看起來像是另一種東西，使用者 10-22 指出）。 */
import { useI18n } from "vue-i18n";
defineProps<{ value: number | string | null | undefined; label: string; tone?: "accent" | "lilac" | "coral" | "ink" }>();
const { t } = useI18n();
</script>

<template>
  <div class="wr-metric" :class="`t-${tone ?? 'ink'}`">
    <div v-if="value == null" class="wr-metric__na">{{ t("warroom.no_access_short") }}</div>
    <div v-else class="wr-metric__v">{{ typeof value === "number" ? value.toLocaleString() : value }}</div>
    <div class="wr-metric__l">{{ label }}</div>
  </div>
</template>

<style scoped>
.wr-metric { display: flex; align-items: flex-end; gap: 16px; min-width: 0; }
.wr-metric__v { font-family: var(--wr-num); font-weight: 300; font-size: 104px; line-height: .9;
  font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
/* 平均斷行：不會只剩一個字掉到第二行（「DNS 不一致群 / 組」） */
.wr-metric__l { font-size: 28px; font-weight: 600; color: var(--wr-ink); max-width: 280px; min-width: 4.5em; line-height: 1.15;
  overflow-wrap: break-word; text-wrap: balance; }
.wr-metric__na { font-size: 28px; color: var(--wr-muted); line-height: 1.15; white-space: nowrap; }
.t-accent .wr-metric__v { color: var(--wr-accent); }
.t-lilac .wr-metric__v { color: var(--wr-lilac); }
.t-coral .wr-metric__v { color: var(--wr-coral); }
.t-ink .wr-metric__v { color: var(--wr-ink); }
</style>
