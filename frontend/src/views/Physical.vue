<script setup lang="ts">
import { computed, h, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NCard, NDataTable, NSpace, NIcon, NButton, NTag, NModal, NForm, NFormItem,
  NSelect, NInput, NInputNumber,
  useMessage, type DataTableColumns,
} from "naive-ui";
import { PhysicalIcon, PowerIcon, VpnIcon, RefreshIcon, PlusIcon } from "@/icons";
import { Physical, type DevicePort } from "@/api/phase3";
import { listDevices, listLocations } from "@/api/basic";
import { autoSort } from "@/composables/useTableSort";

// 三個獨立頁面共用此元件，以 mode 決定顯示哪一段（佈線 / 電力 / VPN）
const props = defineProps<{ mode?: "cabling" | "power" | "vpn" }>();
const mode = computed(() => props.mode ?? "cabling");

const { t } = useI18n();
const msg = useMessage();

const cables = ref<any[]>([]);
const panels = ref<any[]>([]);
const feeds = ref<any[]>([]);
const outlets = ref<any[]>([]);
const vpns = ref<any[]>([]);
const loading = ref(false);

// 共用下拉資料
const devices = ref<{ id: string; name: string }[]>([]);
const locations = ref<{ id: string; name: string }[]>([]);
const deviceOpts = computed(() => devices.value.map((d) => ({ label: d.name, value: d.id })));
const locationOpts = computed(() => locations.value.map((l) => ({ label: l.name, value: l.id })));

async function refresh() {
  loading.value = true;
  try {
    if (mode.value === "cabling") {
      cables.value = await Physical.cables();
    } else if (mode.value === "power") {
      [panels.value, feeds.value, outlets.value] = await Promise.all([
        Physical.panels(), Physical.feeds(), Physical.outlets(),
      ]);
    } else {
      vpns.value = await Physical.vpns();
    }
  } catch { msg.error(t("errors.network")); }
  finally { loading.value = false; }
}

async function ensureDevices() {
  if (!devices.value.length) {
    try { devices.value = (await listDevices({ pageSize: 500 })).items.map((d: any) => ({ id: d.id, name: d.name })); } catch { /* */ }
  }
}
async function ensureLocations() {
  if (!locations.value.length) {
    try { locations.value = (await listLocations()).items.map((l: any) => ({ id: l.id, name: l.name })); } catch { /* */ }
  }
}

const pageTitle = computed(() =>
  mode.value === "power" ? t("nav.power")
    : mode.value === "vpn" ? t("nav.vpn_tunnels")
    : t("nav.cabling"));
const PageIcon = computed(() => (mode.value === "power" ? PowerIcon : mode.value === "vpn" ? VpnIcon : PhysicalIcon));

function statusTag(s: string) {
  const type = s === "connected" ? "success" : s === "planned" ? "info" : "default";
  return h(NTag, { size: "small", type, bordered: false }, () => s);
}

const cableCols = computed<DataTableColumns<any>>(() => autoSort([
  { title: t("cols.type"), key: "type", width: 110, render: (r: any) => r.type ?? "—" },
  { title: t("physical.a_end"), key: "a_end", minWidth: 200, ellipsis: { tooltip: true }, render: (r: any) => r.a_end ?? "—" },
  { title: t("physical.b_end"), key: "b_end", minWidth: 200, ellipsis: { tooltip: true }, render: (r: any) => r.b_end ?? "—" },
  { title: t("common.status"), key: "status", width: 110, render: (r: any) => statusTag(r.status) },
  { title: t("sections.description"), key: "description", minWidth: 160, ellipsis: { tooltip: true }, render: (r: any) => r.label || r.description || "—" },
]));
const panelCols = computed<DataTableColumns<any>>(() => autoSort([
  { title: t("common.name"), key: "name" },
  { title: t("nav.locations"), key: "location_id", render: (r: any) => locations.value.find((l) => l.id === r.location_id)?.name ?? "—" },
]));
const feedCols = computed<DataTableColumns<any>>(() => autoSort([
  { title: t("common.name"), key: "name" },
  { title: t("cols.panel"), key: "panel_id", render: (r: any) => panels.value.find((p) => p.id === r.panel_id)?.name ?? r.panel_id },
]));
const outletCols = computed<DataTableColumns<any>>(() => autoSort([
  { title: t("common.name"), key: "name" },
  { title: t("cols.feed"), key: "feed_id", render: (r: any) => feeds.value.find((f) => f.id === r.feed_id)?.name ?? "—" },
]));
const vpnCols = computed<DataTableColumns<any>>(() => autoSort([
  { title: t("common.name"), key: "name" },
  { title: t("cols.type"), key: "type" },
  { title: t("common.status"), key: "status" },
  {
    title: t("physical.vpn_peer"), key: "peer", minWidth: 260,
    render: (r) => {
      if (r.peered && r.a_device_name && r.b_device_name) {
        const reliable = r.pairing_method === "wireguard_pubkey";
        const badge = h(NTag, {
          size: "tiny", type: reliable ? "success" : "warning", bordered: false,
          style: "margin-left:8px",
        }, () => reliable ? t("physical.pair_reliable") : t("physical.pair_besteffort"));
        return h("span", { class: "vpn-peered" }, [
          `${r.a_device_name} ⇄ ${r.b_device_name}`, badge,
        ]);
      }
      if (r.b_endpoint) return h("span", { style: "opacity:.7" }, `→ ${r.b_endpoint}`);
      return h("span", { style: "opacity:.4" }, "—");
    },
  },
]));

// ── 新增纜線 ──
const cableTypeOpts = ["cat5e", "cat6", "cat6a", "dac", "fiber-mm", "fiber-sm", "power"].map((v) => ({ label: v, value: v }));
const showCable = ref(false);
const savingCable = ref(false);
const cableForm = ref<{ aDevice: string | null; aPort: string | null; bDevice: string | null; bPort: string | null; type: string | null; label: string }>(
  { aDevice: null, aPort: null, bDevice: null, bPort: null, type: "cat6", label: "" });
const aPorts = ref<DevicePort[]>([]);
const bPorts = ref<DevicePort[]>([]);
const portOpts = (ps: DevicePort[]) => ps.map((p) => ({ label: `${p.name}${p.type ? ` (${p.type})` : ""}`, value: p.id }));
watch(() => cableForm.value.aDevice, async (d) => { cableForm.value.aPort = null; aPorts.value = d ? await Physical.ports(d) : []; });
watch(() => cableForm.value.bDevice, async (d) => { cableForm.value.bPort = null; bPorts.value = d ? await Physical.ports(d) : []; });
async function openCable() { await ensureDevices(); cableForm.value = { aDevice: null, aPort: null, bDevice: null, bPort: null, type: "cat6", label: "" }; aPorts.value = []; bPorts.value = []; showCable.value = true; }
async function saveCable() {
  const f = cableForm.value;
  if (!f.aPort || !f.bPort) { msg.warning(t("physical.pick_both_ports")); return; }
  if (f.aPort === f.bPort) { msg.warning(t("physical.same_port")); return; }
  savingCable.value = true;
  try {
    await Physical.connectPorts(f.aPort, f.bPort, { type: f.type ?? undefined, label: f.label.trim() || undefined });
    msg.success(t("common.ok")); showCable.value = false; await refresh();
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.network")); } finally { savingCable.value = false; }
}

// ── 新增電力（配電盤 / 饋線 / 插座）──
const showPower = ref(false);
const powerKind = ref<"panel" | "feed" | "outlet">("panel");
const savingPower = ref(false);
const powerForm = ref<{ name: string; location_id: string | null; panel_id: string | null; feed_id: string | null }>(
  { name: "", location_id: null, panel_id: null, feed_id: null });
const panelOpts = computed(() => panels.value.map((p) => ({ label: p.name, value: p.id })));
const feedOpts = computed(() => feeds.value.map((f) => ({ label: f.name, value: f.id })));
const powerTitle = computed(() => powerKind.value === "panel" ? t("physical.panels") : powerKind.value === "feed" ? t("physical.feeds") : t("physical.outlets"));
async function openPower(kind: "panel" | "feed" | "outlet") {
  powerKind.value = kind;
  powerForm.value = { name: "", location_id: null, panel_id: panels.value[0]?.id ?? null, feed_id: feeds.value[0]?.id ?? null };
  if (kind === "panel") await ensureLocations();
  showPower.value = true;
}
async function savePower() {
  const f = powerForm.value;
  if (!f.name.trim()) { msg.warning(t("physical.need_name")); return; }
  if (powerKind.value === "feed" && !f.panel_id) { msg.warning(t("physical.need_panel")); return; }
  savingPower.value = true;
  try {
    if (powerKind.value === "panel") await Physical.createPanel({ name: f.name.trim(), location_id: f.location_id });
    else if (powerKind.value === "feed") await Physical.createFeed({ panel_id: f.panel_id!, name: f.name.trim() });
    else await Physical.createOutlet({ feed_id: f.feed_id, name: f.name.trim() });
    msg.success(t("common.ok")); showPower.value = false; await refresh();
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.network")); } finally { savingPower.value = false; }
}

onMounted(() => { void refresh(); });
watch(mode, () => { void refresh(); });
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><component :is="PageIcon" /></n-icon>
        <span>{{ pageTitle }}</span>
      </n-space>
    </template>
    <n-space style="margin-bottom: 12px">
      <n-button @click="refresh" :loading="loading">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
      <n-button v-if="mode === 'cabling'" type="primary" @click="openCable">
        <template #icon><n-icon><PlusIcon /></n-icon></template>
        {{ t("physical.add_cable") }}
      </n-button>
    </n-space>

    <n-data-table v-if="mode === 'cabling'"
      :columns="cableCols" :data="cables" :loading="loading" :bordered="false" :scroll-x="900" />

    <template v-else-if="mode === 'power'">
      <n-space align="center" style="margin-bottom:6px">
        <h3 style="margin:0">{{ t("physical.panels") }} ({{ panels.length }})</h3>
        <n-button size="tiny" @click="openPower('panel')">
          <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("common.add") }}
        </n-button>
      </n-space>
      <n-data-table :columns="panelCols" :data="panels" :loading="loading" :bordered="false" />
      <n-space align="center" style="margin:16px 0 6px">
        <h3 style="margin:0">{{ t("physical.feeds") }} ({{ feeds.length }})</h3>
        <n-button size="tiny" :disabled="!panels.length" @click="openPower('feed')">
          <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("common.add") }}
        </n-button>
      </n-space>
      <n-data-table :columns="feedCols" :data="feeds" :loading="loading" :bordered="false" />
      <n-space align="center" style="margin:16px 0 6px">
        <h3 style="margin:0">{{ t("physical.outlets") }} ({{ outlets.length }})</h3>
        <n-button size="tiny" @click="openPower('outlet')">
          <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("common.add") }}
        </n-button>
      </n-space>
      <n-data-table :columns="outletCols" :data="outlets" :loading="loading" :bordered="false" />
    </template>

    <n-data-table v-else
      :columns="vpnCols" :data="vpns" :loading="loading" :bordered="false" />

    <!-- 新增纜線 -->
    <n-modal v-model:show="showCable" preset="card" :title="t('physical.add_cable')" style="max-width:520px">
      <n-form label-placement="top">
        <n-form-item :label="t('physical.a_device')">
          <n-select v-model:value="cableForm.aDevice" :options="deviceOpts" filterable clearable :placeholder="t('physical.pick_device')" />
        </n-form-item>
        <n-form-item :label="t('physical.a_port')">
          <n-select v-model:value="cableForm.aPort" :options="portOpts(aPorts)" :disabled="!cableForm.aDevice" filterable clearable :placeholder="t('physical.pick_port')" />
        </n-form-item>
        <n-form-item :label="t('physical.b_device')">
          <n-select v-model:value="cableForm.bDevice" :options="deviceOpts" filterable clearable :placeholder="t('physical.pick_device')" />
        </n-form-item>
        <n-form-item :label="t('physical.b_port')">
          <n-select v-model:value="cableForm.bPort" :options="portOpts(bPorts)" :disabled="!cableForm.bDevice" filterable clearable :placeholder="t('physical.pick_port')" />
        </n-form-item>
        <n-space>
          <n-form-item :label="t('cols.type')" style="flex:1">
            <n-select v-model:value="cableForm.type" :options="cableTypeOpts" />
          </n-form-item>
          <n-form-item :label="t('physical.cable_label')" style="flex:1">
            <n-input v-model:value="cableForm.label" placeholder="(optional)" />
          </n-form-item>
        </n-space>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showCable = false">{{ t("common.cancel") }}</n-button>
          <n-button type="primary" :loading="savingCable" @click="saveCable">{{ t("common.save") }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 新增電力 -->
    <n-modal v-model:show="showPower" preset="card" :title="`${t('common.add')} — ${powerTitle}`" style="max-width:460px">
      <n-form label-placement="top">
        <n-form-item :label="t('common.name')">
          <n-input v-model:value="powerForm.name" />
        </n-form-item>
        <n-form-item v-if="powerKind === 'panel'" :label="t('nav.locations')">
          <n-select v-model:value="powerForm.location_id" :options="locationOpts" filterable clearable />
        </n-form-item>
        <n-form-item v-if="powerKind === 'feed'" :label="t('cols.panel')">
          <n-select v-model:value="powerForm.panel_id" :options="panelOpts" filterable />
        </n-form-item>
        <n-form-item v-if="powerKind === 'outlet'" :label="t('cols.feed')">
          <n-select v-model:value="powerForm.feed_id" :options="feedOpts" filterable clearable />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showPower = false">{{ t("common.cancel") }}</n-button>
          <n-button type="primary" :loading="savingPower" @click="savePower">{{ t("common.save") }}</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-card>
</template>

<style scoped>
.vpn-peered {
  color: #18a058;
  font-weight: 600;
}
</style>
