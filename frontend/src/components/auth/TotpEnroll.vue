<script setup lang="ts">
/**
 * 啟用 TOTP 的兩步（掃 QR → 輸入第一組驗證碼）。
 *
 * 個人設定頁與登入頁（管理員要求 MFA、還沒設定的人）共用同一個元件 —— 同一件事在兩個地方的
 * 畫面與欄位要一模一樣。金鑰由呼叫端向後端要（兩邊的端點不同），這裡只負責顯示與收驗證碼。
 */
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { NButton, NCode, NIcon, NInput, NSpace } from "naive-ui";
import QRCode from "qrcode";
import { CancelIcon, CheckIcon } from "@/icons";

const props = defineProps<{
  enrollment: { secret: string; otpauth_uri: string };
  busy?: boolean;
  /** 登入頁沒有「取消」（取消＝回到帳密那一步，由登入頁自己處理） */
  cancellable?: boolean;
}>();
const emit = defineEmits<{ (e: "confirm", code: string): void; (e: "cancel"): void }>();
const { t } = useI18n();
const code = ref("");
const qrSvg = ref("");

watch(() => props.enrollment.otpauth_uri, async (uri) => {
  qrSvg.value = uri
    ? await QRCode.toString(uri, { type: "svg", margin: 1, width: 200, errorCorrectionLevel: "M" })
    : "";
}, { immediate: true });

function submit() {
  const c = code.value.trim();
  if (/^\d{6}$/.test(c)) emit("confirm", c);
}
</script>

<template>
  <n-space vertical :size="12" data-testid="totp-enroll">
    <strong>{{ t("settings.security.step1") }}</strong>
    <div class="qr" v-html="qrSvg"></div>
    <details>
      <summary>{{ t("settings.security.cannot_scan") }}</summary>
      <n-code :code="enrollment.otpauth_uri" language="plain" />
      <p class="secret">Secret：<code data-testid="totp-secret">{{ enrollment.secret }}</code></p>
    </details>
    <strong>{{ t("settings.security.step2") }}</strong>
    <n-space :wrap-item="false">
      <n-input v-model:value="code" placeholder="123456" maxlength="6" style="width: 160px"
               :input-props="{ inputmode: 'numeric', autocomplete: 'one-time-code' }"
               data-testid="totp-enroll-code" @keyup.enter="submit" />
      <n-button type="primary" :loading="busy" :disabled="!/^\d{6}$/.test(code.trim())"
                data-testid="totp-enroll-confirm" @click="submit">
        <template #icon><n-icon :component="CheckIcon" /></template>{{ t("settings.security.confirm_enable") }}
      </n-button>
      <n-button v-if="cancellable" type="error" ghost @click="emit('cancel')">
        <template #icon><n-icon :component="CancelIcon" /></template>{{ t("common.cancel") }}
      </n-button>
    </n-space>
  </n-space>
</template>

<style scoped>
.qr { background: white; padding: 8px; border-radius: 4px; display: inline-block; }
.qr :deep(svg) { display: block; }
.secret { font-size: 12px; opacity: 0.7; word-break: break-all; }
</style>
