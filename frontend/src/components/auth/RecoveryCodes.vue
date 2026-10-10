<script setup lang="ts">
/**
 * 顯示 10 組 MFA 復原碼（只出現這一次）：複製、存成文字檔，確認存好才繼續。
 * 登入頁（首次設定）與個人設定頁（啟用、重新產生）共用。
 */
import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { NAlert, NButton, NCheckbox, NIcon, NSpace, useMessage } from "naive-ui";
import { CheckIcon, CopyIcon, DownloadIcon } from "@/icons";
import { saveBlob } from "@/utils/saveFile";

const props = defineProps<{ codes: string[]; account?: string }>();
const emit = defineEmits<{ (e: "done"): void }>();
const { t } = useI18n();
const msg = useMessage();
const saved = ref(false);

function text(): string {
  return [`jt-ipam ${t("recovery.title")}${props.account ? ` (${props.account})` : ""}`, "",
    ...props.codes, "", t("recovery.file_note")].join("\n");
}
async function copy() {
  try { await navigator.clipboard.writeText(props.codes.join("\n")); msg.success(t("common.copied_clipboard")); }
  catch { /* 不支援剪貼簿時使用者還可以下載 */ }
}
function download() {
  saveBlob("jt-ipam-recovery-codes.txt", new Blob([text()], { type: "text/plain;charset=utf-8" }),
           "text/plain;charset=utf-8", { source: "recovery-codes" });
}
</script>

<template>
  <n-space vertical :size="12" data-testid="recovery-codes">
    <n-alert type="warning" :title="t('recovery.title')">{{ t("recovery.intro") }}</n-alert>
    <ol class="codes">
      <li v-for="c in codes" :key="c"><code>{{ c }}</code></li>
    </ol>
    <n-space :wrap-item="false">
      <n-button @click="copy"><template #icon><n-icon :component="CopyIcon" /></template>{{ t("common.copy") }}</n-button>
      <n-button @click="download"><template #icon><n-icon :component="DownloadIcon" /></template>{{ t("recovery.download") }}</n-button>
    </n-space>
    <n-checkbox v-model:checked="saved" data-testid="recovery-saved">{{ t("recovery.saved_check") }}</n-checkbox>
    <n-space justify="end">
      <n-button type="primary" :disabled="!saved" data-testid="recovery-done" @click="emit('done')">
        <template #icon><n-icon :component="CheckIcon" /></template>{{ t("recovery.continue") }}
      </n-button>
    </n-space>
  </n-space>
</template>

<style scoped>
.codes { columns: 2; margin: 0; padding-left: 1.6em; font-size: 15px; }
.codes li { padding: 2px 0; break-inside: avoid; }
@media (max-width: 480px) { .codes { columns: 1; } }
</style>
