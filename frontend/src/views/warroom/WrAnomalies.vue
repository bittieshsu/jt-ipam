<script setup lang="ts">
/**
 * 05 異常與曝露：左邊依類型與時間分群的事件矩陣（近 6 小時、每格 20 分鐘）＋對外開放服務的證據等級，
 * 右邊待處理的異常（每頁三筆）。
 * 觀測資料開始之前的時段畫虛線格並標「資料自 X 起」：沒有資料不等於沒有事件。
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { PageData } from "@/api/warroom";
import { pad2 } from "@/utils/warroom";
import { anomalyCategoryLabel } from "./categories";
import WrMatrix from "./WrMatrix.vue";
import WrName from "./WrName.vue";
import { usePageTransition } from "./paged";
import type { IpHints } from "./hints";
import WrHints from "./WrHints.vue";
import WrIco from "./WrIco.vue";
import { SECTION_ICONS, categoryIcon } from "./wrIcons";

const props = defineProps<{ data: PageData }>();
const pageTrans = usePageTransition();     // 換資料頁只換待處理異常這一區（paged.ts）
const { t, te } = useI18n();

interface Row { category: string; open: number; seen: number; cells: string[] }
interface Timeline { state: string; slots?: number; slot_minutes?: number; start?: string; data_since?: string | null; rows?: Row[]; more?: number }
interface Item { id: string; rank: number; category: string; label: string; ip: string | null; severity: string; first_seen_at: string; baseline?: boolean; hints?: IpHints }
interface Prio { state: string; items?: Item[]; total?: number }
interface Evidence { state: string; counts?: Record<string, number>; total?: number; sensitive?: number }

const tl = computed(() => props.data.timeline as Timeline);
const prio = computed(() => props.data.priority as Prio);
const ev = computed(() => props.data.evidence as Evidence);
const LEGEND = ["new", "active", "resolved", "quiet", "nodata"] as const;
const LEVELS = ["confirmed", "named", "port_guess"] as const;

const hm = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
/** 時間軸刻度：每 6 格（2 小時）一個 */
const ticks = computed(() => {
  const t0 = tl.value.start ? new Date(tl.value.start).getTime() : 0;
  const n = tl.value.slots ?? 18;
  const step = (tl.value.slot_minutes ?? 20) * 60000;
  return [0, 6, 12, 18].filter((i) => i <= n).map((i) => ({ i, label: hm(new Date(t0 + i * step)) }));
});
const hasNoData = computed(() => (tl.value.rows ?? []).some((r) => r.cells.includes("nodata")));
function since(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return `${d.toLocaleDateString([], { month: "2-digit", day: "2-digit" })} ${hm(d)}`;
}
const cat = (c: string) => anomalyCategoryLabel(c, t, te);
</script>

<template>
  <div class="an">
    <div class="an-left">
      <section class="an-tl" data-testid="wr-an-timeline">
        <header class="an-h">
          <div>
            <div class="wr-h2"><WrIco :icon="SECTION_ICONS.timeline" />{{ t("warroom.anomaly_timeline") }}</div>
            <div class="an-sub">{{ t("warroom.last_6h_by_type") }}</div>
          </div>
          <div class="an-legend">
            <span v-for="s in LEGEND" :key="s"><span class="wr-swatch wr-swatch--sm" :class="`s-${s}`" />{{ t(`warroom.tl_${s}`) }}</span>
          </div>
        </header>
        <div v-if="tl.state === 'forbidden'" class="wr-muted">{{ t("warroom.forbidden") }}</div>
        <div v-else-if="!tl.rows?.length" class="wr-muted">{{ t("warroom.no_anomaly_events") }}</div>
        <div v-else class="an-grid">
          <div class="an-axis">
            <span v-for="k in ticks" :key="k.i" class="an-tick" :style="{ left: `${(k.i / (tl.slots ?? 18)) * 100}%` }">{{ k.label }}</span>
          </div>
          <div v-for="r in tl.rows" :key="r.category" class="an-row" data-testid="wr-an-row">
            <WrName :text="cat(r.category)" class="an-row__l" />
            <div class="an-row__n" :class="{ zero: !r.open }">{{ r.open }}</div>
            <WrMatrix :cells="r.cells" :cols="tl.slots ?? 18" :size="38" :gap="6" />
          </div>
          <div class="an-foot">
            <span v-if="hasNoData" class="wr-unit">{{ t("warroom.data_since", { time: since(tl.data_since) }) }}</span>
            <span v-if="tl.more" class="wr-unit">{{ t("warroom.more_types", { n: tl.more }) }}</span>
          </div>
        </div>
      </section>
      <section class="an-ev" data-testid="wr-an-evidence">
        <div class="an-ev__k"><WrIco :icon="SECTION_ICONS.evidence" />{{ t("warroom.exposure_evidence") }}</div>
        <div v-if="ev.state === 'forbidden'" class="wr-muted">{{ t("warroom.forbidden") }}</div>
        <div v-else-if="!ev.counts" class="wr-muted">{{ t("warroom.no_exposure") }}</div>
        <template v-else>
          <div v-for="l in LEVELS" :key="l" class="an-ev__i">
            <span class="wr-big an-ev__v" :class="{ 'tone-muted': !ev.counts[l] }">{{ ev.counts[l] ?? 0 }}</span>
            <span class="an-ev__l">{{ t(`warroom.level_${l}`) }}</span>
          </div>
        </template>
      </section>
    </div>

    <aside class="an-side">
      <header class="an-h">
        <div class="wr-h2"><WrIco :icon="SECTION_ICONS.openAnomalies" />{{ t("warroom.open_anomalies") }}<span v-if="prio.total" class="wr-h2__n">{{ prio.total }}</span></div>
      </header>
      <Transition :name="pageTrans" mode="out-in"><div :key="data.page" class="an-list">
        <div v-if="prio.state === 'forbidden'" class="wr-muted">{{ t("warroom.forbidden") }}</div>
        <div v-else-if="!prio.items?.length" class="wr-muted">{{ t("warroom.no_open_anomalies") }}</div>
        <div v-for="it in prio.items ?? []" :key="it.id" class="an-item" :class="`sev-${it.severity}`" data-testid="wr-an-item">
          <div class="an-item__rank">{{ pad2(it.rank) }}</div>
          <div class="an-item__body">
            <WrName :text="it.label || it.ip || '—'" class="an-item__name" />
          <WrHints :hints="it.hints" />
            <div class="an-item__meta">
              <div><div class="an-item__k">{{ t("warroom.type") }}</div><div class="an-item__cat"><WrIco :icon="categoryIcon(it.category)" />{{ cat(it.category) }}</div></div>
              <div v-if="it.ip && it.ip !== it.label"><div class="an-item__k">IP</div><div class="an-item__v">{{ it.ip }}</div></div>
              <div><div class="an-item__k">{{ t("warroom.first_seen") }}</div><div class="an-item__v">{{ it.baseline ? t("warroom.before_observation") : since(it.first_seen_at) }}</div></div>
            </div>
          </div>
        </div>
      </div></Transition>
    </aside>
  </div>
</template>

<style scoped>
/* 列高固定為可用高度（minmax(0, …)）：某一區內容多時曾把整列撐高，另一區的下半部被推出畫面（10-22） */
.an { display: grid; grid-template-columns: minmax(0, 1fr) 660px; grid-template-rows: minmax(0, 1fr); gap: 44px; height: 100%; }
.an-left { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.an-tl { flex: 1; min-height: 0; display: flex; flex-direction: column; gap: 12px; overflow: hidden; }
.an-h { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; }
.an-sub { font-size: 21px; color: var(--wr-muted); margin-top: 4px; }
.an-legend { display: flex; gap: 18px; flex-wrap: wrap; justify-content: flex-end; font-size: 20px; max-width: 640px; }
.an-legend > span { display: inline-flex; align-items: center; gap: 8px; white-space: nowrap; }
.an-grid { display: flex; flex-direction: column; }
.an-axis { position: relative; height: 30px; margin-left: 334px; width: 786px; font-family: var(--wr-num);
  font-size: 20px; color: var(--wr-muted); }
.an-tick { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.an-tick:first-child { transform: none; }
.an-tick:last-child { transform: translateX(-100%); }
.an-row { display: grid; grid-template-columns: 250px 60px auto; gap: 12px; align-items: center; padding: 10px 0;
  border-top: 1px solid var(--wr-line); }
.an-row__l { font-size: 26px; font-weight: 700; }
.an-row__n { font-family: var(--wr-num); font-size: 36px; text-align: right; padding-right: 12px; font-variant-numeric: tabular-nums; }
.an-row__n.zero { color: var(--wr-muted); }
.an-foot { display: flex; gap: 24px; padding-top: 10px; border-top: 1px solid var(--wr-line); }
.an-ev { display: flex; align-items: center; gap: 36px; border-top: 1px solid var(--wr-line); padding-top: 14px; }
.an-ev__k { font-size: 26px; font-weight: 700; padding-right: 32px; border-right: 1px solid var(--wr-line); white-space: nowrap; }
.an-ev__i { display: flex; align-items: baseline; gap: 12px; }
.an-ev__v { font-size: 64px; font-weight: 600; }
.an-ev__l { font-size: 22px; color: var(--wr-muted); white-space: nowrap; }
.an-side { border-left: 1px solid var(--wr-line); padding-left: 40px; display: flex; flex-direction: column; gap: 16px; min-width: 0;
  min-height: 0; overflow: hidden; }
.an-list { display: flex; flex-direction: column; gap: 16px; min-height: 0; }
.an-item { display: grid; grid-template-columns: 112px minmax(0, 1fr); background: var(--wr-panel); min-height: 0; }
.an-item__rank { display: flex; align-items: center; justify-content: center; background: var(--wr-lilac); color: #101216;
  font-family: var(--wr-num); font-size: 64px; font-weight: 700; }
.sev-high .an-item__rank { background: var(--wr-coral); }
.an-item__body { padding: 14px 22px; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.an-item__name { font-size: 28px; font-weight: 700; font-family: var(--wr-num); }
.an-item__meta { display: grid; grid-template-columns: repeat(3, auto); justify-content: start; gap: 28px; }
.an-item__k { font-size: 18px; color: var(--wr-muted); }
.an-item__cat { font-size: 24px; font-weight: 700; color: var(--wr-lilac); white-space: nowrap; }
.sev-high .an-item__cat { color: var(--wr-coral); }
.an-item__v { font-size: 24px; font-family: var(--wr-num); white-space: nowrap; }
</style>
