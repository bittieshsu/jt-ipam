<script setup lang="ts">
/**
 * 系統診斷的「容量用量」（使用者 2026-10-14：「各個功能吃掉多少容量、資料庫吃多少」「最好還要多個分布圖，一目了然」）。
 * 資料庫依功能分類畫成環圈圖，主機上 jt-ipam 的目錄畫成長條，再列出最大的幾張資料表。
 * 不用圖表套件：SVG 自己畫（與儀表板的使用率環圈一樣），跟著主題色。
 */
import { computed, ref } from "vue";
import { useI18n } from "vue-i18n";
import { NTooltip } from "naive-ui";
import { fmtBytes } from "@/utils/bytes";
import { fmtDateTime } from "@/utils/datetime";

export interface StorageGroup { key: string; bytes: number; tables: number }
export interface StorageTable { name: string; group: string; bytes: number; rows: number }
export interface StorageDir {
  key: string; path: string; exists: boolean; bytes: number | null; approx: boolean; denied?: boolean;
  from_status?: string | null;
  children: { key: string; exists: boolean; bytes: number | null; approx: boolean }[];
}
export interface StorageUsage {
  generated_at: string;
  database: { bytes: number; groups: StorageGroup[]; tables: StorageTable[] };
  directories: StorageDir[];
  disk: { path: string; total: number; used: number; free: number } | null;
  directories_measured_at?: string;
}

const props = defineProps<{ data: StorageUsage; loading?: boolean }>();
const { t } = useI18n();
const nf = new Intl.NumberFormat();

// 每個功能分類固定一個顏色：重新整理、換頁回來都一樣，圖例與長條對得起來
const COLORS: Record<string, string> = {
  ipam: "#18a058", history: "#2080f0", audit: "#8b5cf6", notifications: "#f0a020", dns_dhcp: "#0ea5e9",
  firewall: "#d03050", network_data: "#14b8a6", physical: "#b45309", integrations: "#6366f1", reference: "#64748b",
  ai: "#ec4899", change_impact: "#84cc16", system: "#94a3b8", other: "#a8a29e",
};
const color = (k: string) => COLORS[k] ?? "#a8a29e";

const db = computed(() => props.data.database);
const groupTotal = computed(() => db.value.groups.reduce((s, g) => s + g.bytes, 0) || 1);
const pct = (b: number, total: number) => (total > 0 ? (b / total) * 100 : 0);
const pctText = (p: number) => (p >= 10 ? p.toFixed(0) : p >= 1 ? p.toFixed(1) : p > 0 ? "<1" : "0") + "%";

// 環圈：每段是一個 stroke-dasharray 圓弧；圓周 2πr
const R = 54;
const CIRC = 2 * Math.PI * R;
const hover = ref<string | null>(null);
const arcs = computed(() => {
  let offset = 0;
  return db.value.groups.filter((g) => g.bytes > 0).map((g) => {
    const len = (g.bytes / groupTotal.value) * CIRC;
    const arc = { key: g.key, len, offset, bytes: g.bytes };
    offset += len;
    return arc;
  });
});
const center = computed(() => {
  const g = hover.value ? db.value.groups.find((x) => x.key === hover.value) : null;
  return g ? { label: t(`doctor.storage_group.${g.key}`), value: fmtBytes(g.bytes), sub: pctText(pct(g.bytes, groupTotal.value)) }
    : { label: t("doctor.storage_db"), value: fmtBytes(db.value.bytes), sub: "" };
});

// 目錄長條：以最大的那個目錄為滿格
const dirs = computed(() => props.data.directories.filter((d) => d.exists));
const missingDirs = computed(() => props.data.directories.filter((d) => !d.exists));
const dirMax = computed(() => Math.max(1, ...dirs.value.map((d) => d.bytes ?? 0)));
const approx = (a: boolean) => (a ? `${t("doctor.storage_approx")} ` : "");

const tableMax = computed(() => Math.max(1, ...db.value.tables.map((x) => x.bytes)));
const disk = computed(() => props.data.disk);
const diskPct = computed(() => (disk.value ? pct(disk.value.used, disk.value.total) : 0));
const backups = computed(() => props.data.directories.find((d) => d.key === "backups"));
const install = computed(() => props.data.directories.find((d) => d.key === "install"));
</script>

<template>
  <section class="su" data-testid="doctor-storage">
    <div class="su-head">
      <span class="su-title">{{ t("doctor.storage_title") }}</span>
      <span class="su-hint">{{ t("doctor.storage_hint") }}</span>
      <span v-if="loading" class="su-hint">{{ t("doctor.storage_measuring") }}</span>
    </div>

    <!-- 摘要：資料庫、備份、程式與套件、磁碟 -->
    <div class="su-cards">
      <div class="su-card" data-testid="storage-card-db">
        <div class="su-card-k">{{ t("doctor.storage_db") }}</div>
        <div class="su-card-v">{{ fmtBytes(db.bytes) }}</div>
      </div>
      <div v-if="backups?.exists" class="su-card">
        <div class="su-card-k">{{ t("doctor.storage_dir.backups") }}</div>
        <div class="su-card-v">{{ approx(backups.approx) }}{{ fmtBytes(backups.bytes) }}</div>
      </div>
      <div v-if="install?.exists" class="su-card">
        <div class="su-card-k">{{ t("doctor.storage_dir.install") }}</div>
        <div class="su-card-v">{{ approx(install.approx) }}{{ fmtBytes(install.bytes) }}</div>
      </div>
      <div v-if="disk" class="su-card su-card-disk" data-testid="storage-card-disk">
        <div class="su-card-k">{{ t("doctor.storage_disk") }}</div>
        <div class="su-card-v">{{ t("doctor.storage_disk_free", { free: fmtBytes(disk.free), total: fmtBytes(disk.total) }) }}</div>
        <div class="su-meter"><div class="su-meter-fill" :style="{ width: `${diskPct}%` }"
                                    :data-level="diskPct >= 90 ? 'bad' : diskPct >= 75 ? 'warn' : 'ok'" /></div>
      </div>
    </div>

    <div class="su-panels">
      <!-- 資料庫依功能：環圈＋圖例 -->
      <div class="su-panel" data-testid="storage-db-groups">
        <div class="su-panel-title">{{ t("doctor.storage_db_by_group") }}</div>
        <div class="su-donut-wrap">
          <svg class="su-donut" viewBox="0 0 140 140" role="img" :aria-label="t('doctor.storage_db_by_group')">
            <circle cx="70" cy="70" :r="R" fill="none" class="su-donut-track" stroke-width="20" />
            <circle v-for="a in arcs" :key="a.key" cx="70" cy="70" :r="R" fill="none" :stroke="color(a.key)"
                    :stroke-width="hover === a.key ? 24 : 20" :stroke-dasharray="`${a.len} ${CIRC - a.len}`"
                    :stroke-dashoffset="-a.offset" transform="rotate(-90 70 70)" class="su-arc" :data-group="a.key"
                    @mouseenter="hover = a.key" @mouseleave="hover = null">
              <title>{{ t(`doctor.storage_group.${a.key}`) }}：{{ fmtBytes(a.bytes) }}</title>
            </circle>
            <text x="70" y="62" text-anchor="middle" class="su-donut-label">{{ center.label }}</text>
            <text x="70" y="80" text-anchor="middle" class="su-donut-value">{{ center.value }}</text>
            <text v-if="center.sub" x="70" y="95" text-anchor="middle" class="su-donut-label">{{ center.sub }}</text>
          </svg>
          <ul class="su-legend">
            <li v-for="g in db.groups" :key="g.key" :class="{ dim: hover && hover !== g.key }" :data-group="g.key"
                @mouseenter="hover = g.key" @mouseleave="hover = null">
              <span class="su-dot" :style="{ background: color(g.key) }" />
              <span class="su-legend-k">{{ t(`doctor.storage_group.${g.key}`) }}</span>
              <span class="su-legend-v">{{ fmtBytes(g.bytes) }}</span>
              <span class="su-legend-p">{{ pctText(pct(g.bytes, groupTotal)) }}</span>
            </li>
          </ul>
        </div>
      </div>

      <!-- 主機上的目錄 -->
      <div class="su-panel" data-testid="storage-dirs">
        <div class="su-panel-title">{{ t("doctor.storage_dirs") }}</div>
        <div v-for="d in dirs" :key="d.key" class="su-dir" :data-dir="d.key">
          <div class="su-row">
            <n-tooltip>
              <template #trigger><span class="su-row-k">{{ t(`doctor.storage_dir.${d.key}`) }}</span></template>
              {{ d.path }}
            </n-tooltip>
            <span class="su-row-v">{{ approx(d.approx) }}{{ fmtBytes(d.bytes) }}</span>
          </div>
          <div class="su-bar"><div class="su-bar-fill" :style="{ width: `${pct(d.bytes ?? 0, dirMax)}%` }" /></div>
          <div v-if="d.denied" class="su-note">{{ t("doctor.storage_denied") }}</div>
          <div v-if="d.from_status" class="su-note">{{ t("doctor.storage_from_backup", { at: fmtDateTime(d.from_status) }) }}</div>
          <div v-for="c in d.children.filter((x) => x.exists)" :key="c.key" class="su-child" :data-child="c.key">
            <div class="su-row">
              <span class="su-row-k">{{ t(`doctor.storage_child.${c.key}`) }}</span>
              <span class="su-row-v">{{ approx(c.approx) }}{{ fmtBytes(c.bytes) }}</span>
            </div>
            <div class="su-bar su-bar-thin"><div class="su-bar-fill" :style="{ width: `${pct(c.bytes ?? 0, dirMax)}%` }" /></div>
          </div>
        </div>
        <div v-if="missingDirs.length" class="su-note">
          {{ t("doctor.storage_missing", { dirs: missingDirs.map((d) => t(`doctor.storage_dir.${d.key}`)).join("、") }) }}
        </div>
        <div v-if="data.directories_measured_at" class="su-note">
          {{ t("doctor.storage_measured", { at: fmtDateTime(data.directories_measured_at) }) }}
        </div>
      </div>
    </div>

    <!-- 最大的資料表 -->
    <div class="su-panel su-tables" data-testid="storage-tables">
      <div class="su-panel-title">{{ t("doctor.storage_top_tables") }}</div>
      <div v-for="x in db.tables" :key="x.name" class="su-trow">
        <span class="su-dot" :style="{ background: color(x.group) }" />
        <span class="su-tname" :title="t(`doctor.storage_group.${x.group}`)">{{ x.name }}</span>
        <span class="su-tgroup">{{ t(`doctor.storage_group.${x.group}`) }}</span>
        <span class="su-trows">{{ t("doctor.storage_rows", { n: nf.format(x.rows) }) }}</span>
        <div class="su-bar su-tbar"><div class="su-bar-fill" :style="{ width: `${pct(x.bytes, tableMax)}%`, background: color(x.group) }" /></div>
        <span class="su-row-v">{{ fmtBytes(x.bytes) }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.su { margin-top: 22px; }
.su-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.su-title { font-weight: 600; font-size: 15px; }
.su-hint { font-size: 12px; opacity: .65; }

.su-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 12px; }
.su-card { border: 1px solid var(--n-border-color, rgba(128, 128, 128, .2)); border-radius: 10px; padding: 10px 14px;
           background: rgba(128, 128, 128, .04); }
.su-card-k { font-size: 12px; opacity: .7; }
.su-card-v { font-size: 20px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }
.su-card-disk .su-card-v { font-size: 15px; margin-top: 4px; }
.su-meter { height: 6px; border-radius: 3px; background: rgba(128, 128, 128, .18); margin-top: 8px; overflow: hidden; }
.su-meter-fill { height: 100%; border-radius: 3px; background: #18a058; }
.su-meter-fill[data-level="warn"] { background: #f0a020; }
.su-meter-fill[data-level="bad"] { background: #d03050; }

.su-panels { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); gap: 12px; }
@media (max-width: 900px) { .su-panels { grid-template-columns: minmax(0, 1fr); } }
.su-panel { border: 1px solid var(--n-border-color, rgba(128, 128, 128, .2)); border-radius: 10px; padding: 12px 14px; min-width: 0; }
.su-panel-title { font-size: 13px; font-weight: 600; margin-bottom: 10px; }

.su-donut-wrap { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; }
.su-donut { width: 170px; height: 170px; flex: 0 0 auto; }
.su-donut-track { stroke: rgba(128, 128, 128, .15); }
.su-arc { transition: stroke-width .15s; cursor: pointer; }
.su-donut-label { font-size: 9px; fill: currentColor; opacity: .7; }
.su-donut-value { font-size: 15px; font-weight: 700; fill: currentColor; }
.su-legend { list-style: none; margin: 0; padding: 0; flex: 1 1 220px; min-width: 0; }
.su-legend li { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto 44px; align-items: center; gap: 8px;
                font-size: 13px; padding: 3px 0; cursor: default; transition: opacity .15s; }
.su-legend li.dim { opacity: .35; }
.su-legend-k { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.su-legend-v, .su-row-v { font-variant-numeric: tabular-nums; font-weight: 600; white-space: nowrap; }
.su-legend-p { font-variant-numeric: tabular-nums; opacity: .65; text-align: right; font-size: 12px; }
.su-dot { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }

.su-dir { margin-bottom: 12px; }
.su-row { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; }
.su-row-k { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.su-bar { height: 8px; border-radius: 4px; background: rgba(128, 128, 128, .15); margin: 4px 0 2px; overflow: hidden; }
.su-bar-thin { height: 5px; }
.su-bar-fill { height: 100%; border-radius: 4px; background: #2080f0; min-width: 2px; }
.su-child { margin-left: 14px; margin-top: 4px; font-size: 12px; opacity: .85; }
.su-child .su-row { font-size: 12px; }
.su-child .su-bar-fill { background: #60a5fa; }
.su-note { font-size: 12px; opacity: .6; margin-top: 4px; }

.su-tables { margin-top: 12px; }
.su-trow { display: grid; grid-template-columns: 12px minmax(140px, 1.2fr) minmax(0, 1fr) 110px minmax(60px, 1fr) 70px;
           align-items: center; gap: 10px; font-size: 13px; padding: 4px 0;
           border-bottom: 1px solid var(--n-border-color, rgba(128, 128, 128, .12)); }
.su-trow:last-child { border-bottom: 0; }
.su-tname { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.su-tgroup { font-size: 12px; opacity: .7; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.su-trows { font-size: 12px; opacity: .65; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.su-tbar { margin: 0; }
.su-trow .su-row-v { text-align: right; }
@media (max-width: 700px) {
  .su-trow { grid-template-columns: 12px minmax(0, 1fr) 70px; }
  .su-tgroup, .su-trows, .su-tbar { display: none; }
}
</style>
