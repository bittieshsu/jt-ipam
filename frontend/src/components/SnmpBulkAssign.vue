<script setup lang="ts">
/**
 * 裝置清單的「批次指派 SNMP 收集」（定案設計：裝置清單可批次指派憑證）。
 *
 * 選好的裝置：沒有收集目標的新建、已有的改成這組憑證。收集項目預設「依裝置類型」—— 由後端照
 * 單筆表單同一套規則給（路由器 ARP＋介面、交換器 MAC 表＋鄰居＋介面、其他只讀介面）；選「自訂」才照勾的。
 * 違反單位限制或找不到的裝置逐台列出原因，其餘照做。
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NModal, NForm, NFormItem, NSelect, NCheckbox, NRadioGroup, NRadio, NButton, NIcon, NSpace, NAlert,
  useMessage,
} from "naive-ui";
import { SaveIcon, CancelIcon, PlusIcon, SnmpIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import {
  bulkSnmpTargets, listSnmpCredentials, listSnmpAgents, SNMP_SECTIONS,
  type SnmpCredential, type SnmpAgentInfo, type SnmpBulkResult, type SnmpCollector, type SnmpSection,
} from "@/api/snmp";
import { defaultCredentialId, sectionLabel } from "@/utils/snmp";
import { srvText } from "@/utils/wsError";
import SnmpCredentialForm from "@/components/SnmpCredentialForm.vue";

const props = defineProps<{ show: boolean; deviceIds: string[] }>();
const emit = defineEmits<{ (e: "update:show", v: boolean): void; (e: "done"): void }>();

const { t } = useI18n();
const msg = useMessage();

const creds = ref<SnmpCredential[]>([]);
const agents = ref<SnmpAgentInfo[]>([]);
const credentialId = ref<string | null>(null);
const flagsMode = ref<"by_type" | "custom">("by_type");
const flags = ref<Record<SnmpSection, boolean>>({ arp: false, fdb: true, neighbors: true, interfaces: true });
const collector = ref<SnmpCollector>("auto");
const agentId = ref<string | null>(null);
const saving = ref(false);
const result = ref<SnmpBulkResult | null>(null);
const credFormShow = ref(false);

async function loadCreds(selectId?: string) {
  try { creds.value = await listSnmpCredentials(); } catch (e) { msg.error(apiErrMsg(e)); }
  credentialId.value = selectId ?? credentialId.value ?? defaultCredentialId(creds.value);
}

watch(() => props.show, (v) => {
  if (!v) return;
  result.value = null;
  credentialId.value = null;
  flagsMode.value = "by_type";
  collector.value = "auto";
  agentId.value = null;
  void loadCreds();
  void listSnmpAgents().then((a) => { agents.value = a.items; }).catch(() => { /* 只影響「指定代理」的選單 */ });
}, { immediate: true });

const credOptions = computed(() => creds.value.map((c) => ({
  value: c.id,
  label: [c.name, c.version, c.is_default ? t("snmp.default_tag") : "", c.customer_name ?? ""].filter(Boolean).join(" · "),
})));
const agentOptions = computed(() => agents.value.map((a) => ({
  value: a.agent_id,
  label: [a.name, a.is_local ? t("snmp.agent_local") : "", a.capable ? t("snmp.agent_capable") : t("snmp.agent_not_capable")]
    .filter(Boolean).join(" · "),
})));
const canSave = computed(() => !!credentialId.value && props.deviceIds.length > 0
  && (collector.value !== "agent" || !!agentId.value));

async function submit() {
  if (!canSave.value) return;
  saving.value = true;
  try {
    const payload: Parameters<typeof bulkSnmpTargets>[0] = {
      device_ids: props.deviceIds, credential_id: credentialId.value, collector: collector.value,
      agent_id: collector.value === "agent" ? agentId.value : null,
    };
    if (flagsMode.value === "custom") {
      payload.collect_arp = flags.value.arp;
      payload.collect_fdb = flags.value.fdb;
      payload.collect_neighbors = flags.value.neighbors;
      payload.collect_interfaces = flags.value.interfaces;
    }
    result.value = await bulkSnmpTargets(payload);
    msg.success(t("snmp.bulk_done", { created: result.value.created, updated: result.value.updated }));
    emit("done");
    if (!result.value.skipped.length) emit("update:show", false);
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { saving.value = false; }
}
</script>

<template>
  <n-modal :show="show" preset="card" style="width: 560px; max-width: calc(100vw - 32px)"
           data-testid="snmp-bulk-dialog" @update:show="(v: boolean) => emit('update:show', v)">
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="20"><SnmpIcon /></n-icon>
        <span>{{ t("snmp.bulk_title", { n: deviceIds.length }) }}</span>
      </n-space>
    </template>
    <n-form label-placement="top">
      <n-form-item :label="t('snmp.credential')">
        <div style="width: 100%">
          <n-space :wrap="false" :wrap-item="false" style="width: 100%">
            <n-select v-model:value="credentialId" :options="credOptions" filterable style="flex: 1"
                      data-testid="snmp-bulk-credential" :placeholder="t('snmp.bulk_pick_credential')" />
            <n-button @click="credFormShow = true">
              <template #icon><n-icon><PlusIcon /></n-icon></template>
              {{ t("snmp.new_credential") }}
            </n-button>
          </n-space>
          <div class="sb-hint">{{ t("snmp.bulk_existing_hint") }}</div>
        </div>
      </n-form-item>
      <n-form-item :label="t('snmp.collect_items')">
        <div style="width: 100%">
          <n-radio-group v-model:value="flagsMode" data-testid="snmp-bulk-flags-mode">
            <n-radio value="by_type">{{ t("snmp.bulk_flags_by_type") }}</n-radio>
            <n-radio value="custom">{{ t("snmp.bulk_flags_custom") }}</n-radio>
          </n-radio-group>
          <n-space v-if="flagsMode === 'custom'" :size="16" style="margin-top: 8px">
            <n-checkbox v-for="s in SNMP_SECTIONS" :key="s" v-model:checked="flags[s]">
              {{ sectionLabel(s, t) }}
            </n-checkbox>
          </n-space>
          <div class="sb-hint">{{ t("snmp.bulk_flags_hint") }}</div>
        </div>
      </n-form-item>
      <n-form-item :label="t('snmp.collector')">
        <div style="width: 100%">
          <n-radio-group v-model:value="collector">
            <n-radio value="auto">{{ t("snmp.collector_auto") }}</n-radio>
            <n-radio value="agent">{{ t("snmp.collector_agent") }}</n-radio>
            <n-radio value="server">{{ t("snmp.collector_server") }}</n-radio>
          </n-radio-group>
          <n-select v-if="collector === 'agent'" v-model:value="agentId" :options="agentOptions"
                    style="margin-top: 8px" />
        </div>
      </n-form-item>
    </n-form>
    <n-alert v-if="result && result.skipped.length" type="warning" :bordered="false" style="margin-bottom: 12px"
             data-testid="snmp-bulk-skipped">
      <div>{{ t("snmp.bulk_skipped", { n: result.skipped.length }) }}</div>
      <ul class="sb-skipped">
        <li v-for="s in result.skipped" :key="s.device_id">
          <b>{{ s.device_name || s.device_id }}</b>：{{ srvText(s, s.message ?? "") }}
        </li>
      </ul>
    </n-alert>
    <n-space justify="end">
      <n-button @click="emit('update:show', false)">
        <template #icon><n-icon><CancelIcon /></n-icon></template>
        {{ result ? t("common.close") : t("common.cancel") }}
      </n-button>
      <n-button type="primary" :loading="saving" :disabled="!canSave || saving" data-testid="snmp-bulk-save"
                @click="submit">
        <template #icon><n-icon><SaveIcon /></n-icon></template>
        {{ t("snmp.bulk_apply") }}
      </n-button>
    </n-space>
    <SnmpCredentialForm v-model:show="credFormShow" @saved="(c: SnmpCredential) => loadCreds(c.id)" />
  </n-modal>
</template>

<style scoped>
.sb-hint { font-size: 12px; opacity: .7; margin-top: 4px; }
.sb-skipped { margin: 6px 0 0; padding-left: 18px; }
</style>
