<template>
  <div class="peek">
    <div class="peek-ip">{{ ip }}</div>
    <n-spin v-if="!data" :size="14" style="margin: 6px 0" />
    <div v-else-if="data.missing" class="peek-missing">{{ t("ip_peek.not_found") }}</div>
    <div v-else class="peek-rows">
      <div v-if="data.hostname" class="peek-row">
        <span class="k">{{ t("addresses.hostname") }}</span><span class="v">{{ data.hostname }}</span>
      </div>
      <div class="peek-row">
        <span class="k">{{ t("common.status") }}</span>
        <span class="v">
          <n-tag size="tiny" :bordered="false" :type="data.state === 'active' ? 'success' : 'default'">
            {{ data.state }}
          </n-tag>
          <n-tag v-if="data.effective_status" size="tiny" :bordered="false"
                 :type="String(data.effective_status).startsWith('online') ? 'success' : 'warning'"
                 style="margin-left:4px">
            {{ data.effective_status }}
          </n-tag>
        </span>
      </div>
      <div v-if="data.mac" class="peek-row">
        <span class="k">MAC</span><span class="v mono">{{ data.mac }}</span>
      </div>
      <div v-if="data.device_name" class="peek-row">
        <span class="k">{{ t("nav.devices") }}</span><span class="v">{{ data.device_name }}</span>
      </div>
      <div v-if="lastSeen" class="peek-row">
        <span class="k">{{ t("addresses.last_seen") }}</span><span class="v">{{ lastSeen }}</span>
      </div>
      <div v-if="roles.length" class="peek-row">
        <span class="k">{{ t("ip_peek.role") }}</span>
        <span class="v">
          <n-tag v-for="r in roles" :key="r" size="tiny" :bordered="false" type="info"
                 style="margin-right:4px">{{ r }}</n-tag>
        </span>
      </div>
      <div v-if="data.description" class="peek-row">
        <span class="k">{{ t("common.description") }}</span>
        <span class="v">{{ data.description }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
// 依據資料裡的 IP 滑過去就看得到重點，不用先點進去才知道那是什麼機器。
// 查證的成本越低，使用者才越可能真的去查 —— 這整頁的重點就是「不要照單全收」。
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { NSpin, NTag } from "naive-ui";
import { fmtDateTime } from "@/utils/datetime";

export interface IpPeekData {
  missing?: boolean;
  is_gateway?: boolean | null;
  is_dhcp_server?: boolean | null;
  in_dhcp_range?: boolean | null;
  hostname?: string | null;
  state?: string | null;
  effective_status?: string | null;
  mac?: string | null;
  device_name?: string | null;
  description?: string | null;
  last_seen?: string | null;
}

const props = defineProps<{ ip: string; data?: IpPeekData | null }>();
const { t } = useI18n();

const lastSeen = computed(() => (props.data?.last_seen ? fmtDateTime(props.data.last_seen) : ""));

// 角色標籤：查證一筆「這台在發 DHCP」的發現時，這是最想先看到的一行
const roles = computed(() => {
  const d = props.data;
  if (!d) return [];
  const out: string[] = [];
  if (d.is_gateway) out.push(t("addresses.role_gateway"));
  if (d.is_dhcp_server) out.push(t("addresses.role_dhcp_server"));
  if (d.in_dhcp_range) out.push(t("addresses.role_dhcp_range"));
  return out;
});
</script>

<style scoped>
.peek { min-width: 220px; max-width: 340px; font-size: 12px; }
.peek-ip {
  font-family: var(--font-mono, monospace); font-weight: 600; font-size: 13px;
  margin-bottom: 6px;
}
.peek-missing { color: var(--n-text-color-disabled); }
.peek-rows { display: flex; flex-direction: column; gap: 3px; }
.peek-row { display: flex; gap: 8px; align-items: baseline; }
.peek-row .k { flex: 0 0 auto; min-width: 62px; color: var(--n-text-color-disabled); }
.peek-row .v { flex: 1 1 auto; min-width: 0; word-break: break-word; }
.mono { font-family: var(--font-mono, monospace); }
</style>
