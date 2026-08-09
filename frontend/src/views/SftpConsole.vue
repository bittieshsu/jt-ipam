<script setup lang="ts">
/** 另開視窗的全頁 SFTP 檔案瀏覽器（與 SSH 終端機同一個開法）。 */
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { useI18n } from "vue-i18n";
import { NSpin, NResult } from "naive-ui";
import { getAddress } from "@/api/addresses";
import SftpBrowser from "@/components/SftpBrowser.vue";
import type { IPAddress } from "@/types";

const route = useRoute();
const { t } = useI18n();
const addr = ref<IPAddress | null>(null);
const loading = ref(true);
const failed = ref(false);

onMounted(async () => {
  try {
    addr.value = await getAddress(String(route.params.id));
    if (addr.value?.ip) document.title = `SFTP · ${addr.value.ip}`;
  } catch {
    failed.value = true;
  } finally {
    loading.value = false;
  }
});
</script>

<template>
  <div class="sftp-page">
    <n-spin v-if="loading" />
    <n-result v-else-if="failed || !addr" status="404" :title="t('errors.not_found')" />
    <template v-else>
      <h2 class="sftp-title">SFTP · {{ addr.ip }}<span v-if="addr.hostname"> · {{ addr.hostname }}</span></h2>
      <SftpBrowser :address-id="addr.id" :host="String(addr.ip).split('/')[0]" />
    </template>
  </div>
</template>

<style scoped>
.sftp-page { padding: 16px 20px; height: 100vh; box-sizing: border-box; overflow: auto; }
.sftp-title { font-size: 16px; margin: 0 0 12px; font-weight: 600; }
</style>
