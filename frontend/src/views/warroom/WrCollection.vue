<script setup lang="ts">
/**
 * 07 採集健康：2×2 四台掃描代理（名稱兩行、條帶、三組固定數值欄），底部整合來源與最近備份。
 * - 條帶一格一輪，填色＝耗時／週期比；沒有資料的格子是虛線，不畫成正常
 * - 逾時的代理不顯示像是即時的「執行」秒數，改顯示最後一次成功的時間，並標「資料逾時」
 */
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import type { PageData } from "@/api/warroom";
import { agoParts } from "@/utils/warroom";
import WrMatrix from "./WrMatrix.vue";
import WrName from "./WrName.vue";
import { usePageTransition } from "./paged";
import WrIco from "./WrIco.vue";
import { SECTION_ICONS } from "./wrIcons";

const props = defineProps<{ data: PageData }>();
const pageTrans = usePageTransition();     // 換資料頁只換分頁的那一區（paged.ts）
const { t } = useI18n();

interface Agent {
  id: string; name: string; description: string | null; version: string | null; state: string;
  last_seen_s: number | null; duration_s: number | null; interval_s: number | null; last_success_at: string | null;
  strip: { state: string }[];
}
interface Integ { kind: string; label: string; name: string; state: string }
const agents = computed(() => props.data.agents as { state: string; items?: Agent[] });
const integ = computed(() => props.data.integrations as { state: string; items?: Integ[]; total?: number });

/** 整合來源全部列出（10-22），但最多兩排：兩排放不下時從尾端收起、補「另 N 個」。
 *  順序是有狀況的在前（伺服器排好），所以收起的一定是正常的。換行曾經蓋到上面的代理卡片。 */
const footEl = ref<HTMLElement | null>(null);
const hiddenSrc = ref(0);
const shownSrc = computed(() => {
  const items = integ.value.items ?? [];
  return hiddenSrc.value ? items.slice(0, Math.max(items.length - hiddenSrc.value, 0)) : items;
});
const moreSrc = computed(() => (integ.value.total ?? 0) - shownSrc.value.length);
async function fitSources() {
  hiddenSrc.value = 0;
  await nextTick();
  const el = footEl.value;
  if (!el) return;
  const chips = [...el.querySelectorAll<HTMLElement>("[data-src]")];
  const rows = [...new Set(chips.map((c) => c.offsetTop))].sort((a, b) => a - b);
  if (rows.length <= 2) return;
  const over = chips.filter((c) => c.offsetTop > rows[1]).length;
  hiddenSrc.value = over + 1;                   // 多收一個，留位置給「另 N 個」
}
onMounted(fitSources);
watch(() => props.data.integrations, fitSources);

function hm(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}
const stale = (a: Agent) => a.state === "stale" || a.state === "never";
</script>

<template>
  <div class="col">
    <div v-if="agents.state === 'forbidden'" class="wr-empty">{{ t("warroom.forbidden") }}</div>
    <div v-else-if="!agents.items?.length" class="wr-empty">{{ t("warroom.no_agents") }}</div>
    <Transition v-else :name="pageTrans" mode="out-in"><div :key="data.page" class="col-grid">
      <section v-for="a in agents.items" :key="a.id" class="col-q" data-testid="wr-agent">
        <header class="col-h">
          <span class="col-h__ico"><WrIco :icon="SECTION_ICONS.agent" /></span><WrName :text="a.name" class="col-h__n" />
          <span v-if="a.state !== 'ok'" class="wr-tag" :class="`lv-${a.state}`">{{ t(`warroom.agent_${a.state}`) }}</span>
        </header>
        <div class="col-meta">
          <span class="col-meta__l">{{ [a.description, a.version ? `v${a.version}` : null].filter(Boolean).join(" · ") }}</span>
          <!-- 條帶要說明是什麼（10-22「綠色區塊代表什麼看不出來」）；顏色對照在右上圖例 -->
          <span class="col-meta__r" data-testid="wr-strip-caption">{{ t("warroom.strip_caption", { n: a.strip.length }) }}</span>
        </div>
        <WrMatrix :cells="a.strip.map((c) => c.state)" :cols="24" :size="30" :gap="6" :hollow="stale(a)" />
        <div class="col-nums">
          <div class="col-num">
            <div class="col-num__l">{{ stale(a) ? t("warroom.last_success") : t("warroom.run") }}</div>
            <div v-if="stale(a)" class="col-num__v">{{ hm(a.last_success_at) }}</div>
            <div v-else class="col-num__v">{{ a.duration_s != null ? Math.round(a.duration_s) : "—" }}<span v-if="a.duration_s != null" class="col-num__u">{{ t("warroom.unit_s") }}</span></div>
          </div>
          <div class="col-num">
            <div class="col-num__l">{{ t("warroom.interval") }}</div>
            <div class="col-num__v">{{ a.interval_s ?? "—" }}<span v-if="a.interval_s != null" class="col-num__u">{{ t("warroom.unit_s") }}</span></div>
          </div>
          <div class="col-num">
            <div class="col-num__l">{{ t("warroom.last_report") }}</div>
            <div class="col-num__v">{{ agoParts(a.last_seen_s, t).value }}<span class="col-num__u">{{ agoParts(a.last_seen_s, t).unit }}</span></div>
          </div>
        </div>
      </section>
    </div>
    </Transition>

    <footer ref="footEl" class="col-foot" data-testid="wr-sources">
      <span class="col-foot__k"><WrIco :icon="SECTION_ICONS.sources" />{{ t("warroom.sources") }}</span>
      <!-- 標籤區自己一欄：換行時第二排對齊第一個標籤，不會鑽到標題底下（10-23） -->
      <div v-if="integ.state === 'ok'" class="col-foot__list">
        <span v-for="r in shownSrc" :key="r.kind + r.name" class="col-src" data-src>
          <span class="wr-dot" :class="`d-${r.state}`" />{{ r.name }}
          <!-- 列出全部來源（10-22）：正常的只靠圓點，有狀況的才寫出狀態，一排才放得下 -->
          <span v-if="r.state !== 'ok'" class="col-src__s" :class="`d-${r.state}`">{{ t(`warroom.src_${r.state}`) }}</span>
        </span>
        <span v-if="moreSrc > 0" class="col-src col-src--more" data-src>{{ t("warroom.more_n", { n: moreSrc }) }}</span>
      </div>
      <span v-else class="wr-muted">{{ integ.state === 'forbidden' ? t("warroom.forbidden") : t("warroom.no_sources") }}</span>
    </footer>
  </div>
</template>

<style scoped>
.col { display: flex; flex-direction: column; height: 100%; }
.col-grid { display: grid; grid-template-columns: 1fr 1fr; grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); flex: 1; min-height: 0; }
.col-q { padding: 14px 36px 12px 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; min-height: 0;
  border-bottom: 1px solid var(--wr-line); }
.col-q:nth-child(2n) { padding-left: 36px; padding-right: 0; border-left: 1px solid var(--wr-line); }
.col-h { display: flex; justify-content: space-between; gap: 20px; align-items: start; }
.col-h__ico { font-size: 34px; color: var(--wr-muted); line-height: 1.2; margin-right: -14px; }
.col-h > .col-h__n { flex: 1; min-width: 0; }
.col-h__n { font-size: 34px; font-weight: 700; color: var(--wr-ink); }
.col-meta { min-height: 26px; font-size: 21px; color: var(--wr-muted); font-family: var(--wr-num); margin-top: -4px;
  display: flex; justify-content: space-between; gap: 16px; }
.col-meta__l { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.col-meta__r { white-space: nowrap; flex: none; }
.col-nums { display: grid; grid-template-columns: repeat(3, 1fr); margin-top: auto; }
.col-num { padding-left: 18px; border-left: 1px solid var(--wr-line); }
.col-num:first-child { padding-left: 0; border-left: 0; }
.col-num__l { font-size: 21px; color: var(--wr-muted); }
.col-num__v { font-family: var(--wr-num); font-weight: 300; font-size: 58px; line-height: 1; color: var(--wr-ink);
  font-variant-numeric: tabular-nums; }
.col-num__u { font-size: 24px; margin-left: 8px; font-weight: 500; color: var(--wr-ink); }
.col-foot { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: start; column-gap: 14px;
  padding-top: 16px; border-top: 1px solid var(--wr-line); font-size: 22px; white-space: nowrap; overflow: hidden; flex: none; }
.col-foot__k { line-height: 40px; }
.col-foot__list { display: flex; flex-wrap: wrap; gap: 8px 10px; min-width: 0; }
.col-foot__k { font-weight: 700; font-size: 24px; }
/* 每個來源一個標籤：燈號與名稱包在同一個框裡，才不會分不清燈號屬於左邊還是右邊（10-22） */
.col-src { display: inline-flex; align-items: center; gap: 8px; padding: 3px 14px 3px 10px;
  /* 邊框要看得出一顆一顆（10-22）：--wr-line 在深色底上太淡 */
  border: 1px solid color-mix(in srgb, var(--wr-stop) 75%, transparent);
  border-radius: 999px; background: rgba(255, 255, 255, .05); }
.col-src--more { border-style: dashed; }
.col-src__s { font-weight: 600; }
.col-src--more { color: var(--wr-muted); }
.d-ok { color: var(--wr-accent); }
.d-error { color: var(--wr-coral); }
.d-stale, .d-never { color: var(--wr-lilac); }
</style>
