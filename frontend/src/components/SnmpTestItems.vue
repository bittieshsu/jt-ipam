<script setup lang="ts">
/**
 * SNMP 測試的逐組憑證結果（測試對話框與收集記錄展開列共用）：
 * 成功與否、sysName／sysDescr／sysObjectID／開機時間、耗時，以及每個收集項目讀到幾列或為什麼沒有。
 */
import { useI18n } from "vue-i18n";
import { NTag, NSpace } from "naive-ui";
import { SNMP_SECTIONS, type SnmpTestItem } from "@/api/snmp";
import { fmtUptime, sectionLabel, sectionTagType, sectionText } from "@/utils/snmp";

defineProps<{ items: SnmpTestItem[] }>();
const { t } = useI18n();

function statusLabel(st: string | undefined): string {
  if (!st) return "—";
  const key = `snmp.sec_status_${st}`;
  const out = t(key);
  return out === key ? st : out;
}
</script>

<template>
  <div class="snmp-test-items">
    <div v-for="(it, i) in items" :key="(it.credential_id ?? '') + i" class="snmp-test-item"
         data-testid="snmp-test-item">
      <n-space align="center" :size="8" :wrap-item="false" class="sti-head">
        <n-tag size="small" :type="it.ok ? 'success' : 'error'" :bordered="false" data-testid="snmp-test-ok">
          {{ it.ok ? t("snmp.test_ok") : t("snmp.test_fail") }}
        </n-tag>
        <strong>{{ it.credential_name || it.credential_id || "—" }}</strong>
        <span v-if="it.elapsed_ms != null" class="sti-dim">{{ t("snmp.elapsed_ms", { n: it.elapsed_ms }) }}</span>
      </n-space>
      <div v-if="it.error" class="sti-error">{{ it.error }}</div>
      <table v-if="it.system" class="sti-sys">
        <tbody>
          <tr><th>sysName</th><td data-testid="snmp-sysname">{{ it.system.sys_name || "—" }}</td></tr>
          <tr><th>sysDescr</th><td class="sti-wrap">{{ it.system.sys_descr || "—" }}</td></tr>
          <tr><th>sysObjectID</th><td><code>{{ it.system.sys_object_id || "—" }}</code></td></tr>
          <tr><th>{{ t("snmp.uptime") }}</th><td>{{ fmtUptime(it.system.sys_uptime, t) }}</td></tr>
        </tbody>
      </table>
      <table v-if="it.ok || Object.keys(it.sections ?? {}).length" class="sti-secs">
        <tbody>
          <tr v-for="s in SNMP_SECTIONS" :key="s">
            <th>{{ sectionLabel(s, t) }}</th>
            <td style="width: 1%">
              <n-tag size="tiny" :bordered="false" :type="sectionTagType(it.sections?.[s]?.status)">
                {{ statusLabel(it.sections?.[s]?.status) }}
              </n-tag>
            </td>
            <td class="sti-wrap" :data-testid="`snmp-sec-${s}`">{{ sectionText(it.sections?.[s], t) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.snmp-test-items { display: flex; flex-direction: column; gap: 10px; }
.snmp-test-item {
  border: 1px solid var(--n-border-color, rgba(128, 128, 128, .2));
  border-radius: 6px; padding: 10px 12px;
}
.sti-dim { font-size: 12px; opacity: .65; }
.sti-error { color: var(--n-error-color, #d03050); font-size: 13px; margin-top: 6px; word-break: break-word; }
.sti-sys, .sti-secs { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
.sti-sys th, .sti-secs th {
  text-align: left; font-weight: 500; opacity: .7; white-space: nowrap; padding: 3px 12px 3px 0; width: 1%;
  vertical-align: top;
}
.sti-sys td, .sti-secs td { padding: 3px 8px 3px 0; vertical-align: top; }
.sti-wrap { word-break: break-word; }
.sti-sys code { font-size: 12px; }
</style>
