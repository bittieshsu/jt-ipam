<script setup lang="ts">
/**
 * 跳板主機（issue #24 階段一）：主控台可以改成「後端 → 跳板 → 目標」。
 *
 * 這一頁的重點是**主機金鑰指紋**：跳板是整條路徑的中間人，沒有釘選指紋就不允許連線。
 * 所以流程刻意是兩步 —— 先「測試連線」取回指紋、人核對過，再按「信任並儲存」。
 */
import { computed, h, onMounted, ref } from "vue";
import { fmtDateTime } from "@/utils/datetime";
import { useI18n } from "vue-i18n";
import {
  NCard, NDataTable, NSpace, NButton, NTag, NIcon, NTooltip, NAlert, NModal, NForm,
  NFormItem, NInput, NInputNumber, NSwitch, NRadioGroup, NRadio, NPopconfirm, NCode,
  useMessage, type DataTableColumns,
} from "naive-ui";
import {
  listJumpHosts, createJumpHost, updateJumpHost, deleteJumpHost, testJumpHost, jumpHostUsage,
  type JumpHost, type JumpHostProbe,
} from "@/api/jumpHosts";
import {
  TerminalIcon, PlusIcon, EditIcon, DeleteIcon, RefreshIcon, TestIcon, SaveIcon, CancelIcon,
} from "@/icons";
import { autoSort } from "@/composables/useTableSort";
import { apiErrMsg } from "@/api/client";

const { t } = useI18n();
const msg = useMessage();

const rows = ref<JumpHost[]>([]);
const loading = ref(false);
const show = ref(false);
const editing = ref<JumpHost | null>(null);

const probe = ref<JumpHostProbe | null>(null);
const probeFor = ref<JumpHost | null>(null);
const probeOpen = ref(false);
const usage = ref<{ subnets: number; ips: number } | null>(null);

function blankForm() {
  return {
    name: "", host: "", port: 22, username: "root",
    auth_kind: "key" as "key" | "password",
    private_key: "", password: "",
    enabled: true, max_sessions: 10, description: "",
  };
}
const form = ref(blankForm());

async function refresh() {
  loading.value = true;
  try { rows.value = (await listJumpHosts()).items; }
  catch (e) { msg.error(apiErrMsg(e)); }
  finally { loading.value = false; }
}

function openCreate() {
  editing.value = null;
  form.value = blankForm();
  show.value = true;
}

function openEdit(r: JumpHost) {
  editing.value = r;
  form.value = {
    name: r.name, host: r.host, port: r.port, username: r.username,
    auth_kind: r.auth_kind, private_key: "", password: "",
    enabled: r.enabled, max_sessions: r.max_sessions, description: r.description ?? "",
  };
  show.value = true;
}

async function submit() {
  const payload: Record<string, unknown> = {
    name: form.value.name, host: form.value.host, port: form.value.port,
    username: form.value.username, auth_kind: form.value.auth_kind,
    enabled: form.value.enabled, max_sessions: form.value.max_sessions,
    description: form.value.description || undefined,
  };
  // 空白＝不修改（編輯既有跳板時不必重打金鑰／密碼）
  if (form.value.private_key) payload.private_key = form.value.private_key;
  if (form.value.password) payload.password = form.value.password;
  try {
    if (editing.value) await updateJumpHost(editing.value.id, payload);
    else await createJumpHost(payload as never);
    show.value = false;
    msg.success(t("common.ok"));
    await refresh();
  } catch (e) { msg.error(apiErrMsg(e)); }
}

async function test(r: JumpHost) {
  probeFor.value = r;
  probe.value = null;
  probeOpen.value = true;
  try { probe.value = await testJumpHost(r.id); }
  catch (e) { msg.error(apiErrMsg(e)); probeOpen.value = false; }
}

/** 核對過指紋之後才釘選 —— 這一步就是「我確認對面是我以為的那台機器」。 */
async function trustFingerprint() {
  if (!probeFor.value || !probe.value) return;
  try {
    await updateJumpHost(probeFor.value.id, { host_key_fingerprint: probe.value.fingerprint });
    msg.success(t("jump_hosts.trusted"));
    probeOpen.value = false;
    await refresh();
  } catch (e) { msg.error(apiErrMsg(e)); }
}

async function confirmDelete(r: JumpHost) {
  // 刪掉會讓指向它的子網路／IP 回到直連 —— 在網段重疊的站台，那等於連到別人。
  // 所以刪除前先把影響範圍數出來給人看。
  try {
    const u = await jumpHostUsage(r.id);
    usage.value = { subnets: u.subnets.length, ips: u.ips.length };
  } catch { usage.value = null; }
}

async function del(id: string) {
  try { await deleteJumpHost(id); usage.value = null; await refresh(); }
  catch (e) { msg.error(apiErrMsg(e)); }
}

function iconAction(icon: unknown, label: string, onClick: () => void, type?: string) {
  return h(NTooltip, null, {
    trigger: () => h(NButton, { size: "small", quaternary: true, type: type as never,
      onClick: (e: MouseEvent) => { e.stopPropagation(); onClick(); } },
      { icon: () => h(NIcon, null, () => h(icon as never)) }),
    default: () => label,
  });
}

const cols = computed<DataTableColumns<JumpHost>>(() => autoSort([
  { title: t("common.name"), key: "name", minWidth: 140, ellipsis: { tooltip: true } },
  {
    title: t("jump_hosts.endpoint"), key: "host", minWidth: 170, ellipsis: { tooltip: true },
    render: (r) => `${r.username}@${r.host}:${r.port}`,
  },
  {
    title: t("jump_hosts.auth"), key: "auth_kind", width: 110,
    render: (r) => h(NTag, { size: "small", bordered: false,
      type: r.has_secret ? "default" : "warning" },
      () => r.auth_kind === "key" ? t("jump_hosts.auth_key") : t("jump_hosts.auth_password")),
  },
  {
    // 沒釘選指紋的跳板一定連不了 —— 這一欄要一眼看得出來，不要等到連線失敗才知道
    title: t("jump_hosts.host_key"), key: "host_key_fingerprint", minWidth: 150,
    ellipsis: { tooltip: true },
    render: (r) => r.host_key_fingerprint
      ? h(NTag, { size: "small", type: "success", bordered: false },
        () => t("jump_hosts.pinned"))
      : h(NTag, { size: "small", type: "warning", bordered: false },
        () => t("jump_hosts.not_pinned")),
  },
  {
    title: t("common.status"), key: "enabled", width: 100,
    render: (r) => h(NTag, { type: r.enabled ? "success" : "default", size: "small" },
      () => r.enabled ? t("common.enabled") : t("common.disabled")),
  },
  { title: t("jump_hosts.max_sessions"), key: "max_sessions", width: 110 },
  {
    title: t("jump_hosts.last_ok"), key: "last_ok_at", width: 165,
    render: (r) => fmtDateTime(r.last_ok_at),
  },
  {
    title: t("cols.last_error"), key: "last_error", minWidth: 140,
    ellipsis: { tooltip: true }, render: (r) => r.last_error ?? "—",
  },
  {
    title: t("common.actions"), key: "actions", className: "col-actions", width: 150,
    render: (r) => h(NSpace, { size: 2, wrapItem: false, wrap: false }, () => [
      iconAction(EditIcon, t("common.edit"), () => openEdit(r)),
      iconAction(TestIcon, t("jump_hosts.test"), () => test(r)),
      h(NPopconfirm, { onPositiveClick: () => del(r.id), onShow: () => confirmDelete(r) }, {
        trigger: () => iconAction(DeleteIcon, t("common.delete"), () => {}, "error"),
        default: () => usage.value && (usage.value.subnets || usage.value.ips)
          ? t("jump_hosts.delete_in_use", usage.value)
          : t("common.confirm_delete"),
      }),
    ]),
  },
]));

onMounted(() => { void refresh(); });
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><TerminalIcon /></n-icon>
        <span>{{ t("jump_hosts.title") }}</span>
      </n-space>
    </template>

    <n-alert type="info" :bordered="false" style="margin-bottom: 12px">
      {{ t("jump_hosts.intro") }}
    </n-alert>

    <n-space style="margin-bottom: 12px">
      <n-button @click="refresh" :loading="loading">
        <template #icon><n-icon><RefreshIcon /></n-icon></template>
        {{ t("common.refresh") }}
      </n-button>
      <n-button type="primary" @click="openCreate">
        <template #icon><n-icon><PlusIcon /></n-icon></template>
        {{ t("common.create") }}
      </n-button>
    </n-space>

    <n-data-table :columns="cols" :data="rows" :loading="loading" :bordered="false"
                  :scroll-x="1150" />

    <!-- 新增 / 編輯 -->
    <n-modal v-model:show="show" preset="card"
             :title="editing ? t('common.edit') : `${t('common.create')} — ${t('jump_hosts.title')}`"
             style="width: 600px; max-width: calc(100vw - 32px)">
      <n-form>
        <n-form-item :label="t('common.name')"><n-input v-model:value="form.name" /></n-form-item>
        <n-form-item :label="t('jump_hosts.host')">
          <n-input v-model:value="form.host" placeholder="198.51.100.9" />
        </n-form-item>
        <n-form-item :label="t('jump_hosts.port')">
          <n-input-number v-model:value="form.port" :min="1" :max="65535" style="width: 140px" />
        </n-form-item>
        <n-form-item :label="t('common.username')">
          <n-input v-model:value="form.username" placeholder="jump" />
        </n-form-item>
        <n-form-item :label="t('jump_hosts.auth')">
          <n-radio-group v-model:value="form.auth_kind">
            <n-radio value="key">{{ t("jump_hosts.auth_key") }}</n-radio>
            <n-radio value="password">{{ t("jump_hosts.auth_password") }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item v-if="form.auth_kind === 'key'"
                     :label="editing ? t('jump_hosts.key_keep') : t('jump_hosts.key')">
          <n-input v-model:value="form.private_key" type="textarea" :rows="4"
                   :placeholder="t('jump_hosts.key_ph')" />
        </n-form-item>
        <n-form-item v-else :label="editing ? t('jump_hosts.pw_keep') : t('common.password')">
          <n-input v-model:value="form.password" type="password" show-password-on="click" />
        </n-form-item>
        <n-form-item :label="t('jump_hosts.max_sessions')">
          <div style="width: 100%">
            <n-input-number v-model:value="form.max_sessions" :min="1" :max="200"
                            style="width: 140px" />
            <div class="jh-hint">{{ t("jump_hosts.max_sessions_hint") }}</div>
          </div>
        </n-form-item>
        <n-form-item :label="t('common.enable')">
          <n-switch v-model:value="form.enabled" />
        </n-form-item>
        <n-form-item :label="t('common.description')">
          <n-input v-model:value="form.description" type="textarea" :rows="2" />
        </n-form-item>
      </n-form>
      <n-alert type="warning" :bordered="false" style="margin-bottom: 12px">
        {{ t("jump_hosts.pin_required") }}
      </n-alert>
      <n-space justify="end">
        <n-button @click="show = false">
          <template #icon><n-icon><CancelIcon /></n-icon></template>
          {{ t("common.cancel") }}
        </n-button>
        <n-button type="primary" @click="submit">
          <template #icon><n-icon><SaveIcon /></n-icon></template>
          {{ t("common.save") }}
        </n-button>
      </n-space>
    </n-modal>

    <!-- 測試連線 / 信任主機金鑰 -->
    <n-modal v-model:show="probeOpen" preset="card"
             :title="`${t('jump_hosts.test')} — ${probeFor?.name ?? ''}`"
             style="width: 600px; max-width: calc(100vw - 32px)">
      <template v-if="probe">
        <n-space vertical :size="10">
          <div><strong>{{ t("jump_hosts.endpoint") }}：</strong>{{ probe.host }}:{{ probe.port }}</div>
          <div>
            <strong>{{ t("jump_hosts.fingerprint") }}：</strong>
            <n-code :code="probe.fingerprint" word-wrap />
          </div>
          <n-alert v-if="probe.matches === true" type="success" :bordered="false">
            {{ t("jump_hosts.fp_match") }}
            <template v-if="probe.authenticated"> · {{ t("jump_hosts.auth_ok") }}</template>
          </n-alert>
          <n-alert v-else-if="probe.matches === false" type="error" :bordered="false">
            {{ t("jump_hosts.fp_mismatch") }}
          </n-alert>
          <n-alert v-else type="warning" :bordered="false">
            {{ probe.note || t("jump_hosts.fp_unpinned") }}
          </n-alert>
          <div v-if="probe.server_version" class="jh-hint">{{ probe.server_version }}</div>
        </n-space>
      </template>
      <n-space justify="end" style="margin-top: 12px">
        <n-button @click="probeOpen = false">{{ t("common.close") }}</n-button>
        <n-button v-if="probe && probe.matches !== true" type="primary" @click="trustFingerprint">
          {{ t("jump_hosts.trust") }}
        </n-button>
      </n-space>
    </n-modal>
  </n-card>
</template>

<style scoped>
.jh-hint { font-size: 11px; opacity: .7; margin-top: 4px; }
</style>
