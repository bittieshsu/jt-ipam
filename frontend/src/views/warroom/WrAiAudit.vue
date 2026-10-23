<script setup lang="ts">
/**
 * 11 AI 巡檢結果（2026-10-23）：只播已保存的結果，切頁與輪播都不會重新執行巡檢（後端 warroom/ai_audit.py）。
 *
 * - 次列：巡檢狀態（未執行／進行中／完成／部分完成／失敗／未記錄）、最近完成時間、涵蓋範圍、資料新鮮度；
 *   三個大指標可重疊，旁邊寫明不能相加
 * - 左：每頁最多三筆發現（標題、IP／主機名稱、優先程度、處理狀態、證據來源）
 * - 右：輪流聚焦其中一筆的證據與建議，每筆 10 秒；整個戰情室暫停時不換（注入 wrPaused）
 * - 下：資料缺口、下次排程（沒有排程就不寫時間）
 * 不顯示任何可信度百分比或分數；模型寫的建議一律標「待人工確認」。
 */
import { computed, inject, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";
import type { PageData } from "@/api/warroom";
import { pad2 } from "@/utils/warroom";
import {
  AiAuditIcon, FailIcon, InfoIcon, MissingIcon, OkIcon, PendingIcon, ReviewIcon, SyncIcon, WarnIcon,
} from "@/icons";
import WrIco from "./WrIco.vue";
import { usePageTransition } from "./paged";

const props = defineProps<{ data: PageData }>();
const { t, te, locale } = useI18n();
const pageTrans = usePageTransition();
const paused = inject<Ref<boolean>>("wrPaused", ref(false));
const reduce = inject<Ref<boolean>>("wrReduceMotion", ref(false));

interface Subject { kind: "ip" | "device" | "subnet" | "unverified"; ip?: string; hostname?: string | null;
  name?: string; cidr?: string; text?: string; verified: boolean }
interface Fact { at: string | null; source: string; ip: string; kind: "last_seen" | "never_seen" | "mac"; value?: string }
interface Item {
  id: string; rank: number; priority: "high" | "medium" | "low"; category: string; review: string; title: string;
  subjects: Subject[]; more_subjects: number; unverified: number; sources: string[]; facts: Fact[];
  detail: string; note: string; recommendation: string | null; found_at: string | null; seen_at: string | null;
  model: string | null;
}
interface Run {
  status: string; running_since: string | null; error_code: string | null; last_attempt_at: string | null;
  completed_at: string | null; completed_status: string | null; snapshot_at: string | null;
  freshness: "fresh" | "stale" | "no_schedule" | null;
  scope: { subnets: number | null; ips: number | null; capped: boolean };
  schedule: { enabled: boolean; next_at: string | null; due_now: boolean };
}
interface Gap { code: string; n?: number; failed?: number; total?: number }

const run = computed(() => props.data.run as Run);
const findings = computed(() => props.data.findings as { state: string; items: Item[] });
const items = computed(() => findings.value.items ?? []);
const withheld = computed(() => props.data.withheld as { count: number | null } | null);
const gaps = computed(() => (props.data.gaps as Gap[] | undefined) ?? []);
const dismissed = computed(() => (props.data.dismissed as number | undefined) ?? 0);

// ── 右側焦點：每 10 秒換下一筆；暫停時停住；換頁或資料變了從第一筆開始
const FOCUS_MS = 10000;
const focus = ref(0);
let timer: ReturnType<typeof setInterval> | null = null;
function restart() {
  if (timer) clearInterval(timer);
  timer = null;
  if (paused.value || items.value.length < 2) return;
  timer = setInterval(() => { focus.value = (focus.value + 1) % Math.max(items.value.length, 1); }, FOCUS_MS);
}
watch(() => [props.data.page, items.value.map((i) => i.id).join(",")], () => { focus.value = 0; restart(); },
      { immediate: true });
watch(paused, () => restart());
onBeforeUnmount(() => { if (timer) clearInterval(timer); });
function pick(i: number) { focus.value = i; restart(); }
const cur = computed<Item | null>(() => items.value[Math.min(focus.value, items.value.length - 1)] ?? null);
const focusTrans = computed(() => (reduce.value ? "wr-none" : "aa-fade"));

// ── 文字
const hm = (d: Date) => d.toLocaleTimeString(locale.value, { hour: "2-digit", minute: "2-digit", hour12: false });
/** 今天的只寫時間；其他日子加月日（不寫年，電視上看的是近期） */
function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return hm(d);
  // 日期與時間之間不斷行（窄欄位曾把時間單獨擠到下一行）
  return `${d.toLocaleDateString(locale.value, { month: "2-digit", day: "2-digit" })}\u00a0${hm(d)}`;
}
const STATUS_ICON: Record<string, unknown> = {
  not_run: PendingIcon, running: SyncIcon, completed: OkIcon, partial: WarnIcon, failed: FailIcon, unrecorded: InfoIcon,
};
const statusTone = computed(() => ({
  completed: "accent", running: "lilac", partial: "lilac", failed: "coral",
}[run.value.status] ?? "muted"));
const errText = computed(() => {
  const c = run.value.error_code;
  return c && te(`warroom.ai_err_${c}`) ? t(`warroom.ai_err_${c}`) : "";
});
const completedText = computed(() => {
  const r = run.value;
  if (!r.completed_at) {
    return r.last_attempt_at && r.status === "unrecorded" ? t("warroom.ai_last_attempt", { time: when(r.last_attempt_at) })
      : t("warroom.ai_no_completed");
  }
  // 進行中或失敗：顯示的是上一次完成的結果，要講清楚
  if (r.status === "running" || r.status === "failed") return t("warroom.ai_showing_last", { time: when(r.completed_at) });
  return t("warroom.ai_completed_at", { time: when(r.completed_at) });
});
const scopeText = computed(() => {
  const s = run.value.scope;
  if (!run.value.completed_at) return "";
  if (s.subnets == null) return t("warroom.ai_scope_unknown");
  return s.ips == null ? t("warroom.ai_scope_subnets", { subnets: s.subnets })
    : t("warroom.ai_scope", { subnets: s.subnets, ips: s.ips });
});
const freshText = computed(() => {
  const r = run.value;
  if (!r.freshness) return "";
  const base = r.snapshot_at ? t("warroom.ai_snapshot", { time: when(r.snapshot_at) }) : "";
  const tail = t(`warroom.ai_fresh_${r.freshness}`);
  return base ? `${base}${t("warroom.ai_sep")}${tail}` : tail;
});
function emptyText(): string {
  const f = findings.value.state;
  const r = run.value;
  if (f === "withheld") return t("warroom.ai_withheld_all");
  if (f === "empty") return r.completed_status === "partial" ? t("warroom.ai_empty_partial") : t("warroom.ai_empty");
  if (r.status === "failed") return t("warroom.ai_unavail_failed");
  if (r.status === "running") return t("warroom.ai_unavail_running");
  if (r.status === "not_run") return t("warroom.ai_unavail_not_run");
  return t("warroom.ai_unavail_unrecorded");
}
const cat = (c: string) => (te(`ai_audit.cat_${c}`) ? t(`ai_audit.cat_${c}`) : c);
const src = (s: string) => (te(`warroom.ai_src_${s}`) ? t(`warroom.ai_src_${s}`) : s);
const srcList = (it: Item) => (it.sources.length ? it.sources.map(src).join(t("warroom.ai_list_sep")) : t("warroom.ai_src_none"));
function factText(f: Fact): string {
  if (f.kind === "never_seen") return t("warroom.ai_fact_never_seen", { ip: f.ip });
  if (f.kind === "mac") return t("warroom.ai_fact_mac", { ip: f.ip, mac: f.value ?? "" });
  return t("warroom.ai_fact_last_seen", { ip: f.ip });
}
/** 主要對象（清單上那一格）：第一個對到的 IP；沒有就裝置、子網路；全都對不到就標未驗證 */
function lead(it: Item): Subject | null {
  return it.subjects.find((s) => s.kind === "ip") ?? it.subjects.find((s) => s.verified) ?? it.subjects[0] ?? null;
}
const others = (it: Item) => Math.max(it.subjects.length - 1, 0) + it.more_subjects;
/** 清單上的主機名稱用第一段當短別名（完整名稱在右側焦點）；IP 樣式的名稱照原樣 */
const shortHost = (h: string) => (/^[\d.:]+$/.test(h) ? h : h.split(".")[0]);
/** IP 的斷行點：只在 IPv6 的冒號後面（不在一組數字中間斷，也不加省略號） */
const ipParts = (ip: string) => ip.split(/(?<=:)/);
function gapText(g: Gap): string {
  if (g.code === "partial_run") return t("warroom.ai_gap_partial_run", { failed: g.failed ?? "—", total: g.total ?? "—" });
  if (g.code === "sample_cap") return t("warroom.ai_gap_sample_cap", { n: g.n ?? 0 });
  if (g.code === "not_included") return t("warroom.ai_gap_not_included", { n: g.n ?? 0 });
  return g.code;
}
const scheduleText = computed(() => {
  const s = run.value.schedule;
  if (!s.enabled) return t("warroom.ai_no_schedule");
  if (s.due_now) return t("warroom.ai_due_now");
  return s.next_at ? t("warroom.ai_next", { time: when(s.next_at) }) : t("warroom.ai_no_schedule");
});
</script>

<template>
  <div class="aa" data-testid="wr-ai">
    <!-- 次列：巡檢狀態、最近完成、涵蓋範圍、資料新鮮度；右邊提醒三個指標不能相加 -->
    <div class="aa-run" data-testid="wr-ai-run" :data-status="run.status">
      <span class="aa-status" :class="`tone-${statusTone}`">
        <WrIco :icon="(STATUS_ICON[run.status] ?? InfoIcon) as never" />{{ t(`warroom.ai_st_${run.status}`) }}
        <span v-if="run.status === 'running' && run.running_since" class="aa-status__sub">{{ t("warroom.ai_running_since", { time: when(run.running_since) }) }}</span>
        <span v-if="errText" class="aa-status__sub">{{ errText }}</span>
      </span>
      <span class="aa-run__i">{{ completedText }}</span>
      <span v-if="scopeText" class="aa-run__i">{{ scopeText }}</span>
      <span v-if="freshText" class="aa-run__i aa-run__fresh" :class="{ 'tone-coral': run.freshness === 'stale' }" data-testid="wr-ai-fresh">{{ freshText }}</span>
      <span class="aa-run__note">{{ t("warroom.ai_overlap") }}</span>
    </div>

    <div class="aa-body">
      <section class="aa-list">
        <header class="aa-h">
          <div class="wr-h2"><WrIco :icon="AiAuditIcon" />{{ t("warroom.ai_findings") }}<span v-if="data.total" class="wr-h2__n">{{ data.total }}</span></div>
          <div v-if="withheld && findings.state !== 'withheld'" class="aa-withheld" data-testid="wr-ai-withheld">
            {{ withheld.count != null ? t("warroom.ai_withheld_n", { n: withheld.count }) : t("warroom.ai_withheld_some") }}
          </div>
        </header>
        <Transition :name="pageTrans" mode="out-in"><div :key="data.page" class="aa-items">
          <div v-if="!items.length" class="aa-empty" :class="{ warn: findings.state !== 'empty' }" data-testid="wr-ai-empty">
            <WrIco v-if="findings.state !== 'empty'" :icon="WarnIcon" />{{ emptyText() }}
          </div>
          <article v-for="(it, i) in items" :key="it.id" class="aa-item" :class="[`p-${it.priority}`, { on: i === focus }]"
                   data-testid="wr-ai-item" :data-focus="i === focus ? '1' : '0'" @click="pick(i)">
            <div class="aa-item__rank">{{ pad2(it.rank) }}</div>
            <div class="aa-item__body">
              <div class="aa-item__title aa-clamp2">{{ it.title }}</div>
              <div class="aa-item__meta">
                <div class="aa-cell">
                  <div class="aa-k">{{ t("warroom.ai_col_subject") }}</div>
                  <template v-if="lead(it)">
                    <div v-if="lead(it)!.kind === 'ip'" class="aa-ip"><template v-for="(p, k) in ipParts(lead(it)!.ip!)" :key="k">{{ p }}<wbr></template></div>
                    <div v-else-if="lead(it)!.kind === 'unverified'" class="aa-ip aa-unv"><template v-for="(p, k) in ipParts(lead(it)!.text!)" :key="k">{{ p }}<wbr></template></div>
                    <div v-else class="aa-v">{{ lead(it)!.name ?? lead(it)!.cidr }}</div>
                    <div class="aa-sub">
                      <span v-if="lead(it)!.kind === 'ip' && lead(it)!.hostname" class="aa-host">{{ shortHost(lead(it)!.hostname!) }}</span>
                      <span v-if="it.unverified" class="aa-unv-tag">{{ t("warroom.ai_unverified_n", { n: it.unverified }) }}</span>
                      <span v-if="others(it)" class="aa-more">{{ t("warroom.ai_more_subjects", { n: others(it) }) }}</span>
                    </div>
                  </template>
                  <div v-else class="aa-v tone-muted">{{ t("warroom.ai_no_subject") }}</div>
                </div>
                <div class="aa-cell">
                  <div class="aa-k">{{ t("warroom.ai_col_sources") }}</div>
                  <div class="aa-v aa-clamp2">{{ srcList(it) }}</div>
                </div>
                <div class="aa-cell">
                  <div class="aa-k">{{ t("warroom.ai_col_category") }}</div>
                  <div class="aa-v">{{ cat(it.category) }}</div>
                  <div class="aa-sub aa-sub--wrap">{{ t("warroom.ai_found_at", { time: when(it.found_at) }) }}</div>
                </div>
              </div>
            </div>
            <div class="aa-item__tags">
              <span class="wr-tag aa-pri" :class="`p-${it.priority}`">{{ t(`warroom.ai_pri_${it.priority}`) }}</span>
              <span class="wr-tag aa-review">{{ t("warroom.ai_review_open") }}</span>
            </div>
          </article>
        </div></Transition>
      </section>

      <aside class="aa-focus" data-testid="wr-ai-focus">
        <header class="aa-h">
          <div class="wr-h2"><WrIco :icon="ReviewIcon" />{{ t("warroom.ai_focus") }}</div>
          <div v-if="items.length > 1" class="aa-dots" data-testid="wr-ai-focus-pos">
            <span v-for="(it, i) in items" :key="it.id" class="aa-dot" :class="{ on: i === focus }" />
            <span class="aa-dots__t">{{ t("warroom.ai_focus_pos", { i: focus + 1, n: items.length }) }}</span>
          </div>
        </header>
        <Transition :name="focusTrans" mode="out-in">
          <div v-if="cur" :key="cur.id" class="aa-f" :data-id="cur.id" data-testid="wr-ai-focus-item">
            <div class="aa-f__head">
              <span class="aa-f__rank" :class="`p-${cur.priority}`">{{ pad2(cur.rank) }}</span>
              <div class="aa-f__ht">
                <div class="aa-f__title aa-clamp2">{{ cur.title }}</div>
                <div class="aa-f__subj">
                  <!-- 只列第一個對象＋「另 N 項」：以前列兩個 IP 各佔一行，右欄被擠到證據表貼著標題（10-23） -->
                  <template v-for="(s, k) in cur.subjects.slice(0, 1)" :key="k">
                    <span v-if="s.kind === 'ip'" class="aa-f__ipline"><span class="aa-ip aa-ip--sm"><template v-for="(p, j) in ipParts(s.ip!)" :key="j">{{ p }}<wbr></template></span><span v-if="s.hostname" class="aa-f__host" :title="s.hostname">{{ s.hostname }}</span><span v-if="others(cur)" class="aa-more">{{ t("warroom.ai_more_subjects", { n: others(cur) }) }}</span></span>
                    <span v-else-if="s.kind === 'unverified'" class="aa-ip aa-ip--sm aa-unv"><template v-for="(p, j) in ipParts(s.text!)" :key="j">{{ p }}<wbr></template><em>{{ t("warroom.ai_unverified") }}</em></span>
                    <span v-else class="aa-f__name">{{ s.name ?? s.cidr }}<span v-if="others(cur)" class="aa-more">{{ t("warroom.ai_more_subjects", { n: others(cur) }) }}</span></span>
                  </template>
                </div>
              </div>
            </div>
            <div class="aa-f__k">{{ t("warroom.ai_evidence") }}</div>
            <table v-if="cur.facts.length" class="aa-facts">
              <thead><tr><th>{{ t("warroom.ai_col_time") }}</th><th>{{ t("warroom.ai_col_source") }}</th><th>{{ t("warroom.ai_col_content") }}</th></tr></thead>
              <tbody>
                <tr v-for="(f, k) in cur.facts" :key="k">
                  <td class="num">{{ when(f.at) }}</td><td>{{ src(f.source) }}</td><td class="aa-fact">{{ factText(f) }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="aa-f__none">{{ t("warroom.ai_no_facts") }}</div>
            <div v-if="cur.detail || cur.note" class="aa-f__block">
              <div class="aa-f__k">{{ t("warroom.ai_reading") }}<span v-if="cur.model" class="aa-f__model">{{ t("warroom.ai_by_model", { m: cur.model }) }}</span></div>
              <p class="aa-f__p aa-clamp2">{{ cur.detail || cur.note }}</p>
            </div>
            <div v-if="cur.recommendation" class="aa-f__block">
              <div class="aa-f__k">{{ t("warroom.ai_recommend") }}</div>
              <p class="aa-f__p aa-clamp3">{{ cur.recommendation }}</p>
            </div>
          </div>
          <div v-else class="aa-f__none aa-f__none--big">{{ t("warroom.ai_focus_none") }}</div>
        </Transition>
      </aside>
    </div>

    <div class="aa-foot" data-testid="wr-ai-foot">
      <span class="aa-foot__gaps" data-testid="wr-ai-gaps"><WrIco :icon="MissingIcon" />{{ t("warroom.ai_gaps") }}
        <template v-if="gaps.length"><span v-for="(g, k) in gaps" :key="g.code" class="aa-gap">{{ k ? t("warroom.ai_list_sep") : "" }}{{ gapText(g) }}</span></template>
        <span v-else class="aa-gap tone-muted">{{ t("warroom.ai_no_gaps") }}</span>
      </span>
      <span class="aa-foot__i" data-testid="wr-ai-schedule"><WrIco :icon="PendingIcon" />{{ scheduleText }}</span>
      <span v-if="dismissed" class="aa-foot__i">{{ t("warroom.ai_dismissed", { n: dismissed }) }}</span>
      <span class="aa-foot__note">{{ t("warroom.ai_playback_note") }}</span>
    </div>
  </div>
</template>

<style scoped>
.aa { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; gap: 12px; height: 100%; min-height: 0; }
.aa-run { display: flex; align-items: center; font-size: 23px; color: var(--wr-ink); min-width: 0; white-space: nowrap; }
.aa-run > * + * { margin-left: 20px; padding-left: 20px; border-left: 1px solid var(--wr-line); }
.aa-status { display: inline-flex; align-items: center; font-weight: 700; gap: 10px; flex: none; }
.aa-status__sub { font-weight: 400; color: var(--wr-muted); }
.aa-run__i { color: var(--wr-ink); flex: none; }
/* 只有新鮮度那一段可以縮（時間在前面，縮的是說明文字） */
.aa-run__i.aa-run__fresh { flex: 0 1 auto; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.aa-run__note { margin-left: auto !important; color: var(--wr-muted); font-size: 20px; flex: none; }
/* 右欄放證據表與兩段文字，給寬一點（10-23「右邊太擠」）；左邊三張卡片用不到那麼寬 */
.aa-body { display: grid; grid-template-columns: minmax(0, 1fr) 820px; gap: 36px; min-height: 0; }
.aa-h { display: flex; justify-content: space-between; align-items: baseline; gap: 20px; margin-bottom: 4px; }
.aa-h .wr-h2 { font-size: 30px; }
.aa-withheld { font-size: 20px; color: var(--wr-lilac); text-align: right; line-height: 1.25; max-width: 560px; }
.aa-list { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; border-top: 1px solid var(--wr-line);
  padding-top: 10px; }
.aa-items { display: flex; flex-direction: column; min-height: 0; }
.aa-empty { font-size: 32px; color: var(--wr-muted); padding: 56px 0; display: flex; align-items: center; }
.aa-empty.warn { color: var(--wr-ink); }
.aa-empty.warn .wr-ico { color: var(--wr-lilac); }
/* 一筆一列、沒有底色（不用色條）；焦點那筆加細框 */
.aa-item { display: grid; grid-template-columns: 96px minmax(0, 1fr) auto; gap: 18px; padding: 10px 14px 10px 6px;
  border-bottom: 1px solid var(--wr-line); outline: 2px solid transparent; outline-offset: -2px; cursor: pointer; }
.aa-item.on { outline-color: var(--wr-accent); }
.aa-item__rank { font-family: var(--wr-num); font-size: 68px; font-weight: 300; line-height: .95; color: var(--wr-lilac);
  font-variant-numeric: tabular-nums; }
.p-high .aa-item__rank, .aa-f__rank.p-high { color: var(--wr-coral); }
.p-low .aa-item__rank, .aa-f__rank.p-low { color: var(--wr-ink); }
.aa-item__body { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.aa-item__title { font-size: 28px; font-weight: 700; line-height: 1.2; overflow: hidden; overflow-wrap: anywhere; }
.aa-item__meta { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, .9fr) minmax(0, 1fr); gap: 20px; }
.aa-cell { min-width: 0; }
.aa-cell + .aa-cell { padding-left: 18px; border-left: 1px solid var(--wr-line); }
.aa-k { font-size: 17px; color: var(--wr-muted); }
.aa-v { font-size: 22px; line-height: 1.25; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aa-clamp2 { white-space: normal; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
/* IP 不加省略號：太長（IPv6）就在冒號、點之間換行 */
.aa-ip { font-family: var(--wr-num); font-size: 25px; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.2;
  word-break: keep-all; }
.aa-ip--sm { font-size: 24px; }
.aa-unv { color: var(--wr-muted); }
.aa-unv em { font-style: normal; font-size: 18px; margin-left: 8px; padding: 1px 6px; border: 1px dashed var(--wr-outline); font-family: inherit;
  white-space: nowrap; }
.aa-sub { font-size: 19px; color: var(--wr-muted); display: flex; gap: 12px; min-width: 0; white-space: nowrap; overflow: hidden; }
.aa-sub--wrap { white-space: normal; word-break: keep-all; }
.aa-host { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.aa-unv-tag { color: var(--wr-ink); border: 1px dashed var(--wr-outline); padding: 0 6px; flex: none; }
.aa-more { flex: none; }
.aa-item__tags { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
.aa-pri { color: var(--wr-lilac); }
.aa-pri.p-high { color: var(--wr-coral); }
.aa-pri.p-low { color: var(--wr-ink); }
.aa-review { color: var(--wr-lilac); font-weight: 400; border-width: 1px; }
/* 右側焦點 */
.aa-focus { border-left: 1px solid var(--wr-line); padding-left: 32px; display: flex; flex-direction: column; min-width: 0; min-height: 0;
  overflow: hidden; }
.aa-dots { display: flex; align-items: center; gap: 8px; flex: none; }
.aa-dot { width: 14px; height: 14px; border: 2px solid var(--wr-outline); box-sizing: border-box; }
.aa-dot.on { background: var(--wr-accent); border-color: var(--wr-accent); }
.aa-dots__t { font-family: var(--wr-num); font-size: 20px; color: var(--wr-muted); margin-left: 6px; }
.aa-f { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.aa-f__head { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 16px; align-items: start; }
.aa-f__rank { font-family: var(--wr-num); font-size: 56px; font-weight: 300; line-height: 1; color: var(--wr-lilac); }
.aa-f__title { font-size: 26px; font-weight: 700; line-height: 1.22; overflow-wrap: anywhere; }
.aa-f__subj { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; min-width: 0; }
/* 一行：IP 永遠完整，主機名稱太長才用省略號（以前會折成兩行把右欄擠爆） */
.aa-f__ipline { display: flex; flex-wrap: nowrap; align-items: baseline; gap: 0 14px; min-width: 0; }
.aa-f__ipline .aa-ip { flex: none; }
.aa-f__host { font-size: 19px; color: var(--wr-muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aa-f__ipline .aa-more, .aa-f__name .aa-more { font-size: 19px; color: var(--wr-muted); margin-left: 14px; flex: none; }
.aa-f__name { font-size: 21px; }
.aa-f__k { font-size: 20px; font-weight: 700; color: var(--wr-ink); }
.aa-facts { width: 100%; border-collapse: collapse; font-size: 20px; table-layout: fixed; }
.aa-facts th { font-weight: 400; color: var(--wr-muted); font-size: 17px; text-align: left; padding: 3px 8px; border-bottom: 1px solid var(--wr-deep-line); }
.aa-facts th:first-child { width: 150px; }   /* 跨日時是「10/23 23:22」，120px 會截掉 */
.aa-facts th:nth-child(2) { width: 120px; }
.aa-facts td { padding: 6px 8px; border-bottom: 1px solid var(--wr-line); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.aa-facts td.aa-fact { white-space: normal; overflow-wrap: anywhere; }
.aa-facts td.num { font-family: var(--wr-num); font-variant-numeric: tabular-nums; }
.aa-f__none { font-size: 20px; color: var(--wr-muted); }
.aa-f__none--big { font-size: 28px; padding-top: 40px; }
.aa-f__block { border-top: 1px solid var(--wr-line); padding-top: 10px; }
.aa-f__p { margin: 4px 0 0; font-size: 21px; line-height: 1.38; color: var(--wr-ink); }
.aa-clamp3 { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; }
.aa-f__model { font-weight: 400; font-size: 18px; color: var(--wr-muted); margin-left: 14px; }
.aa-fade-enter-active { transition: opacity .35s ease-out; }
.aa-fade-leave-active { transition: opacity .18s ease-in; }
.aa-fade-enter-from, .aa-fade-leave-to { opacity: 0; }
/* 頁內底列：資料缺口、下次排程；缺口那一段可以縮，其他固定 */
.aa-foot { display: flex; align-items: center; font-size: 21px; color: var(--wr-ink); border-top: 1px solid var(--wr-line);
  padding-top: 8px; white-space: nowrap; min-width: 0; }
.aa-foot > * + * { margin-left: 18px; padding-left: 18px; border-left: 1px solid var(--wr-line); }
.aa-foot__i { display: inline-flex; align-items: center; flex: none; }
.aa-foot__gaps { flex: 0 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; display: block; }
.aa-foot .wr-ico { color: var(--wr-muted); }
.aa-gap:first-of-type { margin-left: 10px; }
.aa-foot__note { margin-left: auto !important; color: var(--wr-muted); font-size: 19px; flex: none; }
</style>
