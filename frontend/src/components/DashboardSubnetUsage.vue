<template>
  <n-card class="dash-subnet-usage" data-testid="subnet-usage-card">
    <template #header>
      <CardTitle :icon="UsageIcon" :text="t('dashboard.card_ip_usage')">
        <n-tag v-if="mode === 'pinned'" size="small" round :bordered="false">
          {{ items.length }} / {{ max }}
        </n-tag>
      </CardTitle>
    </template>

    <!-- 控制列放內文最上方（卡片標題列只放標題＋數量） -->
    <div class="usage-head">
      <span v-if="mode === 'auto'" class="usage-hint" data-testid="subnet-usage-auto">
        {{ t("dashboard.usage_auto_hint", { n: max }) }}
      </span>
      <span v-else />
      <n-button size="small" data-testid="subnet-usage-edit" @click="openPicker">
        <template #icon><n-icon :component="SettingsIcon" /></template>
        {{ t("dashboard.usage_pick") }}
      </n-button>
    </div>

    <n-spin :show="loading">
      <n-empty v-if="!loading && !items.length" size="small" :description="t('dashboard.usage_empty')"
               class="usage-empty" />
      <div v-else class="gauges">
        <div v-for="it in items" :key="it.subnet_id" class="gauge" role="link" tabindex="0"
             data-testid="subnet-usage-gauge" :title="tileTitle(it)"
             @click="goSubnet(it.subnet_id)" @keydown.enter="goSubnet(it.subnet_id)">
          <svg class="gauge-svg" viewBox="0 0 100 100" aria-hidden="true">
            <circle cx="50" cy="50" r="40" fill="none" stroke="currentColor" stroke-width="10" stroke-opacity="0.12" />
            <circle v-if="it.ip_version === 4 && gaugeArc(pctOf(it), it.used) > 0"
                    cx="50" cy="50" r="40" fill="none" :stroke="colorOf(pctOf(it))" stroke-width="10"
                    pathLength="100" :stroke-dasharray="`${gaugeArc(pctOf(it), it.used)} 100`"
                    stroke-linecap="round" transform="rotate(-90 50 50)" class="gauge-arc" />
            <text x="50" :y="it.ip_version === 4 ? 52 : 47" text-anchor="middle" dominant-baseline="middle"
                  class="gauge-pct" :font-size="centerText(it).length > 5 ? 17 : 21"
                  :fill="it.ip_version === 4 ? colorOf(pctOf(it)) : 'currentColor'">
              {{ centerText(it) }}
            </text>
            <text v-if="it.ip_version === 6" x="50" y="64" text-anchor="middle" dominant-baseline="middle"
                  font-size="10" fill="currentColor" opacity="0.6">
              {{ t("dashboard.usage_used") }}
            </text>
          </svg>
          <div class="gauge-name">{{ it.description || it.cidr }}</div>
          <div v-if="it.description" class="gauge-cidr">{{ it.cidr }}</div>
          <div class="gauge-num">
            <template v-if="it.ip_version === 4">
              {{ it.used.toLocaleString() }} / {{ it.total.toLocaleString() }}
            </template>
            <template v-else>{{ t("dashboard.usage_v6") }}</template>
          </div>
        </div>
      </div>
    </n-spin>

    <!-- 選子網路（伺服器端搜尋：子網路很多的站台不能整包載入） -->
    <n-modal v-model:show="pickerShow" preset="card" style="max-width: 600px" :title="t('dashboard.usage_pick_title')">
      <n-alert type="info" :show-icon="true" style="margin-bottom: 12px">
        {{ t("dashboard.usage_pick_hint", { n: max }) }}
      </n-alert>
      <n-select
        v-model:value="draft"
        multiple
        filterable
        clearable
        remote
        :options="selectOptions"
        :loading="optLoading"
        :placeholder="t('dashboard.usage_pick_ph')"
        data-testid="subnet-usage-select"
        @search="onSearch"
      />
      <div v-if="draft.length > max" class="over">{{ t("dashboard.usage_over", { n: max }) }}</div>
      <template #footer>
        <div class="picker-foot">
          <n-button :disabled="saving || !draft.length" data-testid="subnet-usage-reset" @click="draft = []">
            <template #icon><n-icon :component="RestoreIcon" /></template>
            {{ t("dashboard.usage_reset") }}
          </n-button>
          <n-space justify="end">
            <n-button :disabled="saving" @click="pickerShow = false">
              <template #icon><n-icon :component="CancelIcon" /></template>
              {{ t("common.cancel") }}
            </n-button>
            <n-button type="primary" :loading="saving" :disabled="saving || draft.length > max"
                      data-testid="subnet-usage-save" @click="save">
              <template #icon><n-icon :component="SaveIcon" /></template>
              {{ t("common.save") }}
            </n-button>
          </n-space>
        </div>
      </template>
    </n-modal>
  </n-card>
</template>

<script setup lang="ts">
/**
 * 儀表板「IP 使用率」：最多 4 個子網路各自一個小圓環（客戶 2026-10-23）。
 * 以前是全部子網路加總的一個圓環 —— 只要有一個 /8 就永遠是 0%，網段一多就沒有用途。
 * 選哪幾個存在 user_preferences.pinned["usage_subnets"]（跟可用性追蹤卡片同一套釘選）；
 * 沒選 → 後端自動挑可見子網路裡使用率最高的 4 個。
 */
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import {
  NAlert, NButton, NCard, NEmpty, NIcon, NModal, NSelect, NSpace, NSpin, NTag,
  useMessage, useThemeVars,
} from "naive-ui";
import CardTitle from "@/components/CardTitle.vue";
import { CancelIcon, RestoreIcon, SaveIcon, SettingsIcon, UsageIcon } from "@/icons";
import { getSubnetUsageCards, type UsageSubnet } from "@/api/dashboard";
import { listSubnets } from "@/api/subnets";
import { usePinned } from "@/composables/usePinned";
import { fmtUsagePct, gaugeArc, usageLevel, usagePct } from "@/utils/subnetUsageGauge";

const { t } = useI18n();
const router = useRouter();
const msg = useMessage();
const theme = useThemeVars();
const pin = usePinned("usage_subnets");

const items = ref<UsageSubnet[]>([]);
const mode = ref<"pinned" | "auto">("auto");
const max = ref(4);
const loading = ref(false);

async function load() {
  loading.value = true;
  try {
    const r = await getSubnetUsageCards();
    items.value = r.items;
    mode.value = r.mode;
    max.value = r.max;
    for (const it of r.items) labelCache.value[it.subnet_id] = subnetLabel(it.cidr, it.description);
  } catch {
    items.value = [];
  } finally {
    loading.value = false;
  }
}

/** 顏色跟子網路清單頁的使用率長條一致（主題的 success／warning／error，深色模式自動跟著換） */
function colorOf(pct: number): string {
  const lv = usageLevel(pct);
  return lv === "error" ? theme.value.errorColor : lv === "warning" ? theme.value.warningColor : theme.value.successColor;
}

/** 未四捨五入的比例：後端的 used_pct 只到小數兩位，/12 裡用了 5 個會變成 0 */
function pctOf(it: UsageSubnet): number {
  return usagePct(it.used, it.total);
}

/** 圓心：IPv4 顯示百分比；IPv6 位址空間太大，百分比永遠接近 0，改顯示已用數量 */
function centerText(it: UsageSubnet): string {
  return it.ip_version === 4 ? fmtUsagePct(pctOf(it)) : it.used.toLocaleString();
}

function tileTitle(it: UsageSubnet): string {
  const name = it.description ? `${it.description} (${it.cidr})` : it.cidr;
  const who = it.customer_label ? ` · ${it.customer_label}` : "";
  return it.ip_version === 4
    ? `${name}${who}\n${it.used.toLocaleString()} / ${it.total.toLocaleString()} (${fmtUsagePct(pctOf(it))})`
    : `${name}${who}\n${t("dashboard.usage_v6_tip", { n: it.used.toLocaleString() })}`;
}

function goSubnet(id: string) {
  router.push({ name: "subnet-detail", params: { id } }).catch(() => {});
}

// ── 挑選 ──
const pickerShow = ref(false);
const draft = ref<string[]>([]);
const options = ref<{ label: string; value: string }[]>([]);
const optLoading = ref(false);
const saving = ref(false);
// id → 顯示文字：搜尋會把 options 整個換掉，已選那幾個不在結果裡時 n-select 會把 UUID 當標籤畫出來
const labelCache = ref<Record<string, string>>({});

function subnetLabel(cidr: string, description: string | null | undefined): string {
  return description ? `${cidr}  ${description}` : cidr;
}

const selectOptions = computed(() => {
  const inList = new Set(options.value.map((o) => o.value));
  const missing = draft.value
    .filter((id) => !inList.has(id))
    .map((id) => ({ label: labelCache.value[id] ?? id, value: id }));
  // 已經選滿：其他選項反灰（已選的還是可以點掉）
  const full = draft.value.length >= max.value;
  return [...missing, ...options.value].map((o) => ({
    ...o, disabled: full && !draft.value.includes(o.value),
  }));
});

async function loadOptions(q?: string) {
  optLoading.value = true;
  try {
    const res = await listSubnets({ page: 1, pageSize: 50, q: q?.trim() || undefined });
    options.value = res.items.map((s) => {
      const label = subnetLabel(String(s.cidr), s.description);
      labelCache.value[s.id] = label;
      return { label, value: s.id };
    });
  } catch {
    options.value = [];
  } finally {
    optLoading.value = false;
  }
}

let searchTimer: ReturnType<typeof setTimeout> | undefined;
function onSearch(q: string) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => void loadOptions(q), 250);
}

function openPicker() {
  draft.value = mode.value === "pinned" ? items.value.map((i) => i.subnet_id) : [];
  pickerShow.value = true;
  void loadOptions();
}

async function save() {
  saving.value = true;
  try {
    await pin.setAll(draft.value.slice(0, max.value));
    await load();
    pickerShow.value = false;
  } catch {
    msg.error(t("errors.network"));
  } finally {
    saving.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.dash-subnet-usage { height: 100%; }
.usage-head {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  flex-wrap: wrap; margin-bottom: 12px;
}
.usage-hint { font-size: 12.5px; opacity: 0.7; min-width: 0; flex: 1 1 200px; }
.usage-empty { padding: 24px 0; }
/* 寬的時候 4 個排一列，窄的時候 2×2，手機 1～2 欄。auto-fit：選不到 4 個時平均撐滿整列，
   不在右邊留空格（10-24 使用者；auto-fill 會保留空欄） */
.gauges {
  display: grid; gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
}
.gauge {
  display: flex; flex-direction: column; align-items: center; min-width: 0;
  padding: 10px 8px 12px; border-radius: 10px; cursor: pointer;
  border: 1px solid rgba(127, 127, 127, 0.16);
  background: rgba(127, 127, 127, 0.04);
  transition: background .15s, transform .12s;
}
.gauge:hover { background: rgba(127, 127, 127, 0.09); transform: translateY(-1px); }
.gauge:focus-visible { outline: 2px solid #18a058; outline-offset: 2px; }
.gauge-svg { width: 96px; height: 96px; display: block; margin-bottom: 6px; }
.gauge-arc { transition: stroke-dasharray .5s ease, stroke .3s ease; }
.gauge-pct { font-weight: 700; font-variant-numeric: tabular-nums; }
.gauge-name, .gauge-cidr, .gauge-num {
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center;
}
.gauge-name { font-size: 13px; font-weight: 600; line-height: 1.35; }
.gauge-cidr {
  font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; line-height: 1.35;
  opacity: 0.65;
}
.gauge-num {
  margin-top: 3px; font-family: var(--font-mono, ui-monospace, monospace); font-size: 11.5px;
  font-variant-numeric: tabular-nums; opacity: 0.8;
}
.over { margin-top: 8px; font-size: 12px; color: #d03050; }
.picker-foot { display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
</style>
