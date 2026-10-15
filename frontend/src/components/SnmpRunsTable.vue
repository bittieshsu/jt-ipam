<script setup lang="ts">
/**
 * SNMP 收集記錄表（SNMP 頁的「收集記錄」頁籤＝所有目標；單一目標的「記錄」對話框＝這一台）。
 * 展開一列看細節：測試的逐組憑證結果、收集的逐區段計數與錯誤原文。
 */
import { computed, h } from "vue";
import { useI18n } from "vue-i18n";
import { RouterLink } from "vue-router";
import { NDataTable, NTag, NSpace, NTooltip, type DataTableColumns } from "naive-ui";
import { SNMP_SECTIONS, type SnmpRun, type SnmpSection } from "@/api/snmp";
import { autoSort } from "@/composables/useTableSort";
import { useColumnPrefs } from "@/composables/useColumnPrefs";
import { useTablePagination } from "@/composables/useTablePagination";
import ColumnPicker from "@/components/ColumnPicker.vue";
import SnmpTestItems from "@/components/SnmpTestItems.vue";
import { fmtDateTime, fmtDuration } from "@/utils/datetime";
import { errText, runResultType, sectionLabel, sectionTagType, sectionText } from "@/utils/snmp";

const props = defineProps<{ runs: SnmpRun[]; loading?: boolean; showDevice?: boolean; prefsKey: string }>();
const { t } = useI18n();
const pg = useTablePagination();

const ALL = ["started_at", "device", "kind", "trigger", "result", "duration", "sections", "agent", "error"];
const prefs = useColumnPrefs(props.prefsKey, ALL, ALL);
const picker = computed(() => [
  { key: "started_at", label: t("snmp.col_started") },
  ...(props.showDevice ? [{ key: "device", label: t("cols.device") }] : []),
  { key: "kind", label: t("snmp.col_kind") },
  { key: "trigger", label: t("snmp.col_trigger") },
  { key: "result", label: t("cols.result") },
  { key: "duration", label: t("cols.duration") },
  { key: "sections", label: t("snmp.collect_items") },
  { key: "agent", label: t("snmp.col_agent") },
  { key: "error", label: t("cols.error_message") },
]);

function label(prefix: string, v: string): string {
  const key = `snmp.${prefix}_${v}`;
  const out = t(key);
  return out === key ? v : out;
}

function durationText(ms: number | null): string {
  if (ms == null) return "—";
  return ms < 1000 ? `${ms} ms` : fmtDuration(ms / 1000);
}

function sectionTags(r: SnmpRun) {
  const secs = r.sections ?? {};
  const keys = SNMP_SECTIONS.filter((s) => secs[s]);
  if (!keys.length) return h("span", { style: "opacity:.5" }, "—");
  return h(NSpace, { size: 3, wrap: true }, () => keys.map((s: SnmpSection) => h(NTooltip, null, {
    trigger: () => h(NTag, { size: "tiny", bordered: false, type: sectionTagType(secs[s]?.status) },
      () => `${sectionLabel(s, t)} ${typeof secs[s]?.rows === "number" ? secs[s]!.rows : ""}`.trim()),
    default: () => sectionText(secs[s], t),
  })));
}

function expand(r: SnmpRun) {
  const creds = r.info?.credentials;
  if (creds?.length) return h(SnmpTestItems, { items: creds });
  const secs = r.sections ?? {};
  const sys = r.info?.system;
  return h("div", { class: "srt-expand" }, [
    sys?.sys_name || sys?.sys_descr
      ? h("div", { class: "srt-sys" }, [sys.sys_name ? h("strong", null, sys.sys_name) : null,
          sys.sys_descr ? h("span", { class: "srt-dim" }, ` · ${sys.sys_descr}`) : null])
      : null,
    ...SNMP_SECTIONS.filter((s) => secs[s]).map((s) => h("div", { class: "srt-sec" }, [
      h(NTag, { size: "tiny", bordered: false, type: sectionTagType(secs[s]?.status) }, () => sectionLabel(s, t)),
      h("span", null, ` ${sectionText(secs[s], t)}`),
      typeof secs[s]?.new === "number" || typeof secs[s]?.removed === "number"
        ? h("span", { class: "srt-dim" }, ` · ${t("snmp.run_new_removed", { n: secs[s]?.new ?? 0, r: secs[s]?.removed ?? 0 })}`)
        : null,
    ])),
    r.error ? h("div", { class: "srt-err" }, errText(r.error_code, r.error)) : null,
  ]);
}

const allCols = computed<DataTableColumns<SnmpRun>>(() => autoSort([
  { type: "expand", expandable: (r: SnmpRun) => !!(r.sections || r.info || r.error), renderExpand: expand },
  { title: t("snmp.col_started"), key: "started_at", width: 165, render: (r) => fmtDateTime(r.started_at) },
  ...(props.showDevice ? [{
    title: t("cols.device"), key: "device", minWidth: 150, ellipsis: { tooltip: true },
    sorter: (a: SnmpRun, b: SnmpRun) => String(a.device_name ?? "").localeCompare(String(b.device_name ?? "")),
    render: (r: SnmpRun) => r.device_id
      ? h(RouterLink, { to: { name: "device-detail", params: { id: r.device_id } } }, () => r.device_name ?? r.device_id)
      : (r.device_name ?? "—"),
  }] : []),
  {
    title: t("snmp.col_kind"), key: "kind", width: 90,
    render: (r) => h(NTag, { size: "small", bordered: false, type: r.kind === "test" ? "info" : "default" },
      () => label("kind", r.kind)),
  },
  { title: t("snmp.col_trigger"), key: "trigger", width: 90, render: (r) => label("trigger", r.trigger) },
  {
    title: t("cols.result"), key: "result", width: 100,
    render: (r) => h(NTag, { size: "small", bordered: false, type: runResultType(r.result) },
      () => label("result", r.result)),
  },
  { title: t("cols.duration"), key: "duration", width: 90,
    sorter: (a, b) => (a.duration_ms ?? -1) - (b.duration_ms ?? -1), render: (r) => durationText(r.duration_ms) },
  { title: t("snmp.collect_items"), key: "sections", minWidth: 200, render: sectionTags },
  { title: t("snmp.col_agent"), key: "agent", width: 140, ellipsis: { tooltip: true },
    render: (r) => r.agent_name ?? "—" },
  { title: t("cols.error_message"), key: "error", minWidth: 200, ellipsis: { tooltip: true },
    render: (r) => (r.error || r.error_code) ? errText(r.error_code, r.error) : "—" },
]));
const cols = computed<DataTableColumns<SnmpRun>>(() => prefs.orderColumns(allCols.value.filter(
  (c: any) => c.type === "expand" || prefs.visibleKeys.value.includes(c.key))));
</script>

<template>
  <div>
    <n-space style="margin-bottom: 8px" align="center">
      <slot name="toolbar" />
      <ColumnPicker :all="picker" :visible="prefs.visibleKeys.value" @update:visible="prefs.setVisible"
                    @reset="prefs.reset" :order="prefs.order.value" @update:order="prefs.setOrder" />
    </n-space>
    <n-data-table :columns="cols" :data="runs" :loading="loading" :bordered="false" size="small"
                  :row-key="(r: SnmpRun) => r.id" :pagination="pg" :scroll-x="showDevice ? 1250 : 1100"
                  data-testid="snmp-runs-table" />
  </div>
</template>

<style scoped>
:deep(.srt-expand) { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
:deep(.srt-dim) { opacity: .65; }
:deep(.srt-err) { color: var(--n-error-color, #d03050); word-break: break-word; }
</style>
