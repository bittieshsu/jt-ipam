<script setup lang="ts">
/**
 * 10 IP 可用性追蹤：左邊追蹤 IP 的歷史狀態格（每頁六個，名稱與 IP 分行），右邊最近的狀態變化，
 * 下方圖例、時間範圍、最後有效觀察與「不是應用服務 SLA」的說明。
 * 追蹤清單就是儀表板「可用性追蹤」卡片的清單；格子的狀態與聚合全部由後端算（services/warroom/availability.py）：
 *   online＝上線、offline＝離線、unknown＝未知或沒有資料（空心格）、mixed＝同一格裡有上線也有離線
 *   （上半離線、下半上線，中斷不會被蓋掉）、future＝還沒到的時間（不畫）。
 * 反覆中斷另外用文字標籤標示，不另外發明顏色。
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { PageData } from "@/api/warroom";
import WrName from "./WrName.vue";
import WrIco from "./WrIco.vue";
import { usePageTransition } from "./paged";
import { cellWidth, durationParts, eventTime, pctText } from "./statsAvail";
import { IPChangesIcon } from "@/icons";
import { SECTION_ICONS } from "./wrIcons";

const props = defineProps<{ data: PageData }>();
const pageTrans = usePageTransition();     // 換資料頁只換追蹤列表（paged.ts）
const { t } = useI18n();

interface Row {
  id: string; ip: string; name: string | null; cells: string[]; state: "online" | "offline" | "unknown";
  flapping: boolean; flips: number; availability: number | null; coverage: number | null; last_valid_at: string | null;
}
interface Ev { kind: "offline" | "recovered" | "flapping"; at: string; id: string; ip: string; name: string | null; count?: number; last?: string; down_s?: number | null }
interface Col { at: string; span: number; label: string }
interface Win { key: string; requested: string; supported: string[]; cells: number; step_s: number; start: string; history_from: string | null; columns: Col[] }
interface Defs { flap_window_hours: number; flap_min: number; liveness_minutes: number }

const body = computed(() => props.data.body as { state: string; excluded?: number });
const rows = computed(() => (props.data.rows as Row[]) ?? []);
const events = computed(() => (props.data.events as Ev[]) ?? []);
const win = computed(() => props.data.window as Win);
const defs = computed(() => props.data.definitions as Defs);

const GAP = 4;
const GROUP_GAP = 12;
const BAR_WIDTH = 640;                     // 歷史格區域的寬度（1920 畫布；4K 等比放大）
const cw = computed(() => cellWidth(win.value.cells, win.value.columns.length, BAR_WIDTH,
  win.value.cells > 40 ? 2 : GAP, win.value.cells > 40 ? 6 : GROUP_GAP));
const gap = computed(() => (win.value.cells > 40 ? 2 : GAP));
const groupGap = computed(() => (win.value.cells > 40 ? 6 : GROUP_GAP));
/** 每一列切成表頭的群組，群組之間多留一點距離（日期分界） */
function groups(cells: string[]): string[][] {
  const out: string[][] = [];
  let i = 0;
  for (const c of win.value.columns) { out.push(cells.slice(i, i + c.span)); i += c.span; }
  return out;
}
const stepText = computed(() => {
  const s = win.value.step_s;
  return s >= 86400 ? t("warroom.av_step_d", { n: s / 86400 }) : t("warroom.av_step_h", { n: s / 3600 });
});
const fellBack = computed(() => win.value.key !== win.value.requested);
const hm = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
function dur(s: number): string {
  const p = durationParts(s);
  return t(`warroom.${p.key}`, { n: p.n });
}
const stateTone = (s: string) => (s === "online" ? "tone-accent" : s === "offline" ? "tone-coral" : "tone-muted");
const evTone = (k: string) => (k === "offline" ? "tone-coral" : k === "recovered" ? "tone-accent" : "tone-lilac");
const LEGEND = ["online", "offline", "mixed", "unknown"] as const;
</script>

<template>
  <div class="av">
    <section class="av-left" data-testid="wr-av-rows">
      <header class="av-h">
        <div class="wr-h2"><WrIco :icon="SECTION_ICONS.timeline" />{{ t(`warroom.av_window_${win.key}`) }}<span class="av-step">{{ stepText }}</span></div>
        <div v-if="fellBack" class="av-fallback" data-testid="wr-av-fallback">
          {{ t("warroom.av_fallback", { want: t(`warroom.av_span_${win.requested}`), used: t(`warroom.av_span_${win.key}`) }) }}
        </div>
      </header>
      <div v-if="body.state === 'empty'" class="av-empty" data-testid="wr-av-empty">
        <div class="wr-empty">{{ t("warroom.av_empty") }}</div>
        <div class="wr-muted">{{ t("warroom.av_empty_hint") }}</div>
      </div>
      <template v-else>
        <div class="av-cols" :style="{ gap: `${groupGap}px` }">
          <span v-for="c in win.columns" :key="c.at" class="av-col"
                :style="{ width: `${c.span * cw + (c.span - 1) * gap}px` }">{{ c.label }}</span>
        </div>
        <Transition :name="pageTrans" mode="out-in"><div :key="data.page" class="av-list">
          <div v-for="r in rows" :key="r.id" class="av-row" data-testid="wr-av-row" :data-state="r.state">
            <div class="av-row__id">
              <WrName v-if="r.name" :text="r.name" class="av-row__n" />
              <div class="av-row__ip" :class="{ solo: !r.name }">{{ r.ip }}</div>
            </div>
            <div class="av-bars" :style="{ gap: `${groupGap}px` }">
              <div v-for="(g, gi) in groups(r.cells)" :key="gi" class="av-g" :style="{ gap: `${gap}px` }">
                <span v-for="(c, ci) in g" :key="ci" class="av-c" :class="`c-${c}`" :style="{ width: `${cw}px` }" />
              </div>
            </div>
            <div class="av-row__st">
              <div class="av-row__state" :class="stateTone(r.state)">
                {{ t(`warroom.av_state_${r.state}`) }}
                <span v-if="r.flapping" class="wr-tag lv-warn" data-testid="wr-av-flap">{{ t("warroom.av_ev_flapping") }}</span>
              </div>
              <div class="av-row__pct">
                <span class="av-row__k">{{ t("warroom.av_availability") }}</span>
                <span class="av-row__v">{{ pctText(r.availability) }}<template v-if="r.availability != null">%</template></span>
                <span class="av-row__sep">·</span><span class="av-row__cov">{{ r.coverage ? t("warroom.av_coverage", { p: pctText(r.coverage) }) : t("warroom.av_no_coverage") }}</span>
              </div>
            </div>
          </div>
        </div></Transition>
      </template>
      <footer class="av-foot">
        <div class="av-legend">
          <span v-for="s in LEGEND" :key="s" class="av-legend__i">
            <span class="av-c av-c--sw" :class="`c-${s}`" />{{ s === "mixed" ? t("warroom.av_legend_mixed") : s === "unknown" ? t("warroom.av_legend_unknown") : t(`warroom.av_state_${s}`) }}
          </span>
        </div>
        <div class="av-notes">
          <span v-if="data.last_valid_at" data-testid="wr-av-last">{{ t("warroom.av_last_valid", { time: hm(data.last_valid_at as string) }) }}</span>
          <span>{{ t("warroom.av_not_sla") }}</span>
          <span v-if="body.excluded">{{ t("warroom.av_excluded", { n: body.excluded }) }}</span>
        </div>
      </footer>
    </section>

    <aside class="av-side" data-testid="wr-av-events">
      <div class="wr-h2"><WrIco :icon="IPChangesIcon" />{{ t("warroom.av_events_title") }}</div>
      <div v-if="!events.length" class="wr-muted">{{ t("warroom.av_no_events") }}</div>
      <div v-for="e in events" :key="e.kind + e.id + e.at" class="av-ev" data-testid="wr-av-event" :data-kind="e.kind">
        <div class="av-ev__top">
          <WrName v-if="e.name" :text="e.name" class="av-ev__n" />
          <div v-else class="av-ev__n av-ev__solo">{{ e.ip }}</div>
          <span class="av-ev__t">{{ eventTime(e.at) }}</span>
        </div>
        <div v-if="e.name" class="av-ev__ip">{{ e.ip }}</div>
        <div class="av-ev__k" :class="evTone(e.kind)">{{ t(`warroom.av_ev_${e.kind}`) }}</div>
        <div class="av-ev__d">
          <template v-if="e.kind === 'flapping'">
            {{ t("warroom.av_ev_flapping_detail", { h: defs.flap_window_hours, n: e.count ?? 0 }) }}
            <template v-if="e.last"> · {{ t("warroom.av_ev_last", { state: t(`warroom.av_state_${e.last}`) }) }}</template>
          </template>
          <template v-else-if="e.kind === 'recovered' && e.down_s != null">{{ t("warroom.av_ev_recovered_after", { d: dur(e.down_s) }) }}</template>
          <template v-else-if="e.kind === 'offline'">{{ t("warroom.av_ev_offline_detail", { m: defs.liveness_minutes }) }}</template>
        </div>
      </div>
      <!-- 反覆中斷的判定規則放在事件欄底部（左邊頁尾只留圖例與口徑，六列在日文長標題時也放得下） -->
      <div class="av-rule">{{ t("warroom.av_flap_rule", { h: defs.flap_window_hours, n: defs.flap_min }) }}</div>
    </aside>
  </div>
</template>

<style scoped>
.av { display: grid; grid-template-columns: minmax(0, 1fr) 480px; grid-template-rows: minmax(0, 1fr); gap: 40px; height: 100%; }
.av-left { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; }
.av-h { display: flex; justify-content: space-between; align-items: baseline; gap: 20px; }
.av-step { font-size: 22px; color: var(--wr-muted); font-weight: 400; }
.av-fallback { font-size: 21px; color: var(--wr-lilac); text-align: right; }
.av-empty { display: flex; flex-direction: column; gap: 8px; padding-top: 40px; flex: 1; }
.av-cols { display: flex; margin: 8px 0 4px 298px; font-family: var(--wr-num); font-size: 20px; color: var(--wr-muted);
  white-space: nowrap; }
.av-col { flex: none; overflow: visible; }
.av-list { display: flex; flex-direction: column; min-height: 0; }
.av-row { display: grid; grid-template-columns: 270px 640px minmax(0, 1fr); column-gap: 28px; align-items: center; padding: 7px 0;
  border-top: 1px solid var(--wr-line); flex: none; }
.av-row__id { min-width: 0; display: flex; flex-direction: column; gap: 2px; }
/* 短名稱只佔一行（WrName 預設保留兩行高度），長名稱照樣換到第二行 */
.av-row__n { font-size: 25px; font-weight: 700; min-height: 0 !important; }
.av-row__ip { font-family: var(--wr-num); font-size: 21px; color: var(--wr-muted); font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere; }
.av-row__ip.solo { font-size: 25px; font-weight: 700; color: var(--wr-ink); }
.av-bars { display: flex; align-items: center; }
.av-g { display: flex; }
.av-c { display: block; height: 36px; box-sizing: border-box; flex: none; border-radius: 2px; }
.c-online { background: var(--wr-accent); }
.c-offline { background: var(--wr-coral); }
.c-unknown { border: 2px solid var(--wr-outline); }
/* 混合：上半離線、下半上線（平面兩色，不是漸層） */
.c-mixed { background: var(--wr-accent); position: relative; overflow: hidden; }
.c-mixed::before { content: ""; position: absolute; left: 0; right: 0; top: 0; height: 50%; background: var(--wr-coral); }
.c-future { background: transparent; }
.av-row__st { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.av-row__state { font-size: 26px; font-weight: 700; line-height: 1.2; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.av-row__state .wr-tag { font-size: 18px; line-height: 1.2; padding: 1px 8px; }
.av-row__pct { display: flex; align-items: baseline; gap: 8px; white-space: nowrap; line-height: 1.2; }
.av-row__sep { color: var(--wr-muted); }
.av-row__k { font-size: 18px; color: var(--wr-muted); }
.av-row__v { font-family: var(--wr-num); font-size: 26px; font-variant-numeric: tabular-nums; }
.av-row__cov { font-size: 18px; color: var(--wr-muted); white-space: nowrap; }
.av-foot { margin-top: auto; border-top: 1px solid var(--wr-line); padding-top: 10px; display: flex; flex-direction: column; gap: 6px; }
.av-legend { display: flex; gap: 28px; font-size: 21px; flex-wrap: wrap; }
.av-legend__i { display: inline-flex; align-items: center; gap: 10px; white-space: nowrap; }
.av-c--sw { width: 20px; height: 20px; }
.av-notes { display: flex; flex-wrap: wrap; gap: 4px 24px; font-size: 19px; color: var(--wr-muted); }
.av-side { border-left: 1px solid var(--wr-line); padding-left: 36px; display: flex; flex-direction: column; gap: 4px; min-width: 0;
  min-height: 0; overflow: hidden; }
.av-ev { padding: 12px 0; border-top: 1px solid var(--wr-line); display: flex; flex-direction: column; gap: 2px; }
.av-side .wr-h2 { padding-bottom: 10px; }
.av-ev__top { display: flex; justify-content: space-between; align-items: baseline; gap: 16px; }
.av-ev__n { font-size: 26px; font-weight: 700; min-width: 0; flex: 1; min-height: 0 !important; }
.av-ev__t { font-family: var(--wr-num); font-size: 22px; color: var(--wr-ink); white-space: nowrap; }
.av-ev__solo { font-family: var(--wr-num); overflow-wrap: anywhere; }
.av-ev__ip { font-family: var(--wr-num); font-size: 20px; color: var(--wr-muted); overflow-wrap: anywhere; }
.av-ev__k { font-size: 26px; font-weight: 700; margin-top: 2px; }
.av-ev__d { font-size: 20px; color: var(--wr-muted); }
.av-rule { margin-top: auto; padding-top: 10px; border-top: 1px solid var(--wr-line); font-size: 19px; color: var(--wr-muted); }
</style>
