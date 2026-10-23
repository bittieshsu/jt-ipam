<script setup lang="ts">
/**
 * 03 網路關聯：以一台設備為中心的正交關係圖（上一層、這台、下一層）＋右側這台的資料與連線。
 * - 線型照證據：登記與監控回報是實線，被動學到（FDB）與推導是虛線；不用顏色當唯一識別
 * - 線不穿過文字：上一層的名稱放在方塊右邊（線從方塊下緣出去），下一層的名稱放在方塊下方（線從上緣進來）
 * - 名稱照原樣；方塊裡是設備類型的縮寫，不是自創的編號
 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import type { PageData } from "@/api/warroom";
import WrName from "./WrName.vue";
import { usePageTransition } from "./paged";

const props = defineProps<{ data: PageData }>();
const pageTrans = usePageTransition();     // 換資料頁只換分頁的那一區（paged.ts）
const { t, te } = useI18n();

interface Node { id: string; name: string; type: string; ip: string | null; evidence: string; line: "solid" | "dashed";
  port: string | null; degree: number; below: number; endpoints?: number; anomalies: number | null; peer?: boolean }
interface Focus { state: string; devices?: number; limit?: number;
  device?: { id: string; name: string; type: string; ip: string | null; vendor: string | null; model: string | null;
    location: string | null; degree: number; endpoints?: number; anomalies: number | null };
  up?: Node[]; more_up?: number; down?: Node[]; more_down?: number; evidence?: Record<string, number>;
  links?: Node[]; more_links?: number }

const f = computed(() => props.data.focus as Focus);

const W = 1180;
const BOX = 76;
const FOCUS = 112;
const UP_Y = 8;
const BUS_UP = 150;
const FOCUS_Y = 190;
const BUS_DOWN = 362;
const DOWN_Y = 400;
const FOCUS_X = 360;                      // 中心設備方塊的中心（名稱放右邊）

function spread(n: number, maxSlot: number): number[] {
  if (!n) return [];
  const slot = Math.min(W / n, maxSlot);
  const start = (W - slot * n) / 2;
  return Array.from({ length: n }, (_, i) => start + slot * (i + 0.5));
}
const upX = computed(() => spread(f.value.up?.length ?? 0, 400));
const downX = computed(() => spread(f.value.down?.length ?? 0, 260));
const downSlot = computed(() => Math.min(W / Math.max(f.value.down?.length ?? 1, 1), 260));

/** 每條線各自一條路徑（虛線先畫、實線後畫蓋在上面，共用的匯流段看起來是實線＝至少一條有實證） */
const paths = computed(() => {
  const out: { d: string; line: string }[] = [];
  (f.value.up ?? []).forEach((n, i) => {
    out.push({ line: n.line, d: `M ${FOCUS_X} ${FOCUS_Y} V ${BUS_UP} H ${upX.value[i]} V ${UP_Y + BOX}` });
  });
  (f.value.down ?? []).forEach((n, i) => {
    out.push({ line: n.line, d: `M ${FOCUS_X} ${FOCUS_Y + FOCUS} V ${BUS_DOWN} H ${downX.value[i]} V ${DOWN_Y}` });
  });
  return out.sort((a, b) => (a.line === "dashed" ? -1 : 1) - (b.line === "dashed" ? -1 : 1));
});

function abbr(type: string): string {
  const k = `warroom.dtype_short.${type}`;
  return te(k) ? t(k) : t("warroom.dtype_short.other");
}
function typeName(type: string): string {
  const k = `warroom.dtype.${type}`;
  return te(k) ? t(k) : type;
}
function tone(n: { anomalies: number | null }): string {
  return n.anomalies ? "coral" : "lilac";
}
const EV = ["asserted", "monitored", "learned", "inferred"] as const;
/** 右側連線清單只列放得下的幾條（再多會被切成半行），其餘寫「+N」 */
const LINKS_SHOWN = 4;
const shownLinks = computed(() => (f.value.links ?? []).slice(0, LINKS_SHOWN));
const moreLinks = computed(() => Math.max((f.value.links?.length ?? 0) - LINKS_SHOWN, 0) + (f.value.more_links ?? 0));
</script>

<template>
  <Transition :name="pageTrans" mode="out-in"><div :key="data.page" class="tp">
    <div class="tp-left">
      <div v-if="f.state === 'too_large'" class="wr-empty">{{ t("warroom.topo_too_large", { n: f.devices, limit: f.limit }) }}</div>
      <div v-else-if="f.state !== 'ok' || !f.device" class="wr-empty">{{ t("warroom.no_topology") }}</div>
      <div v-else class="tp-canvas" :style="{ width: `${W}px` }" data-testid="wr-topo">
        <svg class="tp-lines" :width="W" height="640" :viewBox="`0 0 ${W} 640`" aria-hidden="true">
          <path v-for="(p, i) in paths" :key="i" :d="p.d" :class="`ln-${p.line}`" fill="none" />
        </svg>

        <!-- 上一層：名稱在方塊右邊 -->
        <div v-for="(n, i) in f.up" :key="n.id" class="tp-node tp-node--up" :style="{ left: `${upX[i] - BOX / 2}px`, top: `${UP_Y}px` }"
             data-testid="wr-topo-up">
          <div class="tp-box" :class="`bg-${tone(n)}`" :style="{ width: `${BOX}px`, height: `${BOX}px` }">{{ abbr(n.type) }}</div>
          <WrName :text="n.name" class="tp-name tp-name--side" />
        </div>
        <div v-if="f.more_up" class="tp-more" :style="{ left: `${W - 120}px`, top: `${UP_Y + 20}px` }">+{{ f.more_up }}</div>

        <!-- 中心設備 -->
        <div class="tp-node tp-node--focus" :style="{ left: `${FOCUS_X - FOCUS / 2}px`, top: `${FOCUS_Y}px` }">
          <div class="tp-box tp-box--focus" :class="f.device.anomalies ? 'bg-coral' : 'bg-accent'"
               :style="{ width: `${FOCUS}px`, height: `${FOCUS}px` }">{{ abbr(f.device.type) }}</div>
          <div class="tp-focus__t">
            <WrName :text="f.device.name" class="tp-name tp-name--focus" />
            <div class="tp-focus__m">{{ [typeName(f.device.type), f.device.ip].filter(Boolean).join(" · ") }}</div>
          </div>
        </div>

        <!-- 下一層：名稱在方塊下方，再下面是它底下還接了幾台 -->
        <div v-for="(n, i) in f.down" :key="n.id" class="tp-node tp-node--down"
             :style="{ left: `${downX[i] - downSlot / 2}px`, top: `${DOWN_Y}px`, width: `${downSlot}px` }" data-testid="wr-topo-down">
          <div class="tp-box" :class="[`bg-${tone(n)}`, { peer: n.peer }]" :style="{ width: `${BOX}px`, height: `${BOX}px` }">{{ abbr(n.type) }}</div>
          <WrName :text="n.name" class="tp-name tp-name--below" />
          <!-- 底下還接的：網路設備＋終端（伺服器、一般裝置不畫在圖上，只算數量） -->
          <div v-if="n.below + (n.endpoints ?? 0)" class="tp-below">
            <span v-for="k in Math.min(n.below + (n.endpoints ?? 0), 6)" :key="k" class="tp-dot" />
            <span class="tp-below__n">{{ n.below + (n.endpoints ?? 0) > 6 ? `+${n.below + (n.endpoints ?? 0) - 6}` : "" }}</span>
          </div>
        </div>
        <div v-if="f.more_down" class="tp-more" :style="{ left: `${W - 100}px`, top: `${DOWN_Y + 20}px` }">+{{ f.more_down }}</div>

        <div class="tp-legend">
          <span><svg width="64" height="10"><line x1="0" y1="5" x2="64" y2="5" class="ln-solid" /></svg>{{ t("warroom.line_solid") }}</span>
          <span><svg width="64" height="10"><line x1="0" y1="5" x2="64" y2="5" class="ln-dashed" /></svg>{{ t("warroom.line_dashed") }}</span>
          <span><span class="tp-dot" />{{ t("warroom.below_devices") }}</span>
        </div>
      </div>
    </div>

    <aside v-if="f.state === 'ok' && f.device" class="tp-side">
      <div class="tp-side__k">{{ t("warroom.selected_device") }}</div>
      <div class="tp-side__h">
        <div class="tp-box tp-box--side" :class="f.device.anomalies ? 'bg-coral' : 'bg-accent'">{{ abbr(f.device.type) }}</div>
        <WrName :text="f.device.name" class="tp-side__n" />
      </div>
      <dl class="tp-dl">
        <dt>{{ t("warroom.type") }}</dt><dd>{{ [typeName(f.device.type), f.device.vendor, f.device.model].filter(Boolean).join(" · ") }}</dd>
        <dt>IP</dt><dd class="num">{{ f.device.ip ?? "—" }}</dd>
        <dt>{{ t("warroom.endpoints") }}</dt><dd class="num">{{ f.device.endpoints ?? 0 }}</dd>
        <dt>{{ t("warroom.location") }}</dt><dd>{{ f.device.location ?? "—" }}</dd>
        <dt>{{ t("warroom.links") }}</dt>
        <dd class="num">{{ f.device.degree }}
          <span class="tp-ev"><template v-for="e in EV" :key="e"><span v-if="f.evidence?.[e]">{{ t(`warroom.ev_${e}`) }} {{ f.evidence[e] }}</span></template></span>
        </dd>
        <template v-if="f.device.anomalies != null">
          <dt>{{ t("warroom.anomaly_state") }}</dt>
          <dd :class="f.device.anomalies ? 'tone-coral' : 'tone-accent'">
            {{ f.device.anomalies ? t("warroom.open_n", { n: f.device.anomalies }) : t("warroom.state_ok") }}</dd>
        </template>
      </dl>
      <div class="tp-side__k">{{ t("warroom.connections") }}<span v-if="moreLinks" class="wr-unit"> +{{ moreLinks }}</span></div>
      <div class="tp-links">
        <div v-for="n in shownLinks" :key="n.id" class="tp-link">
          <span class="tp-link__n">{{ n.name }}</span>
          <span class="tp-link__p">{{ n.port ?? "" }}</span>
          <span class="tp-link__e" :class="n.line === 'solid' ? 'tone-ink' : 'tone-muted'">{{ t(`warroom.ev_${n.evidence}`) }}</span>
        </div>
      </div>
    </aside>
  </div>
  </Transition>
</template>

<style scoped>
/* 列高固定為可用高度（minmax(0, …)）：某一區內容多時曾把整列撐高，另一區的下半部被推出畫面（10-22） */
.tp { display: grid; grid-template-columns: minmax(0, 1fr) 600px; grid-template-rows: minmax(0, 1fr); gap: 44px; height: 100%; }
.tp-left { position: relative; min-width: 0; }
.tp-canvas { position: relative; height: 100%; }
.tp-lines { position: absolute; left: 0; top: 0; }
.ln-solid { stroke: var(--wr-ink); stroke-width: 3; }
.ln-dashed { stroke: var(--wr-ink); stroke-width: 3; stroke-dasharray: 10 8; opacity: .8; }
.tp-node { position: absolute; display: flex; }
.tp-node--up { align-items: center; gap: 18px; }
.tp-node--focus { align-items: center; gap: 28px; }
.tp-node--down { flex-direction: column; align-items: center; gap: 10px; text-align: center; }
.tp-box { display: flex; align-items: center; justify-content: center; color: #101216; font-family: var(--wr-num);
  font-weight: 800; font-size: 24px; flex: none; box-sizing: border-box; }
.tp-box--focus { font-size: 34px; }
.tp-box.peer { background: transparent !important; border: 3px solid var(--wr-lilac); color: var(--wr-lilac); }
.bg-accent { background: var(--wr-accent); }
.bg-lilac { background: var(--wr-lilac); }
.bg-coral { background: var(--wr-coral); }
.tp-name { color: var(--wr-ink); font-family: var(--wr-num); }
.tp-name--side { font-size: 22px; width: 260px; }
.tp-name--below { font-size: 20px; width: calc(100% - 16px); }
.tp-focus__t { width: 600px; }
.tp-name--focus { font-size: 34px; font-weight: 700; }
.tp-focus__m { font-size: 22px; color: var(--wr-muted); margin-top: 6px; font-family: var(--wr-num); }
.tp-below { display: flex; gap: 6px; align-items: center; margin-top: 4px; }
.tp-dot { display: inline-block; width: 16px; height: 16px; background: var(--wr-lilac); opacity: .75; }
.tp-below__n { font-size: 18px; color: var(--wr-muted); font-family: var(--wr-num); }
.tp-more { position: absolute; font-size: 26px; color: var(--wr-muted); font-family: var(--wr-num); }
.tp-legend { position: absolute; left: 0; bottom: 0; display: flex; gap: 32px; font-size: 20px; color: var(--wr-muted); }
.tp-legend > span { display: inline-flex; align-items: center; gap: 10px; }
.tp-side { border-left: 1px solid var(--wr-line); padding-left: 40px; display: flex; flex-direction: column; gap: 14px; min-width: 0;
  min-height: 0; overflow: hidden; }
.tp-side__k { font-size: 22px; color: var(--wr-muted); }
.tp-side__h { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 22px; align-items: center;
  padding-bottom: 14px; border-bottom: 1px solid var(--wr-line); }
.tp-box--side { width: 96px; height: 96px; font-size: 30px; }
.tp-side__n { font-size: 32px; font-weight: 700; font-family: var(--wr-num); }
.tp-dl { display: grid; grid-template-columns: 130px minmax(0, 1fr); margin: 0; font-size: 24px; }
.tp-dl dt, .tp-dl dd { padding: 8px 0; border-bottom: 1px solid var(--wr-line); margin: 0; }
.tp-dl dt { color: var(--wr-muted); }
.tp-dl dd { overflow-wrap: anywhere; }
.tp-dl .num { font-family: var(--wr-num); }
.tp-ev { display: inline-flex; gap: 12px; margin-left: 14px; font-size: 18px; color: var(--wr-muted); }
.tp-links { display: grid; gap: 0; align-content: start; flex: 1; min-height: 0; overflow: hidden; }
.tp-link { display: grid; grid-template-columns: minmax(0, 1fr) 140px 120px; gap: 12px; font-size: 21px; padding: 7px 0;
  border-bottom: 1px solid var(--wr-line); font-family: var(--wr-num); }
.tp-link__n { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tp-link__p { color: var(--wr-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tp-link__e { text-align: right; white-space: nowrap; }
</style>
