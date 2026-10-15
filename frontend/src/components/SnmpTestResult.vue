<script setup lang="ts">
/**
 * SNMP 測試對話框（SNMP 頁、裝置詳情、收集目標表單共用）。
 *
 * 一次可以試最多 5 組憑證，**都只對這一台設備**（不會對網段裡其他主機送憑證：v2c 是明文）。
 * 按下測試 → 伺服器建立工作 → 負責的掃描代理下一次輪詢領走 → 這裡每 1.5 秒問一次結果。
 * 等待期間按鈕反灰＋轉圈，並講清楚現在在等什麼（代理還沒領走 vs. 正在讀）。
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NModal, NSelect, NButton, NIcon, NSpace, NAlert, NSpin, NFormItem, useMessage,
} from "naive-ui";
import { TestIcon, CancelIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import {
  listSnmpCredentials, startSnmpTest, SNMP_MAX_TEST_CREDENTIALS, type SnmpCredential,
} from "@/api/snmp";
import { useSnmpJob } from "@/composables/useSnmpJob";
import SnmpTestItems from "@/components/SnmpTestItems.vue";

interface TargetLike { id: string; device_name?: string | null; credential_id?: string | null; mgmt_ip?: string | null }

const props = defineProps<{
  show: boolean;
  target: TargetLike | null;
  /** 一打開就先選好的憑證（收集目標表單裡還沒存檔的選擇也可以先試） */
  initialCredentialIds?: string[] | null;
  /** 打開後立刻開始測試 */
  autoStart?: boolean;
}>();
const emit = defineEmits<{ (e: "update:show", v: boolean): void; (e: "finished"): void }>();

const { t } = useI18n();
const msg = useMessage();
const creds = ref<SnmpCredential[]>([]);
const picked = ref<string[]>([]);
const job = useSnmpJob();
const startError = ref("");

const credOptions = computed(() => creds.value.map((c) => ({
  value: c.id,
  label: [c.name, c.version, c.is_default ? t("snmp.default_tag") : ""].filter(Boolean).join(" · "),
  disabled: picked.value.length >= SNMP_MAX_TEST_CREDENTIALS && !picked.value.includes(c.id),
})));

watch(() => props.show, async (v) => {
  if (!v) { job.stop(); return; }
  startError.value = "";
  job.job.value = null;
  const init = (props.initialCredentialIds ?? []).filter((x): x is string => !!x);
  picked.value = (init.length ? init : (props.target?.credential_id ? [props.target.credential_id] : []))
    .slice(0, SNMP_MAX_TEST_CREDENTIALS);
  try { creds.value = await listSnmpCredentials(); } catch (e) { msg.error(apiErrMsg(e)); }
  if (props.autoStart) void runTest();
}, { immediate: true });

async function runTest() {
  if (!props.target || job.running.value) return;
  startError.value = "";
  try {
    const j = await job.run(() => startSnmpTest(props.target!.id, picked.value));
    if (j) emit("finished");
  } catch (e) {
    startError.value = apiErrMsg(e);
  }
}

const items = computed(() => job.job.value?.result?.items ?? []);
const status = computed(() => job.job.value?.status ?? null);
const waitingText = computed(() => {
  const agent = job.started.value?.agent_name ?? "";
  if (status.value === "running") return t("snmp.test_running", { agent });
  return t("snmp.test_pending", { agent });
});

function close() { emit("update:show", false); }
</script>

<template>
  <n-modal :show="show" preset="card" style="width: 680px; max-width: calc(100vw - 32px)"
           data-testid="snmp-test-dialog" @update:show="(v: boolean) => emit('update:show', v)">
    <template #header>
      <n-space align="center" :size="8" :wrap-item="false">
        <n-icon :size="20"><TestIcon /></n-icon>
        <span>{{ t("snmp.test_title") }}</span>
        <span v-if="target" class="str-dim">{{ target.device_name }}<template v-if="target.mgmt_ip"> · {{ target.mgmt_ip }}</template></span>
      </n-space>
    </template>
    <n-form-item :label="t('snmp.test_credentials')" :show-feedback="false">
      <div style="width: 100%">
        <n-select v-model:value="picked" :options="credOptions" multiple filterable
                  :max-tag-count="SNMP_MAX_TEST_CREDENTIALS" data-testid="snmp-test-creds" />
        <div class="str-hint">{{ t("snmp.test_credentials_hint", { n: SNMP_MAX_TEST_CREDENTIALS }) }}</div>
      </div>
    </n-form-item>
    <n-space justify="end" style="margin: 10px 0 12px">
      <n-button type="primary" :loading="job.running.value" :disabled="job.running.value || !target"
                data-testid="snmp-test-run" @click="runTest">
        <template #icon><n-icon><TestIcon /></n-icon></template>
        {{ t("common.test") }}
      </n-button>
    </n-space>

    <n-alert v-if="startError" type="error" :bordered="false" style="margin-bottom: 10px" data-testid="snmp-test-error">
      {{ startError }}
    </n-alert>
    <div v-if="job.running.value" class="str-wait" data-testid="snmp-test-waiting">
      <n-spin size="small" /> <span>{{ waitingText }}</span>
    </div>
    <template v-else-if="job.job.value">
      <n-alert v-if="status === 'expired'" type="warning" :bordered="false" style="margin-bottom: 10px">
        {{ t("snmp.test_expired") }}
      </n-alert>
      <n-alert v-else-if="status === 'failed'" type="error" :bordered="false" style="margin-bottom: 10px">
        {{ t("snmp.test_job_failed", { reason: job.job.value.error || "—" }) }}
      </n-alert>
      <n-alert v-else-if="status !== 'done'" type="warning" :bordered="false" style="margin-bottom: 10px">
        {{ t("snmp.test_no_answer") }}
      </n-alert>
      <SnmpTestItems v-if="items.length" :items="items" />
    </template>

    <n-space justify="end" style="margin-top: 14px">
      <n-button @click="close">
        <template #icon><n-icon><CancelIcon /></n-icon></template>
        {{ t("common.close") }}
      </n-button>
    </n-space>
  </n-modal>
</template>

<style scoped>
.str-dim { font-size: 13px; opacity: .65; font-weight: normal; }
.str-hint { font-size: 12px; opacity: .7; margin-top: 4px; }
.str-wait { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 8px 0; }
</style>
