<template>
  <n-card>
    <template #header>
      <CardTitle :icon="AnomalyIcon" :text="t('ai_audit.title')">
        <n-tag v-if="summary" size="small" round :bordered="false">{{ summary.total }}</n-tag>
      </CardTitle>
    </template>
    <template #header-extra>
      <n-space :size="8">
        <n-button size="small" :loading="loading" @click="load">
          <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("common.refresh") }}
        </n-button>
        <n-button v-if="canRun" size="small" type="primary" :loading="running" @click="runNow">
          <template #icon><n-icon><TestIcon /></n-icon></template>{{ t("ai_audit.run_now") }}
        </n-button>
      </n-space>
    </template>

    <!-- 這是 LLM 的推測，不是查核過的事實。放在最上面，不是藏在角落的小字。 -->
    <n-alert type="warning" :bordered="false" :show-icon="true" style="margin-bottom:14px">
      {{ t("ai_audit.disclaimer") }}
    </n-alert>

    <!-- 執行中的進度：巡檢會跑好幾分鐘、切成很多批，只有轉圈的按鈕等於什麼都沒說 -->
    <div v-if="running" class="run-box">
      <div class="run-head">
        <n-spin :size="14" />
        <span class="run-stage">{{ stageText }}</span>
        <span class="run-elapsed">{{ elapsedText }}</span>
      </div>
      <n-progress type="line" :percentage="percent" :height="8"
                  :processing="percent < 100" :show-indicator="percent > 0" />
      <!-- 講清楚可以走：不講的話，使用者會以為要一直停在這頁盯著 -->
      <div class="run-bg">{{ t("ai_audit.run_background") }}</div>
      <div v-if="progressHint" class="run-hint">
        {{ progressHint }}
        <span v-if="writing">
          · {{ t(writePhase === "thinking" ? "ai_audit.progress_thinking"
                                           : "ai_audit.progress_written", { n: writing }) }}
        </span>
      </div>
    </div>

    <!-- 上一次執行失敗要一直看得到。用 toast 的話，人不在畫面前就永遠不會知道出過事 -->
    <n-alert v-else-if="lastError" type="error" closable :bordered="false"
             style="margin-bottom:14px" @close="lastError = null">
      {{ t("ai_audit.run_failed") }}{{ lastError }}
    </n-alert>

    <!-- 跟儀表板同一組數字：從儀表板點進來之後，看到的第一眼要能對得起來 -->
    <div v-if="summary" class="sev-row">
      <div v-for="sv in (['high', 'medium', 'low'] as const)" :key="sv"
           class="sev-cell" :class="[`sev-${sv}`, { on: severity === sv }]"
           @click="toggleSeverity(sv)">
        <div class="sev-n">{{ summary.counts[sv] }}</div>
        <div class="sev-l">{{ t(`ai_audit.sev_${sv}`) }}</div>
      </div>
      <!-- 發現數不等於問題規模：一筆「命名不一致」可能點名 30 個位址 -->
      <div class="sev-cell sev-ips">
        <div class="sev-n">{{ summary.ip_count }}</div>
        <div class="sev-l">{{ t("ai_audit.related_ips") }}</div>
      </div>
    </div>

    <n-space :size="10" align="center" style="margin-bottom:14px" :wrap="true">
      <n-radio-group v-model:value="status" size="small" @update:value="load">
        <n-radio-button value="open">
          <n-icon :component="AnomalyIcon" class="rb-ic" />{{ t("ai_audit.st_open") }}
        </n-radio-button>
        <n-radio-button value="dismissed">
          <n-icon :component="DismissIcon" class="rb-ic" />{{ t("ai_audit.st_dismissed") }}
        </n-radio-button>
        <n-radio-button value="all">
          <n-icon :component="ListIcon" class="rb-ic" />{{ t("common.all") }}
        </n-radio-button>
      </n-radio-group>
      <n-select v-model:value="severity" size="small" clearable style="width:160px"
                :options="sevOptions" :placeholder="t('ai_audit.all_severity')"
                :render-label="renderSevLabel" :render-tag="renderSevTag"
                @update:value="load" />
      <span v-if="summary?.last_run_at" class="hint">
        {{ t("ai_audit.last_run", { at: fmtDateTime(summary.last_run_at) }) }}
      </span>
    </n-space>

    <n-empty v-if="!loading && !rows.length" :description="t('ai_audit.none')" style="margin:28px 0" />

    <n-spin :show="loading">
      <div v-for="f in rows" :key="f.id" class="fx">
        <div class="fx-head">
          <n-tag :type="sevType(f.severity)" size="small" round :bordered="false">
            {{ t(`ai_audit.sev_${f.severity}`) }}
          </n-tag>
          <n-tag size="small" round :bordered="false">{{ t(`ai_audit.cat_${f.category}`) }}</n-tag>
          <h3 class="fx-title">{{ f.title }}</h3>
          <span class="fx-spacer" />
          <span class="fx-when">{{ fmtDateTime(f.created_at) }}</span>
          <n-button v-if="canRun && f.status === 'open'" size="tiny" secondary
                    @click="dismiss(f.id)">
            <template #icon><n-icon><DismissIcon /></n-icon></template>
            {{ t("ai_audit.dismiss") }}
          </n-button>
          <n-button v-else-if="canRun" size="tiny" secondary type="primary"
                    @click="restore(f.id)">
            <template #icon><n-icon><RestoreIcon /></n-icon></template>
            {{ t("ai_audit.restore") }}
          </n-button>
        </div>
        <p v-if="f.detail" class="fx-detail">{{ f.detail }}</p>
        <div v-if="f.recommendation" class="fx-rec">
          <span class="fx-rec-tag">{{ t("ai_audit.recommendation") }}</span>
          <span>{{ f.recommendation }}</span>
        </div>
        <!-- 依據資料一定要看得到：沒有它，上面那段話就無從查證。
             IP 直接做成連結 —— 查證要能一鍵翻到那筆紀錄，不是叫人自己去搜尋。 -->
        <div v-if="f.evidence" class="fx-ev">
          <span class="fx-ev-label">{{ t("ai_audit.evidence") }}</span>
          <n-popover v-for="ip in evIps(f.evidence)" :key="ip" trigger="hover"
                     :delay="120" placement="top" @update:show="(v: boolean) => v && loadIp(ip)">
            <template #trigger>
              <span class="fx-ip" @click="goIp(ip)">{{ ip }}</span>
            </template>
            <IpPeek :ip="ip" :data="ipCache[ip]" />
          </n-popover>
          <span v-for="[k, v] in evRest(f.evidence)" :key="k" class="fx-ev-kv">
            <b>{{ evKeyLabel(k) }}</b>{{ v }}
          </span>
        </div>
      </div>
    </n-spin>
  </n-card>
</template>

<script setup lang="ts">
import { computed, h, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import {
  NAlert, NButton, NCard, NEmpty, NIcon, NPopover, NProgress, NRadioButton,
  NRadioGroup, NSelect, NSpace, NSpin, NTag, useMessage, type SelectOption,
} from "naive-ui";
import { AnomalyIcon, DismissIcon, ListIcon, RefreshIcon, TestIcon, RestoreIcon } from "@/icons";
import IpPeek, { type IpPeekData } from "@/components/IpPeek.vue";
import { listAddresses } from "@/api/addresses";
import CardTitle from "@/components/CardTitle.vue";
import { fmtDateTime } from "@/utils/datetime";
import { apiErrMsg } from "@/api/client";
import {
  dismissAIFindings, getAIAuditStatus, getAIAuditSummary, listAIFindings,
  restoreAIFindings, runAIAudit,
  type AIAuditSummary, type AIAuditTask, type AIFinding,
} from "@/api/system";
import { useAuthStore } from "@/stores/auth";

const { t } = useI18n();
const msg = useMessage();
const auth = useAuthStore();
const router = useRouter();

const rows = ref<AIFinding[]>([]);
const summary = ref<AIAuditSummary | null>(null);
const loading = ref(false);
const running = ref(false);
const status = ref("open");
const severity = ref<string | null>(null);

// 執行進度
const stage = ref<string>("");
const progressHint = ref("");
const lastError = ref<string | null>(null);
const startedAt = ref(0);
const elapsed = ref(0);
const ipsSeen = ref(0);
const modelSeen = ref("");
const writing = ref(0);
const writePhase = ref("");

const stageText = computed(() => t(`ai_audit.stage_${stage.value || "collecting"}`));

// 百分比由後端算好寫進作業列 —— 前端自己再算一次的話，兩邊遲早不一致
const percent = ref(0);

const elapsedText = computed(() => {
  const s = elapsed.value;
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
});

// 執行與忽略是管理員操作（後端仍是唯一真相，這裡只是不要給看得到卻按不動的按鈕）
const canRun = computed(() => !!auth.me?.is_admin);

const sevOptions = computed(() => ["high", "medium", "low"].map((s) => ({
  label: t(`ai_audit.sev_${s}`), value: s,
})));

function sevType(s: string) {
  return s === "high" ? "error" : s === "medium" ? "warning" : "default";
}

const SEV_COLOR: Record<string, string> = {
  high: "#d03050", medium: "#f0a020", low: "#909399",
};

// 下拉裡的高／中／低也要有顏色 —— 選單跟清單上的標籤指的是同一件事，顏色不一致會讓人
// 以為是兩套分類
function sevDot(v: string) {
  return h("span", {
    style: `display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;`
      + `background:${SEV_COLOR[v] ?? "#909399"};flex:0 0 auto`,
  });
}

function renderSevLabel(opt: SelectOption) {
  return h("div", { style: "display:flex;align-items:center" },
    [sevDot(String(opt.value)), h("span", {}, String(opt.label))]);
}

function renderSevTag({ option }: { option: SelectOption }) {
  return h("div", { style: "display:flex;align-items:center" },
    [sevDot(String(option.value)), h("span", {}, String(option.label))]);
}

function toggleSeverity(sv: string) {
  severity.value = severity.value === sv ? null : sv;
  void load();
}

// evidence 的鍵名是模型產生的英文；常見的幾個翻成中文，其餘照原樣顯示
// （硬要翻不認得的鍵只會翻出更難懂的東西）
const EV_KEYS: Record<string, string> = {
  note: "ai_audit.ev_note", reason: "ai_audit.ev_reason",
  hostnames: "ai_audit.ev_hostnames", subnets: "ai_audit.ev_subnets",
  devices: "ai_audit.ev_devices", details: "ai_audit.ev_details",
};

function evKeyLabel(k: string) {
  const key = EV_KEYS[k.toLowerCase()];
  return key ? t(key) : k;
}

// IP 詳細卡片：滑過去才查，查過就快取（同一筆發現裡同一個 IP 只查一次）
const ipCache = ref<Record<string, IpPeekData | undefined>>({});

async function loadIp(ip: string) {
  if (ipCache.value[ip] !== undefined) return;
  ipCache.value = { ...ipCache.value, [ip]: undefined };
  try {
    const r = await listAddresses({ q: ip, exact: true, pageSize: 1 });
    const a = r.items[0];
    ipCache.value = {
      ...ipCache.value,
      [ip]: a
        ? {
            hostname: a.hostname,
            state: a.state, effective_status: (a as any).effective_status ?? null,
            mac: a.mac, device_name: (a as any).device_name ?? null,
            description: a.description,
            is_gateway: (a as any).is_gateway ?? null,
            is_dhcp_server: (a as any).is_dhcp_server ?? null,
            in_dhcp_range: (a as any).in_dhcp_range ?? null,
            last_seen: (a as any).last_seen_scanner ?? (a as any).last_seen_librenms ?? null,
          }
        : { missing: true },
    };
  } catch {
    ipCache.value = { ...ipCache.value, [ip]: { missing: true } };
  }
}

// evidence 的內容是模型產生的，鍵名不保證是哪些 —— IP 清單挑出來做成連結，
// 其餘一律照原樣列出。看不懂的形狀也要顯示出來，不能因為不認得就藏起來。
function evIps(ev: Record<string, unknown> | null): string[] {
  const v = ev?.ips;
  return Array.isArray(v) ? v.map((x) => String(x)).slice(0, 30) : [];
}

function evRest(ev: Record<string, unknown> | null): [string, string][] {
  if (!ev) return [];
  return Object.entries(ev)
    .filter(([k]) => k !== "ips")
    .map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
}

function goIp(ip: string) {
  router.push({ name: "addresses", query: { q: ip, exact: "1" } }).catch(() => {});
}

async function load() {
  loading.value = true;
  try {
    const [f, s] = await Promise.all([
      listAIFindings({ status: status.value, severity: severity.value || undefined, page_size: 200 }),
      getAIAuditSummary(),
    ]);
    rows.value = f.items;
    summary.value = s;
  } catch (e) { msg.error(apiErrMsg(e)); } finally { loading.value = false; }
}

let pollTimer: number | undefined;

function tick() {
  if (startedAt.value) elapsed.value = Math.floor((Date.now() - startedAt.value) / 1000);
}

/** 把作業狀態畫到進度區。作業列是唯一真相 —— 頁面只是在看它。 */
function applyTask(task: AIAuditTask | null) {
  if (!task || (task.status !== "running" && task.status !== "pending")) {
    running.value = false;
    stopPolling();
    if (task?.status === "failed") {
      lastError.value = task.error ?? t("ai_audit.run_unknown_error");
    } else if (task?.summary?.error) {
      // 部分批次失敗但仍有發現 → 作業算成功，可是結果不完整，這件事一定要講
      lastError.value = task.summary.error;
    }
    return false;
  }
  running.value = true;
  lastError.value = null;
  percent.value = task.progress;
  if (task.started_at) startedAt.value = new Date(task.started_at).getTime();
  const live = task.summary?.live;
  if (live) {
    stage.value = live.stage ?? "analyzing";
    if (live.ips) ipsSeen.value = live.ips;
    if (live.model) modelSeen.value = live.model;
    if (live.total) {
      progressHint.value = t("ai_audit.progress_hint", {
        batch: `${live.batch ?? (live.current ?? 0) + 1}/${live.total}`,
        ips: ipsSeen.value, model: modelSeen.value, found: live.found ?? 0,
      });
    }
    writing.value = live.written ?? 0;
    writePhase.value = live.phase ?? "";
  }
  return true;
}

function startPolling() {
  stopPolling();
  tick();
  pollTimer = window.setInterval(async () => {
    tick();
    try {
      const { task } = await getAIAuditStatus();
      const stillRunning = applyTask(task);
      if (!stillRunning) {
        // 跑完了 —— 把發現抓回來，並照實回報結果（成功幾筆 / 失敗原因）
        await load();
        if (task?.status === "succeeded") {
          msg.success(t("ai_audit.run_done", { n: task.summary?.findings ?? 0 }));
        }
      }
    } catch { /* 暫時抓不到狀態不必中斷輪詢 */ }
  }, 2000);
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer);
  pollTimer = undefined;
}

async function runNow() {
  running.value = true;
  lastError.value = null;
  stage.value = "collecting";
  percent.value = 2;
  progressHint.value = "";
  writing.value = 0;
  startedAt.value = Date.now();
  elapsed.value = 0;
  try {
    await runAIAudit();
    startPolling();
  } catch (e) {
    running.value = false;
    lastError.value = apiErrMsg(e);
  }
}

async function dismiss(id: string) {
  try {
    await dismissAIFindings([id]);
    // 講清楚這一按的影響範圍：之後同一件事都不會再跳出來，不是只藏這一次
    msg.success(t("ai_audit.dismiss_done"), { duration: 4000 });
    await load();
  } catch (e) { msg.error(apiErrMsg(e)); }
}

async function restore(id: string) {
  try {
    await restoreAIFindings([id]);
    await load();
  } catch (e) { msg.error(apiErrMsg(e)); }
}

onMounted(async () => {
  await load();
  // 進來就先問一次作業狀態：巡檢是背景跑的，可能是別人（或上一個分頁）觸發的
  try {
    const { task } = await getAIAuditStatus();
    if (applyTask(task)) startPolling();
  } catch { /* 沒權限或還沒跑過 */ }
});

onBeforeUnmount(stopPolling);
</script>

<style scoped>
.hint { font-size: 12px; color: var(--n-text-color-disabled); }
.rb-ic { vertical-align: -2px; margin-right: 5px; }

/* 嚴重度統計：跟儀表板那塊同一組數字、同一組顏色。點一下＝篩選 */
.sev-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.sev-cell {
  padding: 10px; border-radius: 6px; cursor: pointer; text-align: center;
  background: var(--n-color-embedded, rgba(128, 128, 128, .06));
  border: 1px solid transparent; transition: border-color .12s ease, opacity .12s ease;
}
.sev-cell:hover { opacity: .85; }
.sev-cell.on { border-color: currentColor; }
.sev-n { font-size: 20px; font-weight: 700; line-height: 1.2; }
.sev-l { font-size: 12px; color: var(--n-text-color-disabled); margin-top: 2px; }
.sev-high { color: #d03050; }
.sev-medium { color: #f0a020; }
.sev-low { color: var(--n-text-color-3); }
.sev-ips { cursor: default; color: var(--n-text-color-3); }
.sev-ips:hover { opacity: 1; }
.sev-high .sev-n { color: #d03050; }
.sev-medium .sev-n { color: #f0a020; }
.run-box {
  margin-bottom: 14px; padding: 12px 14px; border-radius: 6px;
  background: var(--n-color-embedded, rgba(128, 128, 128, .06));
}
.run-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.run-stage { font-size: 13px; font-weight: 600; }
.run-elapsed {
  margin-left: auto; font-size: 12px; font-variant-numeric: tabular-nums;
  color: var(--n-text-color-disabled);
}
.run-hint { margin-top: 6px; font-size: 12px; color: var(--n-text-color-disabled); }
.run-bg { margin-top: 6px; font-size: 12px; color: var(--n-color-target, #36ad6a); }
.fx { padding: 16px 0 18px; border-bottom: 1px solid var(--n-border-color); }
.fx:last-child { border-bottom: none; }
.fx-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
/* 標題自己一級，不跟標籤和時間擠在同一個字級 */
.fx-title { margin: 0; font-size: 15px; font-weight: 600; line-height: 1.4; }
.fx-spacer { flex: 1 1 auto; }
.fx-when { font-size: 12px; color: var(--n-text-color-disabled); white-space: nowrap; }
/* 內文縮排對齊標題、限制行寬 —— 一行拉到 1400px 寬，眼睛跳行會找不到位置 */
.fx-detail {
  margin: 0 0 8px; font-size: 13.5px; line-height: 1.9;
  max-width: 78ch; color: var(--n-text-color-2);
}
.fx-rec {
  display: flex; gap: 8px; align-items: baseline;
  margin-bottom: 8px; font-size: 13.5px; line-height: 1.9; max-width: 78ch;
}
.fx-rec-tag {
  flex: 0 0 auto; font-size: 11.5px; padding: 1px 8px; border-radius: 4px;
  background: rgba(24, 160, 88, .12); color: var(--n-color-target, #36ad6a);
  position: relative; top: -1px;
}
.fx-ev {
  margin-top: 8px; padding: 6px 10px; border-radius: 4px;
  background: var(--n-color-embedded, rgba(128, 128, 128, .08));
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 8px;
  font-size: 12px; line-height: 1.7;
}
.fx-ev-label { color: var(--n-text-color-disabled); }
.fx-ip {
  font-family: var(--font-mono, monospace); cursor: pointer;
  color: var(--n-color-target, #36ad6a); text-decoration: underline dotted;
}
.fx-ip:hover { text-decoration: underline; }
.fx-ev-kv { color: var(--n-text-color-3); word-break: break-word; }
.fx-ev-kv b { font-weight: 500; color: var(--n-text-color-disabled); margin-right: 4px; }
</style>
