<template>
  <!-- 沒有資料就整張不出現（大部分位址都不在 OpenStack 上） -->
  <n-card v-if="ports.length" size="small" :bordered="true" data-testid="os-ports-card">
    <div class="ospc-title">
      <n-icon :size="16"><VirtualizationIcon /></n-icon>{{ t("openstack.ports_title") }}
    </div>
    <div class="ospc-list">
      <div v-for="p in ports" :key="p.id" class="ospc-item" data-testid="os-port">
        <n-descriptions label-placement="left" :column="isMobile ? 1 : 2" size="small" label-style="white-space:nowrap">
          <n-descriptions-item :label="t('openstack.col_port')">
            <span class="mono">{{ p.port_id }}</span>
            <n-tag v-if="p.status" size="tiny" :bordered="false" :type="p.status === 'ACTIVE' ? 'success' : 'default'"
                   style="margin-left:6px">{{ p.status }}</n-tag>
          </n-descriptions-item>
          <n-descriptions-item label="MAC"><span class="mono">{{ p.mac || "—" }}</span></n-descriptions-item>
          <n-descriptions-item :label="t('openstack.col_network')">{{ p.network_name || p.network_id || "—" }}</n-descriptions-item>
          <n-descriptions-item :label="t('openstack.project')">{{ p.project_name || p.project_id || "—" }}</n-descriptions-item>
          <n-descriptions-item :label="t('openstack.col_vm')">
            <n-button v-if="p.vm_name" text type="primary" @click="goVm(p.vm_name)">{{ p.vm_name }}</n-button>
            <span v-else>{{ p.device_owner || "—" }}</span>
          </n-descriptions-item>
          <n-descriptions-item :label="t('openstack.col_host')">{{ p.binding_host || "—" }}</n-descriptions-item>
          <n-descriptions-item :label="t('openstack.col_floating')">
            <n-space v-if="p.floating_ips?.length" :size="4">
              <n-tag v-for="f in p.floating_ips" :key="f.floating_ip_address" size="small" :bordered="false">
                {{ f.floating_ip_address }}
              </n-tag>
            </n-space>
            <span v-else>—</span>
          </n-descriptions-item>
          <n-descriptions-item :label="t('nav.openstack_admin')">{{ p.instance_name || "—" }}</n-descriptions-item>
        </n-descriptions>
      </div>
    </div>
  </n-card>
</template>

<script setup lang="ts">
/**
 * IP 詳細資料頁：這個位址掛在哪張 OpenStack 網卡（Neutron port）上。
 * 端點只給管理員（整合設定的一部分），非管理員不打這支、也不顯示。
 */
import { onMounted, ref, watch } from "vue";
import { NButton, NCard, NDescriptions, NDescriptionsItem, NIcon, NSpace, NTag } from "naive-ui";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import { useMediaQuery } from "@vueuse/core";
import { OpenStack, type OpenStackPortByIp } from "@/api/openstack";
import { useAuthStore } from "@/stores/auth";
import { VirtualizationIcon } from "@/icons";

const props = defineProps<{ addressId: string }>();
const { t } = useI18n();
const router = useRouter();
const auth = useAuthStore();
const isMobile = useMediaQuery("(max-width: 640px)");
const ports = ref<OpenStackPortByIp[]>([]);

async function load() {
  ports.value = [];
  // 後端允許全域讀取＋這筆 IP 的讀取權限（不限管理員）：稽核員這類萬用唯讀帳號也看得到
  if (!(auth.me?.is_admin || auth.me?.has_global_read) || !props.addressId) return;
  try { ports.value = await OpenStack.portsByIp(props.addressId); }
  catch { ports.value = []; /* 沒有這個整合、或讀不到：不顯示，不打擾 */ }
}

function goVm(name: string) {
  void router.push({ name: "virt_openstack", query: { vm: name } });
}

onMounted(load);
watch(() => [props.addressId, auth.me?.is_admin, auth.me?.has_global_read], () => { void load(); });
</script>

<style scoped>
.ospc-title { display: flex; align-items: center; gap: 6px; font-weight: 600; margin-bottom: 6px; }
.ospc-list { display: flex; flex-direction: column; gap: 10px; }
.ospc-item + .ospc-item { border-top: 1px solid var(--n-border-color, rgba(128, 128, 128, .2)); padding-top: 10px; }
.mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; overflow-wrap: anywhere; }
</style>
