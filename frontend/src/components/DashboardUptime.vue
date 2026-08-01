<template>
  <n-card size="small" class="dash-uptime">
    <template #header>
      <CardTitle :icon="IPChangesIcon" :text="t('uptime.dash_title')">
        <n-tag v-if="ids.length" size="small" round :bordered="false">
          {{ ids.length }} / {{ MAX }}
        </n-tag>
      </CardTitle>
    </template>
    <template #header-extra>
      <n-button size="small" @click="openPicker">
        <template #icon><n-icon :component="SettingsIcon" /></template>
        {{ t("uptime.dash_edit") }}
      </n-button>
    </template>

    <n-empty v-if="!ids.length" :description="t('uptime.dash_empty')" size="small">
      <template #extra>
        <n-button size="small" @click="openPicker">{{ t("uptime.dash_pick") }}</n-button>
      </template>
    </n-empty>

    <n-spin v-else :show="loading">
      <div class="rows">
        <div v-for="r in rows" :key="r.ip_id" class="row">
          <a class="row-label" :title="r.hostname || r.ip" @click="goIp(r.ip_id)">
            <span class="row-ip">{{ r.ip }}</span>
            <span v-if="r.hostname" class="row-host">{{ r.hostname }}</span>
          </a>
          <div class="row-bar">
            <n-tooltip v-for="d in r.items" :key="d.date" trigger="hover" :delay="80">
              <template #trigger>
                <span class="bar" :class="`bar-${d.status}`" />
              </template>
              <div>
                <div style="font-weight:600">{{ d.date }}</div>
                <div>{{ t(`uptime.st_${d.status}`) }}</div>
              </div>
            </n-tooltip>
          </div>
          <span class="row-pct">
            <template v-if="r.uptime_pct !== null">{{ r.uptime_pct }}%</template>
            <template v-else>—</template>
          </span>
        </div>
      </div>
      <div class="rows-foot">
        <span>{{ t("uptime.days_ago", { n: DAYS }) }}</span>
        <span>{{ t("uptime.today") }}</span>
      </div>
    </n-spin>

    <!-- 選 IP -->
    <n-modal
      v-model:show="pickerShow"
      preset="card"
      style="max-width: 640px"
      :title="t('uptime.dash_edit')"
    >
      <n-alert type="info" :show-icon="true" style="margin-bottom: 12px">
        {{ t("uptime.dash_hint", { n: MAX }) }}
      </n-alert>
      <n-select
        v-model:value="draft"
        multiple
        filterable
        clearable
        :options="selectOptions"
        :loading="optLoading"
        :max-tag-count="6"
        :placeholder="t('uptime.dash_ph')"
        @search="onSearch"
      />
      <div v-if="draft.length > MAX" class="over">
        {{ t("uptime.dash_over", { n: MAX }) }}
      </div>
      <template #footer>
        <n-space justify="end">
          <n-button @click="pickerShow = false">{{ t("common.cancel") }}</n-button>
          <n-button type="primary" :disabled="draft.length > MAX" @click="save">
            {{ t("common.save") }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </n-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import {
  NAlert, NButton, NCard, NEmpty, NIcon, NModal, NSelect, NSpace, NSpin, NTag, NTooltip,
} from "naive-ui";
import CardTitle from "@/components/CardTitle.vue";
import { IPChangesIcon, SettingsIcon } from "@/icons";
import { apiClient } from "@/api/client";
import { listAddresses } from "@/api/addresses";
import { usePinned } from "@/composables/usePinned";

const MAX = 30;          // 儀表板區塊上限
const DAYS = 90;

const { t } = useI18n();
const router = useRouter();
// 存在 user_preferences.pinned["uptime_ips"]（既有的通用釘選機制，免加欄位）
const { ids, toggle } = usePinned("uptime_ips");

interface Day { date: string; status: "up" | "partial" | "down" | "unknown" }
interface Row {
  ip_id: string; ip: string; hostname: string | null;
  items: Day[]; uptime_pct: number | null;
}

const rows = ref<Row[]>([]);
const loading = ref(false);
const pickerShow = ref(false);
const draft = ref<string[]>([]);
const ipOptions = ref<{ label: string; value: string }[]>([]);
const optLoading = ref(false);
// id → 顯示文字。搜尋會把 ipOptions 整個換掉，已選那筆的 option 就不見了；
// n-select 找不到 option 時會把 value（UUID）當標籤畫出來 —— 所以要自己留一份。
const labelCache = ref<Record<string, string>>({});

function ipLabel(ip: string, hostname: string | null): string {
  return hostname ? `${ip} — ${hostname}` : ip;
}

/** 搜尋結果 ＋ 已選但不在結果裡的項目（後者補在最前面，才不會被搜尋洗掉標籤）。 */
const selectOptions = computed(() => {
  const inList = new Set(ipOptions.value.map((o) => o.value));
  const missing = draft.value
    .filter((id) => !inList.has(id))
    .map((id) => ({ label: labelCache.value[id] ?? t("uptime.dash_ip_gone"), value: id }));
  return [...missing, ...ipOptions.value];
});

async function load() {
  if (!ids.value.length) { rows.value = []; return; }
  loading.value = true;
  try {
    const { data } = await apiClient.post("/api/v1/addresses/uptime/batch", {
      ip_ids: ids.value.slice(0, MAX), days: DAYS,
    });
    rows.value = data.items ?? [];
    // 批次結果本身就帶 ip/hostname → 已追蹤的那些不必再打一次 API 才有標籤
    for (const r of rows.value) labelCache.value[r.ip_id] = ipLabel(r.ip, r.hostname);
  } catch {
    rows.value = [];
  } finally {
    loading.value = false;
  }
}

async function loadOptions(q?: string) {
  optLoading.value = true;
  try {
    const res = await listAddresses({ page: 1, pageSize: 200, q: q || undefined });
    ipOptions.value = res.items.map((a: any) => {
      const label = ipLabel(a.ip, a.hostname);
      labelCache.value[a.id] = label;
      return { label, value: a.id };
    });
  } catch {
    ipOptions.value = [];
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
  draft.value = [...ids.value];
  pickerShow.value = true;
  void loadOptions();
}

function save() {
  // usePinned 只提供 toggle → 用差集把它推到 draft 的內容
  const before = new Set(ids.value);
  const after = new Set(draft.value.slice(0, MAX));
  for (const id of before) if (!after.has(id)) toggle(id);
  for (const id of after) if (!before.has(id)) toggle(id);
  pickerShow.value = false;
  void load();
}

function goIp(id: string) {
  router.push({ name: "address-detail", params: { id } }).catch(() => {});
}

onMounted(load);
watch(ids, load, { deep: true });
</script>

<style scoped>
.dash-uptime { width: 100%; }
.rows { display: flex; flex-direction: column; gap: 6px; }
/* 標籤固定寬、長條吃掉剩下全部 → 每一列的長條左右對齊 */
.row { display: grid; grid-template-columns: 190px minmax(0, 1fr) 56px; align-items: center; gap: 10px; }
.row-label {
  display: flex; flex-direction: column; line-height: 1.25; min-width: 0;
  cursor: pointer; text-decoration: none;
}
.row-label:hover .row-ip { color: #18a058; }
.row-ip { font-size: 13px; font-weight: 600; color: var(--n-text-color); }
.row-host {
  font-size: 11.5px; color: var(--n-text-color-disabled);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.row-bar { display: flex; gap: 1px; height: 22px; }
.bar { flex: 1 1 0; min-width: 0; border-radius: 0; cursor: pointer; transition: opacity .12s ease; }
.bar:hover { opacity: .72; }
.bar-up { background: #18a058; }
.bar-partial { background: #f0a020; }
.bar-down { background: #d03050; }
.bar-unknown { background: var(--n-border-color); }
.row-pct { font-size: 12px; font-weight: 600; text-align: right; color: var(--n-text-color); }
.rows-foot {
  display: flex; justify-content: space-between; margin-top: 8px;
  font-size: 12px; color: var(--n-text-color-disabled);
  /* 對齊長條區（跳過左側標籤欄與右側百分比欄） */
  padding-left: 200px; padding-right: 66px;
}
.over { margin-top: 8px; font-size: 12px; color: #d03050; }
</style>
