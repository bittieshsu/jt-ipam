<script setup lang="ts">
/**
 * 系統診斷（管理）：把後端查得到的狀況一次列出來，每一項都附「該怎麼修」。
 *
 * 由來（2026-09-05 客戶回報）：儀表板數得出 55 台裝置、點進裝置清單卻是
 * 「Internal Server Error」加一片空白。原因是資料庫結構落後於程式 —— 系統其實
 * 查得出來，卻沒有任何地方講，只能請客戶去伺服器上跑 CLI 版的 doctor。
 * 這一頁就是把那件事搬到畫面上。
 *
 * 系統層的檢查（systemd、nginx、備份檔、掃描代理）後端看不到，頁面上會明講，
 * 不會讓人以為「這裡全綠就等於一切正常」。
 */
import { computed, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  NCard, NSpace, NButton, NIcon, NTag, NAlert, NSpin, NEmpty, useMessage,
} from "naive-ui";
import { apiClient, apiErrMsg } from "@/api/client";
import { RefreshIcon, ExportIcon, TestIcon } from "@/icons";
import { fmtDateTime } from "@/utils/datetime";

const { t } = useI18n();
const msg = useMessage();

interface Check {
  key: string; title: string; status: "ok" | "warn" | "bad";
  detail?: string; fix?: string;
}
interface Report {
  generated_at: string; ok: number; warn: number; bad: number; checks: Check[];
}

const report = ref<Report | null>(null);
const loading = ref(false);

async function run() {
  loading.value = true;
  try {
    const { data } = await apiClient.get<Report>("/api/v1/system/doctor");
    report.value = data;
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}

/** 下載純文字報告 —— 直接貼進工單用（不是給機器讀的 JSON）。 */
async function download() {
  try {
    const { data } = await apiClient.get<string>("/api/v1/system/doctor/report",
                                                 { responseType: "text" });
    const stamp = new Date().toISOString().replace(/[:T]/g, "-").slice(0, 19);
    const url = URL.createObjectURL(new Blob([data], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `jt-ipam-doctor-${stamp}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { msg.error(apiErrMsg(e)); }
}

const overall = computed<"ok" | "warn" | "bad">(() => {
  const r = report.value;
  if (!r) return "ok";
  return r.bad ? "bad" : r.warn ? "warn" : "ok";
});
const TYPE = { ok: "success", warn: "warning", bad: "error" } as const;

onMounted(() => { void run(); });
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><TestIcon /></n-icon>
        <span>{{ t("doctor.title") }}</span>
      </n-space>
    </template>

    <n-space align="center" class="doc-bar">
      <n-button type="primary" @click="run" :loading="loading">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("doctor.run") }}
      </n-button>
      <n-button :disabled="!report" @click="download">
        <template #icon><n-icon><ExportIcon /></n-icon></template>
        {{ t("doctor.download") }}
      </n-button>
      <span v-if="report" class="doc-time">
        {{ t("doctor.generated_at") }}：{{ fmtDateTime(report.generated_at) }}
      </span>
    </n-space>

    <n-spin :show="loading">
      <n-alert v-if="report" :type="TYPE[overall]" :bordered="false" style="margin-bottom: 12px">
        {{ t("doctor.summary", { bad: report.bad, warn: report.warn, ok: report.ok }) }}
      </n-alert>

      <n-empty v-if="!report && !loading" :description="t('doctor.not_run')" />

      <div v-for="c in report?.checks ?? []" :key="c.key" class="doc-row" :data-status="c.status">
        <n-tag :type="TYPE[c.status]" size="small" :bordered="false" class="doc-badge">
          {{ t(`doctor.status_${c.status}`) }}
        </n-tag>
        <div class="doc-body">
          <div class="doc-title">{{ c.title }}</div>
          <div v-if="c.detail" class="doc-detail">{{ c.detail }}</div>
          <!-- 每個非 ok 的項目都要講「怎麼修」——只說壞了等於沒說 -->
          <div v-if="c.fix && c.status !== 'ok'" class="doc-fix">
            <span class="doc-fix-label">{{ t("doctor.fix") }}</span>
            <code>{{ c.fix }}</code>
          </div>
        </div>
      </div>

      <n-alert v-if="report" type="default" :bordered="false" style="margin-top: 14px">
        {{ t("doctor.cli_note") }}
        <code>sudo bash /opt/jt-ipam/scripts/jt-ipam.sh doctor</code>
      </n-alert>
    </n-spin>
  </n-card>
</template>

<style scoped>
.doc-bar { margin-bottom: 12px; flex-wrap: wrap; row-gap: 8px; }
.doc-time { font-size: 12px; opacity: .7; }

.doc-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--n-border-color, rgba(0, 0, 0, .06));
  margin-bottom: 8px;
}
/* 有問題的列要一眼看得出來，不能跟通過的長一樣 */
.doc-row[data-status="bad"] { border-color: rgba(208, 48, 80, .45); background: rgba(208, 48, 80, .06); }
.doc-row[data-status="warn"] { border-color: rgba(240, 160, 32, .45); background: rgba(240, 160, 32, .06); }

.doc-badge { flex: 0 0 auto; margin-top: 1px; }
.doc-body { min-width: 0; flex: 1 1 auto; }
.doc-title { font-weight: 600; }
.doc-detail { font-size: 13px; opacity: .8; margin-top: 2px; word-break: break-word; }
.doc-fix { font-size: 12px; margin-top: 6px; display: flex; gap: 6px; flex-wrap: wrap; }
.doc-fix-label { opacity: .7; flex: 0 0 auto; }
.doc-fix code, .doc-row code { word-break: break-all; }
</style>
