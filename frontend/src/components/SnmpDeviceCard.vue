<script setup lang="ts">
/**
 * 裝置詳情的 SNMP 區塊（限管理員）。
 * 有收集目標：狀態（憑證、收集者、最後收集、最後錯誤、各區段）＋編輯／測試／立即收集／記錄；
 * 沒有：「啟用 SNMP 收集」，打開同一個收集目標表單並固定這台裝置。
 */
import { computed, onMounted, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import {
  NCard, NButton, NIcon, NSpace, NTag, NDescriptions, NDescriptionsItem, NTooltip, NSpin, useMessage,
} from "naive-ui";
import { SnmpIcon, EditIcon, TestIcon, SyncIcon, AuditIcon, OpenNewWindowIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import { getSnmpTargetForDevice, SNMP_SECTIONS, type SnmpSection, type SnmpTarget } from "@/api/snmp";
import { fmtDateTime } from "@/utils/datetime";
import { srvText } from "@/utils/wsError";
import {
  errText, HEALTH_TAG, sectionLabel, sectionTagType, sectionText, targetHealth,
} from "@/utils/snmp";
import { useSnmpCollect } from "@/composables/useSnmpCollect";
import SnmpTargetForm from "@/components/SnmpTargetForm.vue";
import SnmpTestResult from "@/components/SnmpTestResult.vue";
import SnmpRunsModal from "@/components/SnmpRunsModal.vue";

const props = defineProps<{ deviceId: string; deviceName: string; deviceType?: string | null }>();
const emit = defineEmits<{ (e: "loaded"): void }>();
const { t } = useI18n();
const msg = useMessage();
const router = useRouter();

const target = ref<SnmpTarget | null>(null);
const loading = ref(false);
const loaded = ref(false);
const formShow = ref(false);
const testShow = ref(false);
const runsShow = ref(false);
const { busy, collect } = useSnmpCollect(() => void load());

async function load() {
  loading.value = true;
  try {
    target.value = await getSnmpTargetForDevice(props.deviceId);
  } catch (e) {
    msg.error(apiErrMsg(e));
  } finally {
    loading.value = false;
    loaded.value = true;
    emit("loaded");
  }
}
onMounted(load);
watch(() => props.deviceId, () => void load());

const presetDevice = computed(() => ({ id: props.deviceId, name: props.deviceName, type: props.deviceType ?? null }));
const health = computed(() => (target.value ? targetHealth(target.value) : null));

function collectorText(tg: SnmpTarget): string {
  const agent = tg.effective_agent_name ?? "—";
  if (tg.collector === "server") return t("snmp.collector_server_named", { agent });
  if (tg.collector === "agent") return t("snmp.collector_agent_named", { agent });
  return t("snmp.collector_auto_named", { agent });
}

function secResult(s: SnmpSection) {
  return target.value?.last_summary?.sections?.[s];
}
const enabledSections = computed(() => SNMP_SECTIONS.filter((s) => target.value?.[`collect_${s}`]));

function onSaved(tg: SnmpTarget) { target.value = tg; }
</script>

<template>
  <n-card data-testid="device-snmp-card">
    <!-- 標題與裝置頁其他卡片同一個寫法（cardHead：18px 圖示、間距 8px、預設尺寸），高度才一致 -->
    <template #header>
      <span style="display: inline-flex; align-items: center; gap: 8px">
        <n-icon :size="18"><SnmpIcon /></n-icon>
        <span>{{ t("snmp.device_card_title") }}</span>
        <n-tag v-if="health" size="small" :type="HEALTH_TAG[health]">
          {{ t(`snmp.health_${health}`) }}
        </n-tag>
      </span>
    </template>
    <n-spin :show="loading && !loaded">
      <template v-if="target">
        <n-space justify="end" style="margin-bottom: 10px">
          <n-button size="small" type="success" data-testid="device-snmp-edit" @click="formShow = true">
            <template #icon><n-icon><EditIcon /></n-icon></template>
            {{ t("common.edit") }}
          </n-button>
          <n-button size="small" data-testid="device-snmp-test" @click="testShow = true">
            <template #icon><n-icon><TestIcon /></n-icon></template>
            {{ t("common.test") }}
          </n-button>
          <n-button size="small" type="primary" :loading="!!busy[target.id]"
                    :disabled="!!busy[target.id] || !target.enabled" data-testid="device-snmp-collect"
                    @click="collect(target)">
            <template #icon><n-icon><SyncIcon /></n-icon></template>
            {{ t("snmp.collect_now") }}
          </n-button>
          <n-button size="small" @click="runsShow = true">
            <template #icon><n-icon><AuditIcon /></n-icon></template>
            {{ t("snmp.records") }}
          </n-button>
          <n-button size="small" quaternary type="primary" @click="router.push({ name: 'snmp' })">
            <template #icon><n-icon><OpenNewWindowIcon /></n-icon></template>
            {{ t("snmp.open_page") }}
          </n-button>
        </n-space>
        <n-descriptions bordered :column="2" size="small" label-placement="left"
                        :label-style="{ whiteSpace: 'nowrap' }">
          <n-descriptions-item :label="t('snmp.credential')">
            {{ target.credential_name ?? "—" }}
            <n-tag v-if="target.credential_version" size="tiny" :bordered="false"
                   :type="target.credential_version === 'v2c' ? 'warning' : 'success'" style="margin-left: 4px">
              {{ target.credential_version }}
            </n-tag>
          </n-descriptions-item>
          <n-descriptions-item :label="t('snmp.mgmt_ip')">
            {{ target.mgmt_ip ? `${target.mgmt_ip}:${target.port}` : "—" }}
          </n-descriptions-item>
          <n-descriptions-item :label="t('snmp.collector')">
            <span v-if="target.collector_error" class="sdc-err">{{ srvText(target.collector_error) }}</span>
            <template v-else>
              {{ collectorText(target) }}
              <span v-if="target.collector_subnet_cidr" class="sdc-dim">· {{ target.collector_subnet_cidr }}</span>
            </template>
          </n-descriptions-item>
          <n-descriptions-item :label="t('snmp.last_collect')">
            {{ fmtDateTime(target.last_run_at) }}
            <span v-if="target.last_ok_at && target.last_ok_at !== target.last_run_at" class="sdc-dim">
              {{ t("snmp.last_ok", { at: fmtDateTime(target.last_ok_at) }) }}
            </span>
          </n-descriptions-item>
          <n-descriptions-item :label="t('snmp.collect_items')" :span="2">
            <n-space :size="4">
              <n-tooltip v-for="s in enabledSections" :key="s">
                <template #trigger>
                  <n-tag size="small" :bordered="false" :type="secResult(s) ? sectionTagType(secResult(s)?.status) : 'default'">
                    {{ sectionLabel(s, t) }}<template v-if="typeof secResult(s)?.rows === 'number'"> {{ secResult(s)?.rows }}</template>
                  </n-tag>
                </template>
                {{ sectionText(secResult(s), t) }}<template v-if="secResult(s)?.at"> · {{ fmtDateTime(secResult(s)?.at) }}</template>
              </n-tooltip>
            </n-space>
          </n-descriptions-item>
          <n-descriptions-item v-if="target.last_summary?.system?.sys_descr" label="sysDescr" :span="2">
            {{ target.last_summary.system.sys_descr }}
          </n-descriptions-item>
          <n-descriptions-item v-if="target.last_error || target.last_error_code" :label="t('cols.last_error')" :span="2">
            <span class="sdc-err">{{ errText(target.last_error_code, target.last_error) }}</span>
          </n-descriptions-item>
        </n-descriptions>
      </template>
    </n-spin>

    <SnmpTargetForm v-model:show="formShow" :target="target" :preset-device="target ? null : presetDevice"
                    @saved="onSaved" />
    <SnmpTestResult v-model:show="testShow" :target="target" @finished="load" />
    <SnmpRunsModal v-model:show="runsShow" :target="target" />
  </n-card>
</template>

<style scoped>
.sdc-dim { opacity: .65; font-size: 12px; margin-left: 4px; }
.sdc-err { color: var(--n-error-color, #d03050); word-break: break-word; }
</style>
