<script setup lang="ts">
/**
 * SNMP 收集目標的新增／編輯（唯一一份：SNMP 頁、裝置詳情的 SNMP 區塊、子網路頁「對閘道啟用 SNMP 收集」都用這個）。
 *
 * - 裝置：一般是伺服器端搜尋；從裝置頁進來就固定那台；從子網路的閘道進來給的是一筆 IP
 *   （那筆 IP 沒有連結裝置時，後端會以它建一台類型 router 的裝置）
 * - 憑證：預設選「預設憑證」；旁邊可以直接新增一組（用同一個憑證表單），新增完就選它
 * - 收集項目：新增時跟著裝置類型給預設值（utils/snmp.defaultCollectFlags），編輯時照原本的
 * - 收集者「自動」：即時顯示會由哪台代理、從哪個子網路判定；判定不出來就把原因講出來
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NModal, NForm, NFormItem, NInput, NInputNumber, NSelect, NCheckbox, NSwitch, NRadioGroup, NRadio,
  NButton, NIcon, NSpace, NAlert, NSpin, NTag, NCollapse, NCollapseItem, NTooltip, useMessage,
} from "naive-ui";
import { SaveIcon, CancelIcon, TestIcon, PlusIcon, SnmpIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import { listDevices } from "@/api/basic";
import {
  createSnmpTarget, updateSnmpTarget, listSnmpCredentials, listSnmpAgents, previewSnmpCollector,
  SNMP_SECTIONS, SNMP_DEFAULT_INTERVALS, SNMP_MIN_INTERVALS,
  type SnmpTarget, type SnmpCredential, type SnmpAgentInfo, type SnmpCollectorPreview, type SnmpSection,
} from "@/api/snmp";
import {
  blankTargetForm, buildTargetPayload, defaultCollectFlags, defaultCredentialId, sectionLabel,
  targetFormFrom, type TargetForm, type SnmpPresetDevice, type SnmpPresetIp,
} from "@/utils/snmp";
import { srvText } from "@/utils/wsError";
import SnmpCredentialForm from "@/components/SnmpCredentialForm.vue";
import SnmpTestResult from "@/components/SnmpTestResult.vue";

const props = defineProps<{
  show: boolean;
  target?: SnmpTarget | null;
  presetDevice?: SnmpPresetDevice | null;
  presetIp?: SnmpPresetIp | null;
}>();
const emit = defineEmits<{
  (e: "update:show", v: boolean): void;
  (e: "saved", tg: SnmpTarget): void;
}>();

const { t } = useI18n();
const msg = useMessage();

/** 目前編輯中的收集目標：編輯進來的、或這個對話框剛建立的（「儲存並測試」之後就變成編輯） */
const current = ref<SnmpTarget | null>(null);
const form = ref<TargetForm>(blankTargetForm());
const snapshot = ref("");
const saving = ref(false);
const flagsTouched = ref(false);
const creds = ref<SnmpCredential[]>([]);
const agents = ref<SnmpAgentInfo[]>([]);
const credFormShow = ref(false);
const testShow = ref(false);

const dirty = computed(() => JSON.stringify(form.value) !== snapshot.value);
const fixedDevice = computed(() => !!current.value || !!props.presetDevice || !!props.presetIp);

// ─── 裝置選單：伺服器端搜尋（裝置可能上萬台，只抓一頁會找不到） ───
const deviceOptions = ref<{ label: string; value: string }[]>([]);
const deviceTypes = ref<Record<string, string>>({});
const deviceSearching = ref(false);
let deviceSeq = 0;
async function searchDevices(q: string) {
  const seq = ++deviceSeq;
  deviceSearching.value = true;
  try {
    const r = await listDevices({ q, pageSize: 20 });
    if (seq !== deviceSeq) return;
    for (const d of r.items) deviceTypes.value[d.id] = d.type;
    const keep = deviceOptions.value.filter((o) => o.value === form.value.device_id);
    deviceOptions.value = [...keep, ...r.items.filter((d) => d.id !== form.value.device_id)
      .map((d) => ({ label: d.name, value: d.id }))];
  } catch { /* 搜尋失敗：保留原本的選項 */ }
  finally { if (seq === deviceSeq) deviceSearching.value = false; }
}

const fixedDeviceText = computed(() => {
  if (current.value) return current.value.device_name ?? current.value.device_id;
  if (props.presetDevice) return props.presetDevice.name;
  if (props.presetIp) return props.presetIp.deviceName || props.presetIp.ip;
  return "";
});

/** 收集項目預設值依據的裝置類型 */
function presetType(): string | null {
  if (props.presetDevice) return props.presetDevice.type ?? null;
  if (props.presetIp) return props.presetIp.deviceType ?? (props.presetIp.deviceId ? null : "router");
  return null;
}

watch(() => form.value.device_id, (id) => {
  if (current.value || props.presetDevice || props.presetIp || flagsTouched.value || !id) return;
  form.value.flags = defaultCollectFlags(deviceTypes.value[id]);
});
function touchFlag(s: SnmpSection, v: boolean) {
  flagsTouched.value = true;
  form.value.flags[s] = v;
}

async function loadCreds(selectId?: string) {
  try {
    creds.value = await listSnmpCredentials();
  } catch (e) { msg.error(apiErrMsg(e)); }
  if (selectId) form.value.credential_id = selectId;
  else if (!current.value && !form.value.credential_id) form.value.credential_id = defaultCredentialId(creds.value);
}


const credOptions = computed(() => creds.value.map((c) => ({
  value: c.id,
  label: [c.name, c.version, c.is_default ? t("snmp.default_tag") : "", c.customer_name ?? ""].filter(Boolean).join(" · "),
})));
const agentOptions = computed(() => agents.value.map((a) => ({
  value: a.agent_id,
  label: [a.name, a.is_local ? t("snmp.agent_local") : "", a.capable ? t("snmp.agent_capable") : t("snmp.agent_not_capable")]
    .filter(Boolean).join(" · "),
})));

function onCredCreated(c: SnmpCredential) {
  void loadCreds(c.id);
}

// ─── 收集者判定預覽 ───
const preview = ref<SnmpCollectorPreview | null>(null);
const previewLoading = ref(false);
let previewTimer: ReturnType<typeof setTimeout> | undefined;
let previewSeq = 0;
function previewArgs() {
  // 從閘道 IP 建立時，管理位址就是那筆 IP（後端會在裝置主要 IP 不是它時自動填覆寫）
  const ip = form.value.ip_override.trim() || (props.presetIp && !current.value ? props.presetIp.ip : "");
  const deviceId = form.value.device_id ?? current.value?.device_id ?? null;
  if (!ip && !deviceId) return null;
  return { device_id: deviceId, ip: ip || null, collector: form.value.collector, agent_id: form.value.agent_id };
}
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(() => void runPreview(), 300);
}
async function runPreview() {
  const args = previewArgs();
  const seq = ++previewSeq;
  if (!args || (args.collector === "agent" && !args.agent_id)) {
    preview.value = null;
    return;
  }
  previewLoading.value = true;
  try {
    const r = await previewSnmpCollector(args);
    if (seq === previewSeq) preview.value = r;
  } catch (e) {
    if (seq === previewSeq) preview.value = { ip: null, collector: args.collector, agent_id: null, agent_name: null,
      capable: false, subnet_id: null, subnet_cidr: null, error: { message: apiErrMsg(e) } };
  } finally {
    if (seq === previewSeq) previewLoading.value = false;
  }
}
watch(() => [form.value.device_id, form.value.ip_override, form.value.collector, form.value.agent_id],
  () => { if (props.show) schedulePreview(); });

const previewText = computed(() => {
  const p = preview.value;
  if (!p || p.error) return "";
  const where = p.subnet_cidr ? t("snmp.collector_via_subnet", { cidr: p.subnet_cidr }) : "";
  return t("snmp.collector_resolved", { agent: p.agent_name ?? "—", ip: p.ip ?? "—" }) + where;
});

// 打開時重設（放在預覽的宣告之後：immediate 的 watch 在宣告當下就會執行）
watch(() => props.show, async (v) => {
  if (!v) return;
  current.value = props.target ?? null;
  flagsTouched.value = false;
  if (current.value) {
    form.value = targetFormFrom(current.value);
  } else {
    form.value = blankTargetForm(presetType());
    form.value.device_id = props.presetDevice?.id ?? props.presetIp?.deviceId ?? null;
  }
  snapshot.value = JSON.stringify(form.value);
  preview.value = null;
  if (!fixedDevice.value) void searchDevices("");
  listSnmpAgents().then((r) => { agents.value = r.items; }).catch(() => { agents.value = []; });
  await loadCreds();
  snapshot.value = JSON.stringify(form.value);
  schedulePreview();
}, { immediate: true });

// ─── 存檔／測試 ───
function validate(): boolean {
  if (!current.value && !form.value.device_id && !props.presetIp) {
    msg.warning(t("snmp.pick_device"));
    return false;
  }
  if (form.value.collector === "agent" && !form.value.agent_id) {
    msg.warning(t("errors.snmp_agent_required"));
    return false;
  }
  if (!SNMP_SECTIONS.some((s) => form.value.flags[s])) {
    msg.warning(t("snmp.pick_section"));
    return false;
  }
  return true;
}

async function persist(): Promise<SnmpTarget | null> {
  if (!validate()) return null;
  saving.value = true;
  try {
    const payload = buildTargetPayload(form.value);
    let saved: SnmpTarget;
    if (current.value) {
      saved = await updateSnmpTarget(current.value.id, payload);
    } else if (props.presetIp) {
      // 一律給 IP：已連結裝置就用那台（主要 IP 不是它時後端自動填管理位址覆寫），沒有就替它建一台
      saved = await createSnmpTarget({ ...payload, ip_address_id: props.presetIp.id });
    } else {
      saved = await createSnmpTarget({ ...payload, device_id: form.value.device_id! });
    }
    current.value = saved;
    form.value = targetFormFrom(saved);
    snapshot.value = JSON.stringify(form.value);
    emit("saved", saved);
    return saved;
  } catch (e) {
    msg.error(apiErrMsg(e));
    return null;
  } finally {
    saving.value = false;
  }
}

async function save() {
  if (current.value && !dirty.value) { close(); return; }
  const s = await persist();
  if (s) {
    msg.success(t("common.saved"));
    close();
  }
}

/** 測試：還沒存檔（或有改動）就先存，測試才會用到畫面上這份設定 */
async function test() {
  if (!current.value || dirty.value) {
    const s = await persist();
    if (!s) return;
    msg.success(t("common.saved"));
  }
  testShow.value = true;
}

function close() { emit("update:show", false); }

const sectionHints = computed(() => Object.fromEntries(SNMP_SECTIONS.map((s) => [s, t(`snmp.section_${s}_hint`)])));
</script>

<template>
  <n-modal :show="show" preset="card" style="width: 680px; max-width: calc(100vw - 32px)"
           data-testid="snmp-target-form" @update:show="(v: boolean) => emit('update:show', v)">
    <template #header>
      <n-space align="center" :size="8" :wrap-item="false">
        <n-icon :size="20"><SnmpIcon /></n-icon>
        <span>{{ current ? t("snmp.target_edit_title") : t("snmp.target_create_title") }}</span>
      </n-space>
    </template>
    <n-form label-placement="top">
      <n-form-item :label="t('cols.device')">
        <div style="width: 100%">
          <n-input v-if="fixedDevice" :value="fixedDeviceText" readonly data-testid="snmp-target-device-fixed" />
          <n-select v-else v-model:value="form.device_id" :options="deviceOptions" filterable remote clearable
                    :loading="deviceSearching" :placeholder="t('snmp.pick_device')" data-testid="snmp-target-device"
                    @search="searchDevices" />
          <div v-if="presetIp && !presetIp.deviceId && !current" class="stf-hint">
            {{ t("snmp.preset_ip_new_device", { ip: presetIp.ip }) }}
          </div>
        </div>
      </n-form-item>

      <n-form-item :label="t('snmp.credential')">
        <div style="width: 100%">
          <div class="stf-row">
            <n-select v-model:value="form.credential_id" :options="credOptions" filterable
                      :placeholder="t('snmp.pick_credential')" data-testid="snmp-target-credential" />
            <n-button data-testid="snmp-target-new-cred" @click="credFormShow = true">
              <template #icon><n-icon><PlusIcon /></n-icon></template>
              {{ t("snmp.new_credential") }}
            </n-button>
          </div>
          <div v-if="!creds.length" class="stf-hint stf-warn">{{ t("snmp.no_credentials") }}</div>
          <div v-else class="stf-hint">{{ t("snmp.credential_hint") }}</div>
        </div>
      </n-form-item>

      <n-form-item :label="t('snmp.collect_items')">
        <div style="width: 100%">
          <n-space :size="14" :wrap="true">
            <n-tooltip v-for="s in SNMP_SECTIONS" :key="s" :delay="300">
              <template #trigger>
                <n-checkbox :checked="form.flags[s]" :data-testid="`snmp-flag-${s}`"
                            @update:checked="(v: boolean) => touchFlag(s, v)">
                  {{ sectionLabel(s, t) }}
                </n-checkbox>
              </template>
              {{ sectionHints[s] }}
            </n-tooltip>
          </n-space>
          <div class="stf-hint">{{ t("snmp.collect_items_hint") }}</div>
          <div v-if="form.flags.arp" style="margin-top: 8px">
            <n-checkbox v-model:checked="form.auto_create_ips" data-testid="snmp-auto-create">
              {{ t("snmp.auto_create") }}
            </n-checkbox>
            <div class="stf-hint">{{ t("snmp.auto_create_hint") }}</div>
          </div>
        </div>
      </n-form-item>

      <n-form-item :label="t('snmp.collector')">
        <div style="width: 100%">
          <n-radio-group v-model:value="form.collector" data-testid="snmp-collector">
            <n-space :size="16">
              <n-radio value="auto">{{ t("snmp.collector_auto") }}</n-radio>
              <n-radio value="agent">{{ t("snmp.collector_agent") }}</n-radio>
              <n-radio value="server">{{ t("snmp.collector_server") }}</n-radio>
            </n-space>
          </n-radio-group>
          <n-select v-if="form.collector === 'agent'" v-model:value="form.agent_id" :options="agentOptions"
                    filterable style="margin-top: 8px" :placeholder="t('snmp.pick_agent')"
                    data-testid="snmp-agent-select" />
          <div class="stf-preview" data-testid="snmp-collector-preview">
            <n-spin v-if="previewLoading" size="small" />
            <template v-else-if="preview">
              <n-alert v-if="preview.error" type="warning" :bordered="false" :show-icon="false" class="stf-alert">
                {{ srvText(preview.error) }}
              </n-alert>
              <span v-else>
                <n-tag size="tiny" type="success" :bordered="false">{{ t("snmp.collector_ok") }}</n-tag>
                {{ previewText }}
              </span>
            </template>
            <span v-else class="stf-hint">{{ t("snmp.collector_hint") }}</span>
          </div>
        </div>
      </n-form-item>

      <div class="stf-pair">
        <n-form-item :label="t('cols.port')">
          <n-input-number v-model:value="form.port" :min="1" :max="65535" style="width: 100%"
                          data-testid="snmp-port" />
        </n-form-item>
        <n-form-item :label="t('snmp.ip_override')">
          <div style="width: 100%">
            <n-input v-model:value="form.ip_override" :placeholder="t('snmp.ip_override_ph')" clearable
                     data-testid="snmp-ip-override" />
          </div>
        </n-form-item>
      </div>
      <div class="stf-hint" style="margin: -10px 0 12px">
        {{ t("snmp.ip_override_hint") }}
        <template v-if="current?.mgmt_ip"> {{ t("snmp.current_mgmt_ip", { ip: current.mgmt_ip }) }}</template>
      </div>

      <n-collapse style="margin-bottom: 12px">
        <n-collapse-item :title="t('snmp.advanced')" name="adv">
          <div class="stf-hint" style="margin-bottom: 8px">{{ t("snmp.intervals_hint") }}</div>
          <div class="stf-pair">
            <n-form-item v-for="s in SNMP_SECTIONS" :key="s" :label="sectionLabel(s, t)">
              <n-input-number v-model:value="form.intervals_min[s]" :min="SNMP_MIN_INTERVALS[s] / 60" :max="1440"
                              clearable :placeholder="String(SNMP_DEFAULT_INTERVALS[s] / 60)" style="width: 100%">
                <template #suffix>{{ t("scan_probes.mins") }}</template>
              </n-input-number>
            </n-form-item>
          </div>
          <n-form-item :label="t('common.description')">
            <n-input v-model:value="form.description" type="textarea" :rows="2" :maxlength="2048" />
          </n-form-item>
        </n-collapse-item>
      </n-collapse>

      <n-form-item :label="t('common.enable')">
        <n-switch v-model:value="form.enabled" data-testid="snmp-enabled" />
      </n-form-item>
    </n-form>

    <n-space justify="end">
      <n-button @click="close">
        <template #icon><n-icon><CancelIcon /></n-icon></template>
        {{ t("common.cancel") }}
      </n-button>
      <n-tooltip :delay="300">
        <template #trigger>
          <n-button :loading="saving" :disabled="saving" data-testid="snmp-target-test" @click="test">
            <template #icon><n-icon><TestIcon /></n-icon></template>
            {{ current ? t("common.test") : t("snmp.save_and_test") }}
          </n-button>
        </template>
        {{ t("snmp.test_saves_first") }}
      </n-tooltip>
      <n-button type="primary" :loading="saving" :disabled="saving" data-testid="snmp-target-save" @click="save">
        <template #icon><n-icon><SaveIcon /></n-icon></template>
        {{ t("common.save") }}
      </n-button>
    </n-space>

    <SnmpCredentialForm v-model:show="credFormShow" @saved="onCredCreated" />
    <SnmpTestResult v-model:show="testShow" :target="current"
                    :initial-credential-ids="form.credential_id ? [form.credential_id] : null" :auto-start="true" />
  </n-modal>
</template>

<style scoped>
.stf-hint { font-size: 12px; opacity: .7; margin-top: 4px; line-height: 1.5; }
.stf-warn { color: var(--n-warning-color, #f0a020); opacity: 1; }
.stf-row { display: flex; gap: 8px; align-items: center; }
.stf-row > :first-child { flex: 1 1 auto; min-width: 0; }
.stf-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
.stf-preview { margin-top: 8px; font-size: 13px; min-height: 22px; display: flex; align-items: center; gap: 6px; }
.stf-alert { padding: 4px 8px; font-size: 13px; }
@media (max-width: 560px) {
  .stf-pair { grid-template-columns: 1fr; }
  .stf-row { flex-wrap: wrap; }
}
</style>
