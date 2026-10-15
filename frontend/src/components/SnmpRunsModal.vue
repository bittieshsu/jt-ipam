<script setup lang="ts">
/** 單一收集目標的收集記錄（SNMP 頁的「記錄」按鈕、裝置詳情的 SNMP 區塊共用）。 */
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { NModal, NButton, NIcon, NSpace, useMessage } from "naive-ui";
import { AuditIcon, RefreshIcon, CancelIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import { listSnmpTargetRuns, type SnmpRun } from "@/api/snmp";
import SnmpRunsTable from "@/components/SnmpRunsTable.vue";

const props = defineProps<{ show: boolean; target: { id: string; device_name?: string | null } | null }>();
const emit = defineEmits<{ (e: "update:show", v: boolean): void }>();
const { t } = useI18n();
const msg = useMessage();
const runs = ref<SnmpRun[]>([]);
const loading = ref(false);

async function load() {
  if (!props.target) return;
  loading.value = true;
  try { runs.value = await listSnmpTargetRuns(props.target.id, { limit: 300 }); }
  catch (e) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}
watch(() => props.show, (v) => { if (v) { runs.value = []; void load(); } }, { immediate: true });
</script>

<template>
  <n-modal :show="show" preset="card" style="width: 1000px; max-width: calc(100vw - 32px)"
           data-testid="snmp-runs-modal" @update:show="(v: boolean) => emit('update:show', v)">
    <template #header>
      <n-space align="center" :size="8" :wrap-item="false">
        <n-icon :size="20"><AuditIcon /></n-icon>
        <span>{{ t("snmp.runs_title") }}</span>
        <span v-if="target?.device_name" style="font-size: 13px; opacity: .65">{{ target.device_name }}</span>
      </n-space>
    </template>
    <SnmpRunsTable :runs="runs" :loading="loading" prefs-key="snmp_target_runs">
      <template #toolbar>
        <n-button size="small" :loading="loading" @click="load">
          <template #icon><n-icon><RefreshIcon /></n-icon></template>
          {{ t("common.refresh") }}
        </n-button>
      </template>
    </SnmpRunsTable>
    <n-space justify="end" style="margin-top: 12px">
      <n-button @click="emit('update:show', false)">
        <template #icon><n-icon><CancelIcon /></n-icon></template>
        {{ t("common.close") }}
      </n-button>
    </n-space>
  </n-modal>
</template>
