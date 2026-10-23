<template>
  <!-- 每個服務連不連得到＋看得到哪些欄位：一行一項、前面是圖示，不丟原始 JSON 給人看 -->
  <div class="osc" data-testid="os-capabilities">
    <div class="osc-head">{{ t("openstack.services") }}</div>
    <div v-for="s in services" :key="s.key" class="osc-row" :data-testid="`os-svc-${s.key}`">
      <n-icon :size="16" :color="s.state == null ? undefined : s.state.ok ? OK : BAD" class="osc-ico"
              :class="{ 'osc-muted': s.state == null }">
        <OkIcon v-if="s.state?.ok" /><FailIcon v-else-if="s.state" /><PendingIcon v-else />
      </n-icon>
      <span class="osc-label">{{ t(`openstack.svc_${s.key}`) }}</span>
      <span class="osc-val">
        <template v-if="s.state == null">{{ t("openstack.svc_not_checked") }}</template>
        <template v-else-if="s.state.ok">{{ s.state.url || t("common.ok") }}</template>
        <template v-else>{{ serviceError(s.state) }}</template>
      </span>
    </div>

    <div class="osc-head" style="margin-top: 8px">{{ t("openstack.capabilities") }}</div>
    <div v-for="c in caps" :key="c.key" class="osc-row" :data-testid="`os-cap-${c.key}`">
      <n-icon :size="16" :color="c.ok == null ? undefined : c.ok ? OK : WARN" class="osc-ico"
              :class="{ 'osc-muted': c.ok == null }">
        <OkIcon v-if="c.ok" /><WarnIcon v-else-if="c.ok === false" /><PendingIcon v-else />
      </n-icon>
      <span class="osc-label">{{ t(`openstack.cap_${c.key}`) }}</span>
      <span class="osc-val">{{ capText(c) }}</span>
    </div>
    <div v-if="capabilities?.checked_at" class="osc-foot">
      {{ t("openstack.checked_at", { at: fmtDateTime(capabilities.checked_at) }) }}
    </div>
  </div>
</template>

<script setup lang="ts">
/** OpenStack 的測試結果／能力清單（測試視窗與每個連線的頁籤共用） */
import { computed } from "vue";
import { NIcon } from "naive-ui";
import { useI18n } from "vue-i18n";
import {
  capabilityItems, serviceRows, type CapabilityItem, type OpenStackCapabilities, type OpenStackProduct,
  type OpenStackServiceState,
} from "@/api/openstack";
import { FailIcon, OkIcon, PendingIcon, WarnIcon } from "@/icons";
import { fmtDateTime } from "@/utils/datetime";

const props = defineProps<{ capabilities: OpenStackCapabilities | null | undefined; product: OpenStackProduct }>();
const { t, te } = useI18n();
const OK = "#18a058";
const BAD = "#d03050";
const WARN = "#f0a020";

const services = computed(() => serviceRows(props.capabilities, props.product));
const caps = computed(() => capabilityItems(props.capabilities));

function serviceError(s: OpenStackServiceState): string {
  if (s.code && te(`errors.${s.code}`)) return t(`errors.${s.code}`, { reason: s.message ?? "", ...(s.params ?? {}) });
  return s.message || t("openstack.test_failed");
}

function capText(c: CapabilityItem): string {
  if (c.ok == null) return t("openstack.cap_unknown");
  if (c.value != null) return c.value;
  if (c.ok) return t("common.yes");
  return te(`openstack.cap_${c.key}_no`) ? t(`openstack.cap_${c.key}_no`) : t("common.no");
}
</script>

<style scoped>
.osc { font-size: 13px; }
.osc-head { font-weight: 600; margin-bottom: 4px; }
.osc-row { display: flex; align-items: flex-start; gap: 8px; padding: 3px 0; }
.osc-ico { flex: none; margin-top: 2px; }
.osc-muted { opacity: .45; }
.osc-label { flex: none; width: 170px; }
.osc-val { flex: 1; min-width: 0; opacity: .8; overflow-wrap: anywhere; }
.osc-foot { margin-top: 6px; font-size: 12px; opacity: .6; }
@media (max-width: 640px) {
  .osc-label { width: 120px; }
}
</style>
