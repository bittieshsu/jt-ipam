<script setup lang="ts">
/**
 * 外部 DNS 解析器（管理 → 系統設定；使用者 2026-10-19 定案）。
 *
 * 對外開放服務的 FQDN 要從網際網路上看是什麼：用這裡設定的解析器（可多台）查、另外直接問網域的權威伺服器。
 * 只查「對外網域」底下的名稱 —— 內部名稱送去公共解析器等於把內部架構洩漏出去。
 * 「我方公網位址」補上沒接整合的線路；防火牆自己的介面位址會從各整合自動帶入。
 * 之後 DNS 查詢工具、AI 工具、懸空 DNS 也會用這裡的解析器。
 */
import { onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { NButton, NDynamicTags, NIcon, NInput, NInputNumber, NSwitch, useMessage } from "naive-ui";
import { DeleteIcon, PlusIcon, RefreshIcon, SaveIcon, TestIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import { getDnsResolvers, putDnsResolvers, testDnsResolver, type DnsResolversConfig } from "@/api/system";
import { runExternalDns } from "@/api/attackSurface";
import { trackTask } from "@/composables/useTaskTracker";
import { fmtDateTime } from "@/utils/datetime";

const { t } = useI18n();
const msg = useMessage();
const cfg = ref<DnsResolversConfig | null>(null);
const saving = ref(false);
const running = ref(false);
const testing = ref<number | null>(null);

async function load() {
  try { cfg.value = await getDnsResolvers(); } catch (e) { msg.error(apiErrMsg(e)); }
}
onMounted(load);

function addResolver() {
  cfg.value?.resolvers.push({ name: "", address: "", port: 53, enabled: true });
}
async function save() {
  if (!cfg.value) return;
  saving.value = true;
  try {
    const { state: _s, ...body } = cfg.value;
    body.resolvers = body.resolvers.filter((r) => r.address.trim());
    cfg.value = await putDnsResolvers(body);
    msg.success(t("common.saved"));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { saving.value = false; }
}
async function test(i: number) {
  const r = cfg.value?.resolvers[i];
  if (!r?.address.trim()) return;
  testing.value = i;
  try {
    const res = await testDnsResolver(r.address.trim(), r.port || 53);
    const addrs = [...res.a, ...res.aaaa].join("、");
    if (res.rcode === "NOERROR") msg.success(t("dns_resolvers.test_ok", { name: res.name, addrs: addrs || "—" }));
    else msg.warning(t("dns_resolvers.test_fail", { name: res.name, rcode: res.rcode, error: res.error ?? "" }));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { testing.value = null; }
}
async function runNow() {
  running.value = true;
  try {
    const r = await runExternalDns();
    trackTask(r.task_id, { onDone: () => { void load(); } });
    msg.info(t("dns_resolvers.run_started"));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { running.value = false; }
}
function summaryText(s: Record<string, unknown> | undefined): string {
  if (!s) return "";
  if (s.skipped) return t("dns_resolvers.not_configured");
  return t("dns_resolvers.summary", { names: s.names ?? 0, failed: s.failed ?? 0, changed: s.changed ?? 0 });
}
</script>

<template>
  <div v-if="cfg" data-testid="dns-resolvers">
    <div class="hint" style="line-height:1.6; margin-bottom: 10px">{{ t("dns_resolvers.intro") }}</div>
    <label class="dr-label">{{ t("dns_resolvers.resolvers") }}</label>
    <div v-for="(r, i) in cfg.resolvers" :key="i" class="dr-row" data-testid="dns-resolver-row">
      <n-input v-model:value="r.name" :placeholder="t('dns_resolvers.name_ph')" style="width: 160px" />
      <n-input v-model:value="r.address" placeholder="1.1.1.1" style="width: 180px" />
      <n-input-number v-model:value="r.port" :min="1" :max="65535" :show-button="false" style="width: 90px" />
      <n-switch v-model:value="r.enabled" />
      <n-button size="small" :loading="testing === i" :disabled="!r.address.trim()" @click="test(i)">
        <template #icon><n-icon><TestIcon /></n-icon></template>{{ t("dns_resolvers.test") }}
      </n-button>
      <n-button size="small" type="error" ghost :title="t('common.delete')" @click="cfg.resolvers.splice(i, 1)">
        <template #icon><n-icon><DeleteIcon /></n-icon></template>
      </n-button>
    </div>
    <n-button size="small" dashed :disabled="cfg.resolvers.length >= 10" style="margin: 4px 0 12px" @click="addResolver">
      <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("dns_resolvers.add") }}
    </n-button>

    <div class="dr-grid">
      <div class="fld">
        <label>{{ t("dns_resolvers.public_domains") }}</label>
        <n-dynamic-tags v-model:value="cfg.public_domains" data-testid="dns-public-domains" />
        <div class="hint">{{ t("dns_resolvers.public_domains_hint") }}</div>
      </div>
      <div class="fld">
        <label>{{ t("dns_resolvers.public_ranges") }}</label>
        <n-dynamic-tags v-model:value="cfg.public_ranges" />
        <div class="hint">{{ t("dns_resolvers.public_ranges_hint") }}</div>
      </div>
      <div class="fld">
        <label>{{ t("dns_resolvers.interval") }}</label>
        <n-input-number v-model:value="cfg.interval_hours" :min="1" :max="168" style="width: 140px">
          <template #suffix>{{ t("dns_resolvers.hours") }}</template>
        </n-input-number>
      </div>
      <div class="fld">
        <div class="dr-switch"><n-switch v-model:value="cfg.query_authoritative" />{{ t("dns_resolvers.authoritative") }}</div>
        <div class="hint">{{ t("dns_resolvers.authoritative_hint") }}</div>
        <div class="dr-switch" style="margin-top: 8px"><n-switch v-model:value="cfg.notify_on_change" />{{ t("dns_resolvers.notify") }}</div>
      </div>
    </div>
    <div class="dr-actions">
      <div style="flex:1"></div>
      <n-button size="small" :loading="saving" @click="save">
        <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
      </n-button>
      <n-button size="small" type="primary" :loading="running" data-testid="dns-run-now" @click="runNow">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>{{ t("dns_resolvers.run_now") }}
      </n-button>
    </div>
    <div v-if="cfg.state?.last_run_at" class="dr-status" style="opacity:.75">
      {{ t("dns_resolvers.last_run") }}：{{ fmtDateTime(cfg.state.last_run_at) }}
      <span v-if="cfg.state.last_summary"> · {{ summaryText(cfg.state.last_summary) }}</span>
    </div>
  </div>
</template>

<style scoped>
/* 與系統設定頁其他卡片同一套外觀（那邊的樣式是 scoped，子元件拿不到，照抄一份） */
.dr-label, .fld label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 5px; }
.hint { font-size: 11px; opacity: 0.65; margin-top: 4px; }
.dr-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 640px) { .dr-grid { grid-template-columns: 1fr; } }
.dr-actions { display: flex; align-items: center; gap: 12px; margin-top: 14px; flex-wrap: wrap; }
.dr-status { margin-top: 12px; font-size: 12px; }
.dr-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 6px; }
.dr-switch { display: flex; align-items: center; gap: 8px; font-size: 13px; }
</style>
