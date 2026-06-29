<script setup lang="ts">
/**
 * IP 角色標記（清單視覺化）：閘道 / DHCP 伺服器 / 在 DHCP 範圍內。
 * 旗標由後端讀取端推導（is_gateway / is_dhcp_server / dhcp_server_auto / in_dhcp_range / in_dhcp_lease）。
 * hideRange=true 時不畫「DHCP 範圍」標（給本身已有 DHCP 欄位的表格，如子網路詳情，避免重複）。
 */
import { NTag, NTooltip } from "naive-ui";
import { useI18n } from "vue-i18n";

const props = withDefaults(defineProps<{ row: any; hideRange?: boolean }>(), { hideRange: false });
const { t } = useI18n();
const r = () => props.row || {};
const isDhcpServer = () => r().is_dhcp_server || r().dhcp_server_auto;
const inRange = () => !props.hideRange && (r().in_dhcp_range || r().in_dhcp_lease);
</script>

<template>
  <span v-if="r().is_gateway || isDhcpServer() || inRange()" class="ip-roles">
    <n-tooltip v-if="r().is_gateway" :delay="200">
      <template #trigger><n-tag size="tiny" type="info" :bordered="false">{{ t("addresses.role_gateway") }}</n-tag></template>
      {{ t("addresses.role_gateway_hint") }}
    </n-tooltip>
    <n-tooltip v-if="isDhcpServer()" :delay="200">
      <template #trigger><n-tag size="tiny" type="warning" :bordered="false">{{ t("addresses.role_dhcp_server") }}</n-tag></template>
      {{ r().is_dhcp_server ? t("addresses.role_dhcp_server_manual") : t("addresses.role_dhcp_server_auto") }}
    </n-tooltip>
    <n-tooltip v-if="inRange()" :delay="200">
      <template #trigger><n-tag size="tiny" :bordered="false">{{ t("addresses.role_dhcp_range") }}</n-tag></template>
      {{ t("addresses.role_dhcp_range_hint") }}
    </n-tooltip>
  </span>
</template>

<style scoped>
.ip-roles { display: inline-flex; gap: 4px; margin-left: 6px; vertical-align: middle; }
</style>
