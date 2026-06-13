<script setup lang="ts">
import { computed, h, onMounted, ref } from "vue";
import { fmtDateTime } from "@/utils/datetime";
import { useI18n } from "vue-i18n";
import {
  NCard, NTabs, NTabPane, NDataTable, NSpace, NButton, NIcon, NTag, NModal, NForm,
  NFormItem, NInput, NInputNumber, NDynamicTags, NSelect, NPopconfirm, NAlert,
  NCheckbox, useMessage, type DataTableColumns,
} from "naive-ui";
import { PlusIcon, RefreshIcon, CopyIcon, LockIcon, InfoIcon } from "@/icons";
import {
  listCertificates, createCertificate, deleteCertificate, uploadVersion, generateSelfSigned,
  listCertAgents, createCertAgent, rotateCertAgentKey, deleteCertAgent,
  type Certificate, type CertAgent,
} from "@/api/certificates";

const { t } = useI18n();
const msg = useMessage();

const certs = ref<Certificate[]>([]);
const agents = ref<CertAgent[]>([]);
const loading = ref(false);

async function loadCerts() {
  loading.value = true;
  try { certs.value = (await listCertificates()).items; }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
  finally { loading.value = false; }
}
async function loadAgents() {
  try { agents.value = (await listCertAgents()).items; }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}
onMounted(() => { loadCerts(); loadAgents(); });

// ── 到期狀態著色 ──
function expiryTag(c: Certificate) {
  if (c.current_days_remaining === null || c.current_not_after === null)
    return h(NTag, { size: "small" }, () => t("certs.no_version"));
  const d = c.current_days_remaining;
  const type = d < 0 ? "error" : d <= 21 ? "warning" : "success";
  const label = d < 0 ? t("certs.expired") : t("certs.days_left", { n: d });
  return h(NTag, { size: "small", type }, () => `${fmtDateTime(c.current_not_after!).slice(0, 10)} · ${label}`);
}

// ── 新增憑證 ──
const showNew = ref(false);
const newForm = ref({ name: "", description: "" });
async function doCreate() {
  if (!newForm.value.name.trim()) { msg.warning(t("certs.name_required")); return; }
  try {
    await createCertificate({ name: newForm.value.name.trim(), description: newForm.value.description || null });
    showNew.value = false; newForm.value = { name: "", description: "" };
    await loadCerts(); msg.success(t("common.saved"));
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}

// ── 上傳新版 ──
const showUpload = ref(false);
const uploadTarget = ref<Certificate | null>(null);
const upCert = ref<File | null>(null);
const upKey = ref<File | null>(null);
const upChain = ref<File | null>(null);
const upAllowExpired = ref(false);
const upBusy = ref(false);
function openUpload(c: Certificate) {
  uploadTarget.value = c; upCert.value = upKey.value = upChain.value = null;
  upAllowExpired.value = false; showUpload.value = true;
}
function pick(ev: Event, slot: "cert" | "key" | "chain") {
  const f = (ev.target as HTMLInputElement).files?.[0] ?? null;
  if (slot === "cert") upCert.value = f; else if (slot === "key") upKey.value = f; else upChain.value = f;
}
async function doUpload() {
  if (!uploadTarget.value || !upCert.value || !upKey.value) { msg.warning(t("certs.need_cert_key")); return; }
  upBusy.value = true;
  try {
    await uploadVersion(uploadTarget.value.id, { cert: upCert.value, key: upKey.value, chain: upChain.value },
      upAllowExpired.value);
    showUpload.value = false; await loadCerts(); msg.success(t("certs.uploaded"));
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
  finally { upBusy.value = false; }
}

// ── 產生自簽 ──
const showSelf = ref(false);
const selfTarget = ref<Certificate | null>(null);
const selfForm = ref({ common_name: "", sans: [] as string[], days: 365 });
function openSelf(c: Certificate) {
  selfTarget.value = c; selfForm.value = { common_name: "", sans: [], days: 365 }; showSelf.value = true;
}
async function doSelf() {
  if (!selfTarget.value || !selfForm.value.common_name.trim()) { msg.warning(t("certs.cn_required")); return; }
  try {
    await generateSelfSigned(selfTarget.value.id, {
      common_name: selfForm.value.common_name.trim(), sans: selfForm.value.sans, days: selfForm.value.days });
    showSelf.value = false; await loadCerts(); msg.success(t("certs.self_signed_done"));
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}

async function removeCert(c: Certificate) {
  try { await deleteCertificate(c.id); await loadCerts(); msg.success(t("common.deleted")); }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}

// ── 派送代理 ──
const showNewAgent = ref(false);
const agentForm = ref({ name: "", description: "", scope_cert_ids: [] as string[] });
const newKey = ref<string | null>(null);
const certOptions = computed(() => certs.value.map(c => ({ label: c.name, value: c.id })));
async function doCreateAgent() {
  if (!agentForm.value.name.trim()) { msg.warning(t("certs.name_required")); return; }
  try {
    const r = await createCertAgent({ name: agentForm.value.name.trim(),
      description: agentForm.value.description || null, scope_cert_ids: agentForm.value.scope_cert_ids });
    newKey.value = r.enroll_key; await loadAgents();
    agentForm.value = { name: "", description: "", scope_cert_ids: [] };
  } catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}
async function doRotate(a: CertAgent) {
  try { const r = await rotateCertAgentKey(a.id); newKey.value = r.enroll_key; showNewAgent.value = true; }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}
async function removeAgent(a: CertAgent) {
  try { await deleteCertAgent(a.id); await loadAgents(); msg.success(t("common.deleted")); }
  catch (e: any) { msg.error(e?.response?.data?.detail ?? t("errors.server")); }
}
function copy(s: string) { navigator.clipboard?.writeText(s); msg.success(t("common.copied")); }

// ── 安裝說明 ──
const showHelp = ref(false);
const serverOrigin = window.location.origin;
const installerOneLiner = computed(() =>
  `curl -fsSLk ${serverOrigin}/api/v1/cert-agents/installer.sh | sudo `
  + `JT_IPAM_URL=${serverOrigin} JT_IPAM_AGENT_KEY=${newKey.value || "<建立代理時的-KEY>"} JT_IPAM_INSECURE=1 bash`);
const configExample = `deployments:
  - cert: wildcard-example-com
    profile: nginx
  - cert: mail-cert
    profile: pmg`;

const certCols = computed<DataTableColumns<Certificate>>(() => [
  { title: t("cols.name"), key: "name" },
  { title: t("certs.domains"), key: "domains",
    render: (c) => h(NSpace, { size: 4 }, () => (c.domains ?? []).slice(0, 4).map(d =>
      h(NTag, { size: "small" }, () => d))) },
  { title: t("certs.expiry"), key: "exp", render: expiryTag },
  { title: t("certs.versions"), key: "version_count", width: 80 },
  { title: t("cols.actions"), key: "actions", width: 280, render: (c) => h(NSpace, { size: 6 }, () => [
    h(NButton, { size: "small", onClick: () => openUpload(c) }, () => t("certs.upload_version")),
    h(NButton, { size: "small", onClick: () => openSelf(c) }, () => t("certs.self_signed")),
    h(NPopconfirm, { onPositiveClick: () => removeCert(c) }, {
      trigger: () => h(NButton, { size: "small", tertiary: true, type: "error" }, () => t("common.delete")),
      default: () => t("certs.delete_confirm") }),
  ]) },
]);

const agentCols = computed<DataTableColumns<CertAgent>>(() => [
  { title: t("cols.name"), key: "name" },
  { title: t("cols.enabled"), key: "enabled", width: 70,
    render: (a) => h(NTag, { size: "small", type: a.enabled ? "success" : "default" },
      () => a.enabled ? "✓" : "—") },
  { title: t("certs.scope"), key: "scope", width: 90,
    render: (a) => `${(a.scope_cert_ids ?? []).length} ${t("certs.certs_unit")}` },
  { title: t("cols.version"), key: "agent_version", width: 90,
    render: (a) => a.agent_version ?? "—" },
  { title: t("cols.last_report"), key: "last_seen_at",
    render: (a) => a.last_seen_at ? fmtDateTime(a.last_seen_at) : "—" },
  { title: t("certs.deployed"), key: "reported",
    render: (a) => `${(a.reported ?? []).filter(d => (d as any).status === "ok").length} / ${(a.reported ?? []).length}` },
  { title: t("cols.actions"), key: "actions", width: 200, render: (a) => h(NSpace, { size: 6 }, () => [
    h(NButton, { size: "small", onClick: () => doRotate(a) }, () => t("certs.rotate_key")),
    h(NPopconfirm, { onPositiveClick: () => removeAgent(a) }, {
      trigger: () => h(NButton, { size: "small", tertiary: true, type: "error" }, () => t("common.delete")),
      default: () => t("common.delete") + "?" }),
  ]) },
]);
</script>

<template>
  <n-card :bordered="false">
    <template #header>
      <n-space align="center" :size="8">
        <n-icon :component="LockIcon" /> {{ t("nav.certificates") }}
      </n-space>
    </template>

    <n-tabs type="line" animated>
      <!-- 憑證 -->
      <n-tab-pane name="certs" :tab="t('certs.tab_certs')">
        <n-space justify="space-between" style="margin-bottom: 10px">
          <n-button type="primary" size="small" @click="showNew = true">
            <template #icon><n-icon :component="PlusIcon" /></template>{{ t("certs.new") }}
          </n-button>
          <n-button size="small" quaternary @click="loadCerts">
            <template #icon><n-icon :component="RefreshIcon" /></template>{{ t("common.refresh") }}
          </n-button>
        </n-space>
        <n-data-table :columns="certCols" :data="certs" :loading="loading" size="small"
                      :row-key="(r:Certificate) => r.id" />
      </n-tab-pane>

      <!-- 派送代理 -->
      <n-tab-pane name="agents" :tab="t('certs.tab_agents')">
        <n-space justify="space-between" style="margin-bottom: 10px">
          <n-space :size="8">
            <n-button type="primary" size="small" @click="newKey = null; showNewAgent = true">
              <template #icon><n-icon :component="PlusIcon" /></template>{{ t("certs.new_agent") }}
            </n-button>
            <n-button size="small" @click="showHelp = true">
              <template #icon><n-icon :component="InfoIcon" /></template>{{ t("certHelp.button") }}
            </n-button>
          </n-space>
          <n-button size="small" quaternary @click="loadAgents">
            <template #icon><n-icon :component="RefreshIcon" /></template>{{ t("common.refresh") }}
          </n-button>
        </n-space>
        <n-data-table :columns="agentCols" :data="agents" size="small" :row-key="(r:CertAgent) => r.id" />
      </n-tab-pane>
    </n-tabs>
  </n-card>

  <!-- 新增憑證 -->
  <n-modal v-model:show="showNew" preset="card" :title="t('certs.new')" style="max-width: 460px">
    <n-form>
      <n-form-item :label="t('cols.name')">
        <n-input v-model:value="newForm.name" placeholder="wildcard-example-com" />
      </n-form-item>
      <n-form-item :label="t('common.description')">
        <n-input v-model:value="newForm.description" type="textarea" :rows="2" />
      </n-form-item>
    </n-form>
    <template #footer>
      <n-button type="primary" @click="doCreate">{{ t("common.save") }}</n-button>
    </template>
  </n-modal>

  <!-- 上傳新版 -->
  <n-modal v-model:show="showUpload" preset="card"
           :title="`${t('certs.upload_version')} — ${uploadTarget?.name}`" style="max-width: 520px">
    <n-form>
      <n-form-item :label="t('certs.crt_file')">
        <input type="file" accept=".crt,.pem,.cer" @change="(e) => pick(e, 'cert')" />
      </n-form-item>
      <n-form-item :label="t('certs.key_file')">
        <input type="file" accept=".key,.pem" @change="(e) => pick(e, 'key')" />
      </n-form-item>
      <n-form-item :label="t('certs.chain_file')">
        <input type="file" accept=".crt,.pem,.cer" @change="(e) => pick(e, 'chain')" />
      </n-form-item>
      <n-checkbox v-model:checked="upAllowExpired">{{ t("certs.allow_expired") }}</n-checkbox>
    </n-form>
    <template #footer>
      <n-button type="primary" :loading="upBusy" @click="doUpload">{{ t("certs.upload") }}</n-button>
    </template>
  </n-modal>

  <!-- 產生自簽 -->
  <n-modal v-model:show="showSelf" preset="card"
           :title="`${t('certs.self_signed')} — ${selfTarget?.name}`" style="max-width: 480px">
    <n-form>
      <n-form-item :label="t('certs.common_name')">
        <n-input v-model:value="selfForm.common_name" placeholder="host.lan" />
      </n-form-item>
      <n-form-item :label="t('certs.sans')">
        <n-dynamic-tags v-model:value="selfForm.sans" />
      </n-form-item>
      <n-form-item :label="t('certs.days')">
        <n-input-number v-model:value="selfForm.days" :min="1" :max="3650" />
      </n-form-item>
    </n-form>
    <template #footer>
      <n-button type="primary" @click="doSelf">{{ t("certs.generate") }}</n-button>
    </template>
  </n-modal>

  <!-- 新增代理 / 顯示 key -->
  <n-modal v-model:show="showNewAgent" preset="card" :title="t('certs.new_agent')" style="max-width: 540px">
    <n-alert v-if="newKey" type="success" :title="t('certs.key_once')" :bordered="false" style="margin-bottom: 12px">
      <n-space align="center">
        <code style="word-break: break-all">{{ newKey }}</code>
        <n-button size="tiny" @click="copy(newKey!)">
          <template #icon><n-icon :component="CopyIcon" /></template>
        </n-button>
      </n-space>
    </n-alert>
    <n-form v-if="!newKey">
      <n-form-item :label="t('cols.name')">
        <n-input v-model:value="agentForm.name" placeholder="web01" />
      </n-form-item>
      <n-form-item :label="t('certs.scope_certs')">
        <n-select v-model:value="agentForm.scope_cert_ids" multiple filterable :options="certOptions"
                  :placeholder="t('certs.scope_hint')" />
      </n-form-item>
    </n-form>
    <template #footer>
      <n-button v-if="!newKey" type="primary" @click="doCreateAgent">{{ t("common.save") }}</n-button>
      <n-button v-else @click="showNewAgent = false">{{ t("common.close") }}</n-button>
    </template>
  </n-modal>

  <!-- 安裝說明 -->
  <n-modal v-model:show="showHelp" preset="card" :title="t('certHelp.title')"
           style="width: 720px; max-width: 92vw">
    <n-alert type="info" :bordered="false" :show-icon="true" style="margin-bottom: 14px">
      {{ t("certHelp.intro") }}
    </n-alert>
    <ol style="padding-left: 18px; margin: 0 0 12px; line-height: 1.9">
      <li>{{ t("certHelp.step1") }}</li>
      <li>{{ t("certHelp.step2") }}</li>
      <li>{{ t("certHelp.step3") }}</li>
    </ol>

    <div style="font-weight: 600; margin: 10px 0 4px">{{ t("certHelp.oneliner_label") }}</div>
    <n-space align="center" :wrap="false">
      <code style="flex: 1; word-break: break-all; background: var(--n-color-embedded); padding: 8px; border-radius: 4px; font-size: 12px">{{ installerOneLiner }}</code>
      <n-button size="small" secondary @click="copy(installerOneLiner)">
        <template #icon><n-icon :component="CopyIcon" /></template>{{ t("certHelp.copy") }}
      </n-button>
    </n-space>
    <div style="font-size: 12px; opacity: .7; margin-top: 6px">{{ t("certHelp.distros") }}</div>

    <div style="font-weight: 600; margin: 16px 0 4px">{{ t("certHelp.config_label") }}</div>
    <pre style="background: var(--n-color-embedded); padding: 10px; border-radius: 4px; font-size: 12px; white-space: pre-wrap">{{ configExample }}</pre>
    <div style="font-size: 12px; opacity: .8; margin-top: 6px">{{ t("certHelp.profiles") }}</div>
    <div style="font-size: 12px; opacity: .8; margin-top: 8px">{{ t("certHelp.dryrun") }}</div>
  </n-modal>
</template>
