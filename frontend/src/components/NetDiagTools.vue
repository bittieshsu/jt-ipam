<template>
  <div class="nd">
    <n-divider style="margin: 22px 0 14px">
      <span class="nd-div">{{ t("netdiag.section") }}</span>
    </n-divider>
    <div class="nd-note">{{ t("netdiag.section_note") }}</div>

    <div class="nd-grid">
      <!-- Ping -->
      <div class="nd-wide">
        <n-card size="small">
          <template #header>
            <CardTitle :icon="LiveIcon" :text="t('netdiag.ping')">
              <n-tag v-if="caps && !caps.ping" size="small" type="warning" :bordered="false">
                {{ t("netdiag.unavailable") }}
              </n-tag>
            </CardTitle>
          </template>
          <n-input v-model:value="ping.targets" type="textarea" :rows="3"
                   :placeholder="t('netdiag.targets_ph')" />
          <div class="nd-hint">{{ t("netdiag.targets_hint", { n: MAX_TARGETS }) }}</div>
          <n-space align="center" :size="14" style="margin-top:10px" :wrap="true">
            <span class="nd-lbl">{{ t("netdiag.count") }}
              <n-input-number v-model:value="ping.count" :min="1" :max="10" size="small" style="width:92px" />
            </span>
            <span class="nd-lbl">{{ t("netdiag.timeout") }}
              <n-input-number v-model:value="ping.timeout" :min="1" :max="10" size="small" style="width:92px" />
            </span>
            <span class="nd-lbl">{{ t("netdiag.concurrency") }}
              <n-input-number v-model:value="ping.concurrency" :min="1" :max="32" size="small" style="width:92px" />
            </span>
            <n-button type="primary" :loading="ping.busy" :disabled="caps ? !caps.ping : false"
                      @click="runPing">
              <template #icon><n-icon><SearchIcon /></n-icon></template>{{ t("netdiag.run") }}
            </n-button>
          </n-space>
          <div v-if="ping.rows.length" class="nd-sum">{{ pingSummary }}</div>
          <n-data-table v-if="ping.rows.length" size="small" :columns="pingCols" :data="ping.rows"
                        :bordered="true" style="margin-top:8px" />
        </n-card>
      </div>

      <!-- 路徑追蹤 -->
      <div class="nd-wide">
        <n-card size="small">
          <template #header>
            <CardTitle :icon="TopologyIcon" :text="t('netdiag.trace')">
              <n-tag v-if="caps && !caps.tracepath && !caps.traceroute" size="small" type="warning" :bordered="false">
                {{ t("netdiag.unavailable") }}
              </n-tag>
            </CardTitle>
          </template>
          <n-space :size="10" align="center" :wrap="true">
            <n-input v-model:value="trace.target" style="width:280px"
                     placeholder="8.8.8.8" @keyup.enter="runTrace" />
            <span class="nd-lbl">{{ t("netdiag.max_hops") }}
              <n-input-number v-model:value="trace.maxHops" :min="1" :max="30" size="small" style="width:92px" />
            </span>
            <n-button type="primary" :loading="trace.busy"
                      :disabled="caps ? !(caps.tracepath || caps.traceroute) : false" @click="runTrace">
              <template #icon><n-icon><SearchIcon /></n-icon></template>{{ t("netdiag.run") }}
            </n-button>
          </n-space>
          <div class="nd-hint">{{ t("netdiag.trace_hint") }}</div>
          <div v-if="trace.res" class="nd-sum">
            {{ t("netdiag.trace_tool", { tool: trace.res.tool }) }}
            <template v-if="trace.res.path_mtu"> · {{ t("netdiag.path_mtu", { n: trace.res.path_mtu }) }}</template>
            <n-tag v-if="trace.res.truncated" size="small" type="warning" :bordered="false"
                   style="margin-left:8px">{{ t("netdiag.truncated") }}</n-tag>
          </div>
          <n-data-table v-if="trace.res?.hops.length" size="small" :columns="hopCols"
                        :data="trace.res.hops" :bordered="true" style="margin-top:8px" />
        </n-card>
      </div>

      <!-- TCP 埠 -->
      <div>
        <n-card size="small">
          <template #header><CardTitle :icon="LinkIcon" :text="t('netdiag.tcp')" /></template>
          <n-input v-model:value="tcp.targets" type="textarea" :rows="2"
                   :placeholder="t('netdiag.targets_ph')" />
          <n-space align="center" :size="14" style="margin-top:10px" :wrap="true">
            <span class="nd-lbl">{{ t("netdiag.ports") }}
              <n-input v-model:value="tcp.ports" size="small" style="width:180px" placeholder="443, 22, 3389" />
            </span>
            <span class="nd-lbl">{{ t("netdiag.timeout") }}
              <n-input-number v-model:value="tcp.timeout" :min="1" :max="10" size="small" style="width:92px" />
            </span>
            <n-button type="primary" :loading="tcp.busy" @click="runTcp">
              <template #icon><n-icon><SearchIcon /></n-icon></template>{{ t("netdiag.run") }}
            </n-button>
          </n-space>
          <div class="nd-hint">{{ t("netdiag.tcp_hint") }}</div>
          <n-data-table v-if="tcp.rows.length" size="small" :columns="tcpCols" :data="tcp.rows"
                        :bordered="true" style="margin-top:8px" />
        </n-card>
      </div>
      <!-- UDP 埠 -->
      <div>
        <n-card size="small">
          <template #header><CardTitle :icon="LinkIcon" :text="t('netdiag.udp')" /></template>
          <n-input v-model:value="udp.targets" type="textarea" :rows="2"
                   :placeholder="t('netdiag.targets_ph')" />
          <n-space align="center" :size="14" style="margin-top:10px" :wrap="true">
            <span class="nd-lbl">{{ t("netdiag.ports") }}
              <n-input v-model:value="udp.ports" size="small" style="width:180px" placeholder="53, 123, 161" />
            </span>
            <span class="nd-lbl">{{ t("netdiag.timeout") }}
              <n-input-number v-model:value="udp.timeout" :min="1" :max="10" size="small" style="width:92px" />
            </span>
            <n-button type="primary" :loading="udp.busy" @click="runUdp">
              <template #icon><n-icon><SearchIcon /></n-icon></template>{{ t("netdiag.run") }}
            </n-button>
          </n-space>
          <n-alert type="warning" :bordered="false" :show-icon="false" class="nd-udp-note">
            {{ t("netdiag.udp_note") }}
          </n-alert>
          <n-data-table v-if="udp.rows.length" size="small" :columns="udpCols" :data="udp.rows"
                        :bordered="true" style="margin-top:8px" />
        </n-card>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, h, onMounted, reactive, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  NAlert, NButton, NCard, NDataTable, NDivider, NIcon, NInput, NInputNumber,
  NSpace, NTag, useMessage,
} from "naive-ui";
import { LinkIcon, SearchIcon, TopologyIcon } from "@/icons";
import { Antenna as LiveIcon } from "@iconoir/vue";
import CardTitle from "@/components/CardTitle.vue";
import { apiClient, apiErrMsg } from "@/api/client";

const MAX_TARGETS = 64;
const { t } = useI18n();
const msg = useMessage();

interface Caps { ping: boolean; tracepath: boolean; traceroute: boolean; tcp: boolean }
interface PingRow {
  target: string; alive: boolean; sent: number; received: number;
  loss_pct: number | null; rtt_avg_ms: number | null; error: string | null;
}
interface Hop { hop: number; host: string | null; rtt_ms: number | null; note: string | null }
interface TraceRes { target: string; tool: string; path_mtu: number | null; truncated: boolean; hops: Hop[] }
interface TcpRow { target: string; port: number; open: boolean; latency_ms: number | null; error: string | null }
interface UdpRow {
  target: string; port: number; state: "open" | "closed" | "no_reply";
  probe: string; latency_ms: number | null; reply_bytes: number | null; detail: string | null;
}

const caps = ref<Caps | null>(null);
const ping = reactive({ targets: "", count: 3, timeout: 2, concurrency: 8, busy: false, rows: [] as PingRow[] });
// 預設 15：內網目標會提早結束；對不回應的網際網路目標，躍點數直接決定等待時間
const trace = reactive({ target: "", maxHops: 15, busy: false, res: null as TraceRes | null });
const tcp = reactive({ targets: "", ports: "443, 22", timeout: 2, busy: false, rows: [] as TcpRow[] });
const udp = reactive({ targets: "", ports: "53, 123", timeout: 3, busy: false, rows: [] as UdpRow[] });

const pingSummary = computed(() => {
  const up = ping.rows.filter((r) => r.alive).length;
  return t("netdiag.ping_summary", { up, total: ping.rows.length });
});

// 「通/不通」用顏色點表示，掃一眼就看得出來，不必逐格讀字
function dot(ok: boolean) {
  return h("span", { style: `display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:${ok ? "#18a058" : "#d03050"}` });
}

const pingCols = computed(() => [
  { title: t("netdiag.target"), key: "target", width: 190 },
  {
    title: t("netdiag.state"), key: "alive", width: 110,
    render: (r: PingRow) => h("span", null, [dot(r.alive), r.alive ? t("netdiag.up") : t("netdiag.down")]),
  },
  { title: t("netdiag.loss"), key: "loss_pct", width: 100,
    render: (r: PingRow) => (r.loss_pct === null ? "—" : `${r.loss_pct}%`) },
  { title: t("netdiag.rtt_avg"), key: "rtt_avg_ms", width: 120,
    render: (r: PingRow) => (r.rtt_avg_ms === null ? "—" : `${r.rtt_avg_ms} ms`) },
  { title: t("netdiag.note"), key: "error", render: (r: PingRow) => r.error || "" },
]);

const hopCols = computed(() => [
  { title: "#", key: "hop", width: 60 },
  { title: t("netdiag.hop_host"), key: "host",
    render: (h2: Hop) => h2.host || h("span", { style: "opacity:.5" }, t("netdiag.no_reply")) },
  { title: t("netdiag.rtt"), key: "rtt_ms", width: 120,
    render: (h2: Hop) => (h2.rtt_ms === null ? "—" : `${h2.rtt_ms} ms`) },
  { title: t("netdiag.note"), key: "note", render: (h2: Hop) => h2.note || "" },
]);

const tcpCols = computed(() => [
  { title: t("netdiag.target"), key: "target", width: 190 },
  { title: t("netdiag.port"), key: "port", width: 90 },
  {
    title: t("netdiag.state"), key: "open", width: 110,
    render: (r: TcpRow) => h("span", null, [dot(r.open), r.open ? t("netdiag.open") : t("netdiag.closed")]),
  },
  { title: t("netdiag.latency"), key: "latency_ms", width: 120,
    render: (r: TcpRow) => (r.latency_ms === null ? "—" : `${r.latency_ms} ms`) },
  { title: t("netdiag.note"), key: "error", render: (r: TcpRow) => r.error || "" },
]);

const UDP_COLOR: Record<string, string> = { open: "#18a058", closed: "#d03050", no_reply: "#9aa0a6" };
const udpCols = computed(() => [
  { title: t("netdiag.target"), key: "target", width: 170 },
  { title: t("netdiag.port"), key: "port", width: 80 },
  {
    title: t("netdiag.state"), key: "state", width: 150,
    render: (r: UdpRow) => h("span", null, [
      h("span", { style: `display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:${UDP_COLOR[r.state]}` }),
      t(`netdiag.udp_${r.state}`),
    ]),
  },
  { title: t("netdiag.probe"), key: "probe", width: 90 },
  { title: t("netdiag.latency"), key: "latency_ms", width: 110,
    render: (r: UdpRow) => (r.latency_ms === null ? "—" : `${r.latency_ms} ms`) },
  { title: t("netdiag.note"), key: "detail", render: (r: UdpRow) => r.detail || "" },
]);

async function runUdp() {
  if (!udp.targets.trim()) { msg.error(t("netdiag.need_target")); return; }
  const ports = udp.ports.split(/[\s,;]+/).filter(Boolean).map(Number).filter((x) => x >= 1 && x <= 65535);
  if (!ports.length) { msg.error(t("netdiag.need_port")); return; }
  udp.busy = true;
  try {
    const { data } = await apiClient.post("/api/v1/tools/net/udp", {
      targets: udp.targets, ports, timeout: udp.timeout,
    });
    udp.rows = data.results;
  } catch (e) { msg.error(apiErrMsg(e)); } finally { udp.busy = false; }
}

async function runPing() {
  if (!ping.targets.trim()) { msg.error(t("netdiag.need_target")); return; }
  ping.busy = true;
  try {
    const { data } = await apiClient.post("/api/v1/tools/net/ping", {
      targets: ping.targets, count: ping.count,
      timeout: ping.timeout, concurrency: ping.concurrency,
    });
    ping.rows = data.results;
  } catch (e) { msg.error(apiErrMsg(e)); } finally { ping.busy = false; }
}

async function runTrace() {
  if (!trace.target.trim()) { msg.error(t("netdiag.need_target")); return; }
  trace.busy = true;
  try {
    const { data } = await apiClient.post("/api/v1/tools/net/traceroute", {
      target: trace.target, max_hops: trace.maxHops,
    });
    trace.res = data;
  } catch (e) { msg.error(apiErrMsg(e)); } finally { trace.busy = false; }
}

async function runTcp() {
  if (!tcp.targets.trim()) { msg.error(t("netdiag.need_target")); return; }
  const ports = tcp.ports.split(/[\s,;]+/).filter(Boolean).map(Number).filter((n) => n >= 1 && n <= 65535);
  if (!ports.length) { msg.error(t("netdiag.need_port")); return; }
  tcp.busy = true;
  try {
    const { data } = await apiClient.post("/api/v1/tools/net/tcp", {
      targets: tcp.targets, ports, timeout: tcp.timeout,
    });
    tcp.rows = data.results;
  } catch (e) { msg.error(apiErrMsg(e)); } finally { tcp.busy = false; }
}

onMounted(async () => {
  // 伺服器上沒裝 tracepath/traceroute 時要先反灰，而不是讓人按了才看到錯誤
  try { caps.value = (await apiClient.get("/api/v1/tools/net/capabilities")).data; } catch { /* 取不到就不反灰 */ }
});
</script>

<style scoped>
.nd-div { font-size: 13px; font-weight: 600; }
/* 與上方計算工具用同一組格線參數（2 欄、12px、820px 收合），整頁節奏才一致 */
.nd-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  align-items: start;
}
/* 輸入與結果表都寬的（ping、路徑追蹤）佔滿整列，硬塞成半欄反而更擠 */
.nd-wide { grid-column: 1 / -1; }
@media (max-width: 820px) {
  .nd-grid { grid-template-columns: 1fr; }
  .nd-wide { grid-column: auto; }
}
.nd-note { font-size: 12.5px; line-height: 1.7; color: var(--n-text-color-disabled); margin-bottom: 14px; }
.nd-hint { margin-top: 6px; font-size: 12px; line-height: 1.6; color: var(--n-text-color-disabled); }
.nd-lbl { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.nd-udp-note { margin-top: 10px; font-size: 12px; line-height: 1.7; }
.nd-sum { margin-top: 10px; font-size: 13px; font-weight: 600; }
</style>
