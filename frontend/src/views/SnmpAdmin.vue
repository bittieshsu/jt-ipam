<script setup lang="ts">
/**
 * SNMP 收集（管理 → 外部系統整合 → SNMP，migration 0217）。
 *
 * 掃描代理對「伺服器指派的網路設備」做唯讀 SNMP 讀取：路由器的 ARP 表（補隔著路由器的網段的 MAC）、
 * 交換器的 MAC 表、LLDP／CDP 鄰居、介面。三個頁籤：
 * - 收集目標：一台裝置一筆（測試／立即收集／記錄／編輯／刪除，照 Wazuh 頁）
 * - 憑證：社群字串與 v3 帳密集中在這裡，不在每台裝置上填；換代理也不必重填
 * - 收集記錄：所有目標最近的收集與測試
 * 編輯表單是共用元件（SnmpTargetForm／SnmpCredentialForm），裝置詳情與子網路頁用的是同一份。
 */
import { computed, h, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRoute, useRouter, RouterLink } from "vue-router";
import {
  NCard, NDataTable, NSpace, NButton, NTag, NIcon, NTooltip, NAlert, NTabs, NTabPane, NPopconfirm,
  NInput, NSelect, NModal, useMessage, type DataTableColumns,
} from "naive-ui";
import {
  SnmpIcon, PlusIcon, EditIcon, DeleteIcon, RefreshIcon, SyncIcon, TestIcon, AuditIcon, KeyIcon,
  CancelIcon, ScanAgentsIcon,
} from "@/icons";
import { apiErrMsg } from "@/api/client";
import {
  listAllSnmpTargets, deleteSnmpTarget, listSnmpCredentials, deleteSnmpCredential, listSnmpRuns, listSnmpAgents,
  SNMP_SECTIONS, type SnmpTarget, type SnmpCredential, type SnmpRun, type SnmpAgentInfo,
} from "@/api/snmp";
import { autoSort } from "@/composables/useTableSort";
import { useColumnPrefs } from "@/composables/useColumnPrefs";
import { useTablePagination } from "@/composables/useTablePagination";
import { useTableQuickFilter } from "@/composables/useTableQuickFilter";
import { useSnmpCollect } from "@/composables/useSnmpCollect";
import ColumnPicker from "@/components/ColumnPicker.vue";
import ExportButton from "@/components/ExportButton.vue";
import SnmpTargetForm from "@/components/SnmpTargetForm.vue";
import SnmpCredentialForm from "@/components/SnmpCredentialForm.vue";
import SnmpTestResult from "@/components/SnmpTestResult.vue";
import SnmpRunsModal from "@/components/SnmpRunsModal.vue";
import SnmpRunsTable from "@/components/SnmpRunsTable.vue";
import { fmtDateTime } from "@/utils/datetime";
import { srvText } from "@/utils/wsError";
import { withExportValue } from "@/utils/tableExport";
import {
  errText, HEALTH_TAG, sectionLabel, sectionTagType, sectionText, targetHealth,
} from "@/utils/snmp";

const { t } = useI18n();
const msg = useMessage();
const route = useRoute();
const router = useRouter();

type Tab = "targets" | "credentials" | "runs";
const TABS: Tab[] = ["targets", "credentials", "runs"];
const tab = ref<Tab>(TABS.includes(route.query.tab as Tab) ? (route.query.tab as Tab) : "targets");
watch(tab, (v) => {
  void router.replace({ query: { ...route.query, tab: v === "targets" ? undefined : v } });
  if (v === "runs" && !runsLoaded.value) void loadRuns();
});

// ─── 資料 ───
const targets = ref<SnmpTarget[]>([]);
const creds = ref<SnmpCredential[]>([]);
const agents = ref<SnmpAgentInfo[]>([]);
const minAgentVersion = ref("");
const loading = ref(false);
/** 從掃描代理頁點進來：只看這台代理負責的收集目標（?agent=<id>） */
const agentFilter = ref<string | null>(typeof route.query.agent === "string" ? route.query.agent : null);
const agentFilterName = computed(() => agents.value.find((a) => a.agent_id === agentFilter.value)?.name ?? "");
function clearAgentFilter() {
  agentFilter.value = null;
  void router.replace({ query: { ...route.query, agent: undefined } });
  void loadTargets();
}

async function loadTargets() {
  targets.value = await listAllSnmpTargets(agentFilter.value ? { agent_id: agentFilter.value } : {});
}
async function refresh() {
  loading.value = true;
  try {
    const [, c, a] = await Promise.all([loadTargets(), listSnmpCredentials(), listSnmpAgents()]);
    creds.value = c;
    agents.value = a.items;
    minAgentVersion.value = a.min_agent_version;
    if (runsLoaded.value) await loadRuns();
  } catch (e) {
    msg.error(apiErrMsg(e));
  } finally {
    loading.value = false;
  }
}
const noCapableAgent = computed(() => agents.value.length > 0 && !agents.value.some((a) => a.capable && a.enabled));

const { query: targetQ, filtered: targetsFiltered } = useTableQuickFilter(targets);
const pg = useTablePagination();
const credPg = useTablePagination();

// ─── 對話框 ───
const targetFormShow = ref(false);
const editingTarget = ref<SnmpTarget | null>(null);
const credFormShow = ref(false);
const editingCred = ref<SnmpCredential | null>(null);
const testShow = ref(false);
const testTarget = ref<SnmpTarget | null>(null);
const runsShow = ref(false);
const runsTarget = ref<SnmpTarget | null>(null);

function openCreateTarget() { editingTarget.value = null; targetFormShow.value = true; }
function openEditTarget(r: SnmpTarget) { editingTarget.value = r; targetFormShow.value = true; }
function openTest(r: SnmpTarget) { testTarget.value = r; testShow.value = true; }
function openRuns(r: SnmpTarget) { runsTarget.value = r; runsShow.value = true; }
function openCreateCred() { editingCred.value = null; credFormShow.value = true; }
function openEditCred(c: SnmpCredential) { editingCred.value = c; credFormShow.value = true; }

const { busy: collectBusy, collect } = useSnmpCollect(() => void refresh());

async function delTarget(r: SnmpTarget) {
  try {
    await deleteSnmpTarget(r.id);
    msg.success(t("common.deleted"));
    await refresh();
  } catch (e) { msg.error(apiErrMsg(e)); }
}

// 使用中的憑證刪不掉：後端回 409＋前 50 台；這裡列出用它的裝置，點了就到裝置頁
const inUseShow = ref(false);
const inUseCred = ref<SnmpCredential | null>(null);
const inUseTargets = ref<SnmpTarget[]>([]);
async function delCred(c: SnmpCredential) {
  try {
    await deleteSnmpCredential(c.id);
    msg.success(t("common.deleted"));
    await refresh();
  } catch (e) {
    const code = (e as { response?: { data?: { detail_code?: string } } })?.response?.data?.detail_code;
    msg.error(apiErrMsg(e));
    if (code === "snmp_credential_in_use") {
      inUseCred.value = c;
      inUseTargets.value = await listAllSnmpTargets({ credential_id: c.id }).catch(() => []);
      inUseShow.value = true;
    }
  }
}

// ─── 收集記錄 ───
const runs = ref<SnmpRun[]>([]);
const runsLoaded = ref(false);
const runsLoading = ref(false);
const runResult = ref<string | null>(null);
const runKind = ref<string | null>(null);
async function loadRuns() {
  runsLoading.value = true;
  try {
    runs.value = await listSnmpRuns({ limit: 500, result: runResult.value, kind: runKind.value });
    runsLoaded.value = true;
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { runsLoading.value = false; }
}
watch([runResult, runKind], () => void loadRuns());
const resultOptions = computed(() => ["running", "success", "partial", "failed"].map((v) => ({
  value: v, label: t(`snmp.result_${v}`) })));
const kindOptions = computed(() => ["collect", "test"].map((v) => ({ value: v, label: t(`snmp.kind_${v}`) })));

// ─── 表格 ───
function iconAction(icon: unknown, label: string, onClick: () => void, opts: { type?: string; loading?: boolean;
  disabled?: boolean; testid?: string } = {}) {
  return h(NTooltip, null, {
    trigger: () => h(NButton, {
      size: "small", quaternary: true, type: opts.type as never, loading: opts.loading,
      disabled: opts.disabled, "data-testid": opts.testid,
      onClick: (e: MouseEvent) => { e.stopPropagation(); onClick(); },
    }, { icon: () => h(NIcon, null, () => h(icon as never)) }),
    default: () => label,
  });
}

const TARGET_COLS = ["device", "mgmt_ip", "credential", "sections", "collector", "last_run_at", "last_error",
  "status", "actions"];
const tp = useColumnPrefs("snmp_targets", TARGET_COLS, TARGET_COLS);
const targetPicker = computed(() => [
  { key: "device", label: t("cols.device") },
  { key: "mgmt_ip", label: t("snmp.mgmt_ip") },
  { key: "credential", label: t("snmp.credential") },
  { key: "sections", label: t("snmp.collect_items") },
  { key: "collector", label: t("snmp.collector") },
  { key: "last_run_at", label: t("snmp.last_collect") },
  { key: "last_error", label: t("cols.last_error") },
  { key: "status", label: t("cols.status") },
  { key: "actions", label: t("cols.actions") },
]);

function collectorCell(r: SnmpTarget) {
  if (r.collector_error) {
    return h(NTooltip, null, {
      trigger: () => h(NTag, { size: "small", type: "error", bordered: false }, () => t("snmp.collector_error_tag")),
      default: () => srvText(r.collector_error),
    });
  }
  const mode = t(`snmp.collector_${r.collector}`);
  const tip = [r.effective_agent_name, r.collector_subnet_cidr].filter(Boolean).join(" · ");
  return h(NTooltip, null, {
    trigger: () => h("span", null, `${mode} · ${r.effective_agent_name ?? "—"}`),
    default: () => tip || mode,
  });
}

function sectionsCell(r: SnmpTarget) {
  const on = SNMP_SECTIONS.filter((s) => r[`collect_${s}`]);
  return h(NSpace, { size: 3, wrap: true }, () => on.map((s) => {
    const res = r.last_summary?.sections?.[s];
    return h(NTooltip, null, {
      trigger: () => h(NTag, { size: "tiny", bordered: false, type: res ? sectionTagType(res.status) : "info" },
        () => sectionLabel(s, t)),
      default: () => sectionText(res, t),
    });
  }));
}

const allTargetCols = computed<DataTableColumns<SnmpTarget>>(() => autoSort([
  withExportValue({
    title: t("cols.device"), key: "device", minWidth: 160, ellipsis: { tooltip: true },
    sorter: (a: SnmpTarget, b: SnmpTarget) => String(a.device_name ?? "").localeCompare(String(b.device_name ?? "")),
    render: (r: SnmpTarget) => h(RouterLink, { to: { name: "device-detail", params: { id: r.device_id } } },
      () => r.device_name ?? r.device_id),
  }, (r: SnmpTarget) => r.device_name),
  withExportValue({
    title: t("snmp.mgmt_ip"), key: "mgmt_ip", width: 150,
    render: (r: SnmpTarget) => h("span", { style: "font-family:monospace;white-space:nowrap" },
      r.mgmt_ip ? (r.port === 161 ? r.mgmt_ip : `${r.mgmt_ip}:${r.port}`) : "—"),
  }, (r: SnmpTarget) => r.mgmt_ip),
  withExportValue({
    title: t("snmp.credential"), key: "credential", width: 170, ellipsis: { tooltip: true },
    sorter: (a: SnmpTarget, b: SnmpTarget) => String(a.credential_name ?? "").localeCompare(String(b.credential_name ?? "")),
    render: (r: SnmpTarget) => h(NSpace, { size: 4, wrap: false, align: "center" }, () => [
      h("span", null, r.credential_name ?? "—"),
      r.credential_version
        ? h(NTag, { size: "tiny", bordered: false, type: r.credential_version === "v2c" ? "warning" : "success" },
          () => r.credential_version)
        : null,
    ]),
  }, (r: SnmpTarget) => r.credential_name),
  withExportValue({ title: t("snmp.collect_items"), key: "sections", minWidth: 190, render: sectionsCell },
    (r: SnmpTarget) => SNMP_SECTIONS.filter((s) => r[`collect_${s}`]).map((s) => sectionLabel(s, t)).join(", ")),
  withExportValue({ title: t("snmp.collector"), key: "collector", minWidth: 170, render: collectorCell },
    (r: SnmpTarget) => r.collector_error ? srvText(r.collector_error) : (r.effective_agent_name ?? "")),
  {
    title: t("snmp.last_collect"), key: "last_run_at", width: 165,
    render: (r) => h("span", { style: "white-space:nowrap" }, fmtDateTime(r.last_run_at)),
  },
  withExportValue({
    title: t("cols.last_error"), key: "last_error", minWidth: 180, ellipsis: { tooltip: true },
    render: (r: SnmpTarget) => (r.last_error || r.last_error_code) ? errText(r.last_error_code, r.last_error) : "—",
  }, (r: SnmpTarget) => errText(r.last_error_code, r.last_error)),
  withExportValue({
    title: t("cols.status"), key: "status", width: 110,
    sorter: (a: SnmpTarget, b: SnmpTarget) => targetHealth(a).localeCompare(targetHealth(b)),
    render: (r: SnmpTarget) => h(NTag, { size: "small", bordered: false, type: HEALTH_TAG[targetHealth(r)] },
      () => t(`snmp.health_${targetHealth(r)}`)),
  }, (r: SnmpTarget) => t(`snmp.health_${targetHealth(r)}`)),
  {
    title: t("common.actions"), key: "actions", className: "col-actions", width: 196,
    render: (r) => h(NSpace, { size: 2, wrapItem: false, wrap: false }, () => [
      iconAction(EditIcon, t("common.edit"), () => openEditTarget(r), { type: "success", testid: "snmp-row-edit" }),
      iconAction(TestIcon, t("common.test"), () => openTest(r), { testid: "snmp-row-test" }),
      iconAction(SyncIcon, r.enabled ? t("snmp.collect_now") : t("errors.snmp_target_disabled"), () => collect(r),
        { type: "primary", loading: !!collectBusy.value[r.id], disabled: !!collectBusy.value[r.id] || !r.enabled,
          testid: "snmp-row-collect" }),
      iconAction(AuditIcon, t("snmp.records"), () => openRuns(r), { testid: "snmp-row-runs" }),
      h(NPopconfirm, { onPositiveClick: () => delTarget(r) }, {
        trigger: () => iconAction(DeleteIcon, t("common.delete"), () => {}, { type: "error", testid: "snmp-row-delete" }),
        default: () => t("snmp.target_delete_confirm", { name: r.device_name ?? "" }),
      }),
    ]),
  },
]));
const targetCols = computed<DataTableColumns<SnmpTarget>>(() =>
  tp.orderColumns(allTargetCols.value.filter((c: any) => tp.visibleKeys.value.includes(c.key))));

const CRED_COLS = ["name", "version", "unit", "target_count", "description", "actions"];
const cp = useColumnPrefs("snmp_credentials", CRED_COLS, CRED_COLS);
const credPicker = computed(() => [
  { key: "name", label: t("cols.name") },
  { key: "version", label: t("snmp.version") },
  { key: "unit", label: t("cols.unit") },
  { key: "target_count", label: t("snmp.target_count") },
  { key: "description", label: t("cols.description") },
  { key: "actions", label: t("cols.actions") },
]);
function versionDetail(c: SnmpCredential): string {
  if (c.version === "v2c") return t("snmp.v2c_plaintext");
  return [c.security_level, c.auth_protocol, c.security_level === "authPriv" ? c.priv_protocol : null]
    .filter(Boolean).join(" · ");
}
const allCredCols = computed<DataTableColumns<SnmpCredential>>(() => autoSort([
  {
    title: t("common.name"), key: "name", minWidth: 180, ellipsis: { tooltip: true },
    render: (c) => h(NSpace, { size: 6, wrap: false, align: "center" }, () => [
      h("span", null, c.name),
      c.is_default ? h(NTag, { size: "tiny", type: "info", bordered: false }, () => t("snmp.default_tag")) : null,
    ]),
  },
  withExportValue({
    title: t("snmp.version"), key: "version", minWidth: 220,
    render: (c: SnmpCredential) => h(NSpace, { size: 6, wrap: false, align: "center" }, () => [
      h(NTag, { size: "small", bordered: false, type: c.version === "v2c" ? "warning" : "success" }, () => c.version),
      h("span", { style: "font-size:12px;opacity:.75" }, versionDetail(c)),
    ]),
  }, (c: SnmpCredential) => `${c.version} ${versionDetail(c)}`),
  withExportValue({
    title: t("cols.unit"), key: "unit", width: 160, ellipsis: { tooltip: true },
    sorter: (a: SnmpCredential, b: SnmpCredential) => String(a.customer_name ?? "").localeCompare(String(b.customer_name ?? "")),
    render: (c: SnmpCredential) => c.customer_name ?? t("snmp.cred_all_units"),
  }, (c: SnmpCredential) => c.customer_name ?? ""),
  { title: t("snmp.target_count"), key: "target_count", width: 110 },
  { title: t("common.description"), key: "description", minWidth: 160, ellipsis: { tooltip: true },
    render: (c) => c.description ?? "—" },
  {
    title: t("common.actions"), key: "actions", className: "col-actions", width: 96,
    render: (c) => h(NSpace, { size: 2, wrapItem: false, wrap: false }, () => [
      iconAction(EditIcon, t("common.edit"), () => openEditCred(c), { type: "success", testid: "snmp-cred-edit" }),
      h(NPopconfirm, { onPositiveClick: () => delCred(c) }, {
        trigger: () => iconAction(DeleteIcon, t("common.delete"), () => {}, { type: "error", testid: "snmp-cred-delete" }),
        default: () => c.target_count
          ? t("snmp.cred_delete_in_use_confirm", { name: c.name, n: c.target_count })
          : t("snmp.cred_delete_confirm", { name: c.name }),
      }),
    ]),
  },
]));
const credCols = computed<DataTableColumns<SnmpCredential>>(() =>
  cp.orderColumns(allCredCols.value.filter((c: any) => cp.visibleKeys.value.includes(c.key))));

onMounted(() => {
  void refresh();
  // 帶 ?tab=runs 進來：頁籤的 watch 不會觸發，記錄要在這裡載
  if (tab.value === "runs") void loadRuns();
});
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><SnmpIcon /></n-icon>
        <span>{{ t("snmp.title") }}</span>
      </n-space>
    </template>

    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">{{ t("snmp.intro") }}</n-alert>
    <n-alert v-if="noCapableAgent" type="warning" :bordered="false" style="margin-bottom: 12px"
             data-testid="snmp-no-capable-agent">
      {{ t("snmp.no_capable_agent", { v: minAgentVersion }) }}
      <n-button text type="primary" style="margin-left: 6px" @click="router.push({ name: 'scan_agents' })">
        <template #icon><n-icon><ScanAgentsIcon /></n-icon></template>
        {{ t("nav.scan_agents") }}
      </n-button>
    </n-alert>

    <n-tabs v-model:value="tab" type="line">
      <n-tab-pane name="targets">
        <template #tab>
          <span class="snmp-tab"><n-icon :size="16"><SnmpIcon /></n-icon>{{ t("snmp.tab_targets") }} ({{ targets.length }})</span>
        </template>
        <n-space style="margin-bottom: 12px" align="center">
          <n-button :loading="loading" @click="refresh">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>
            {{ t("common.refresh") }}
          </n-button>
          <n-button type="primary" data-testid="snmp-target-create" @click="openCreateTarget">
            <template #icon><n-icon><PlusIcon /></n-icon></template>
            {{ t("snmp.target_create") }}
          </n-button>
          <n-input v-model:value="targetQ" :placeholder="t('common.filter')" clearable style="width: 180px" />
          <n-tag v-if="agentFilter" closable type="info" :bordered="false" @close="clearAgentFilter">
            {{ t("snmp.agent_filter", { agent: agentFilterName || agentFilter }) }}
          </n-tag>
          <ColumnPicker :all="targetPicker" :visible="tp.visibleKeys.value" @update:visible="tp.setVisible"
                        @reset="tp.reset" :order="tp.order.value" @update:order="tp.setOrder" />
          <ExportButton :columns="targetCols" :rows="targetsFiltered" filename="snmp-targets" :title="t('snmp.tab_targets')" />
        </n-space>
        <n-data-table :columns="targetCols" :data="targetsFiltered" :loading="loading" :bordered="false"
                      :row-key="(r: SnmpTarget) => r.id" :pagination="pg" :scroll-x="1400"
                      data-testid="snmp-targets-table" />
      </n-tab-pane>

      <n-tab-pane name="credentials">
        <template #tab>
          <span class="snmp-tab"><n-icon :size="16"><KeyIcon /></n-icon>{{ t("snmp.tab_credentials") }} ({{ creds.length }})</span>
        </template>
        <n-space style="margin-bottom: 12px" align="center">
          <n-button :loading="loading" @click="refresh">
            <template #icon><n-icon><RefreshIcon /></n-icon></template>
            {{ t("common.refresh") }}
          </n-button>
          <n-button type="primary" data-testid="snmp-cred-create" @click="openCreateCred">
            <template #icon><n-icon><PlusIcon /></n-icon></template>
            {{ t("snmp.cred_create") }}
          </n-button>
          <ColumnPicker :all="credPicker" :visible="cp.visibleKeys.value" @update:visible="cp.setVisible"
                        @reset="cp.reset" :order="cp.order.value" @update:order="cp.setOrder" />
        </n-space>
        <div class="snmp-hint">{{ t("snmp.cred_tab_hint") }}</div>
        <n-data-table :columns="credCols" :data="creds" :loading="loading" :bordered="false"
                      :row-key="(c: SnmpCredential) => c.id" :pagination="credPg" :scroll-x="900"
                      data-testid="snmp-creds-table" />
      </n-tab-pane>

      <n-tab-pane name="runs">
        <template #tab>
          <span class="snmp-tab"><n-icon :size="16"><AuditIcon /></n-icon>{{ t("snmp.tab_runs") }}</span>
        </template>
        <SnmpRunsTable :runs="runs" :loading="runsLoading" :show-device="true" prefs-key="snmp_runs">
          <template #toolbar>
            <n-button :loading="runsLoading" @click="loadRuns">
              <template #icon><n-icon><RefreshIcon /></n-icon></template>
              {{ t("common.refresh") }}
            </n-button>
            <n-select v-model:value="runResult" :options="resultOptions" clearable style="width: 150px"
                      :placeholder="t('cols.result')" />
            <n-select v-model:value="runKind" :options="kindOptions" clearable style="width: 130px"
                      :placeholder="t('snmp.col_kind')" />
          </template>
        </SnmpRunsTable>
      </n-tab-pane>
    </n-tabs>

    <SnmpTargetForm v-model:show="targetFormShow" :target="editingTarget" @saved="refresh" />
    <SnmpCredentialForm v-model:show="credFormShow" :credential="editingCred" @saved="refresh" />
    <SnmpTestResult v-model:show="testShow" :target="testTarget" @finished="refresh" />
    <SnmpRunsModal v-model:show="runsShow" :target="runsTarget" />

    <!-- 使用中的憑證：列出用它的裝置（後端拒絕刪除） -->
    <n-modal v-model:show="inUseShow" preset="card" style="width: 520px; max-width: calc(100vw - 32px)"
             data-testid="snmp-cred-in-use">
      <template #header>
        <n-space align="center" :size="8" :wrap-item="false">
          <n-icon :size="20"><KeyIcon /></n-icon>
          <span>{{ t("snmp.in_use_title", { name: inUseCred?.name ?? "" }) }}</span>
        </n-space>
      </template>
      <n-alert type="warning" :bordered="false" style="margin-bottom: 10px">
        {{ t("errors.snmp_credential_in_use", { count: inUseTargets.length || inUseCred?.target_count || 0 }) }}
      </n-alert>
      <ul class="snmp-in-use">
        <li v-for="tg in inUseTargets" :key="tg.id">
          <router-link :to="{ name: 'device-detail', params: { id: tg.device_id } }" @click="inUseShow = false">
            {{ tg.device_name ?? tg.device_id }}
          </router-link>
        </li>
      </ul>
      <n-space justify="end">
        <n-button @click="inUseShow = false">
          <template #icon><n-icon><CancelIcon /></n-icon></template>
          {{ t("common.close") }}
        </n-button>
      </n-space>
    </n-modal>
  </n-card>
</template>

<style scoped>
.snmp-tab { display: inline-flex; align-items: center; gap: 6px; }
.snmp-hint { font-size: 12px; opacity: .7; margin: -4px 0 10px; }
.snmp-in-use { margin: 0 0 12px; padding-left: 20px; max-height: 300px; overflow: auto; line-height: 1.8; }
</style>
