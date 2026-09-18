<script setup lang="ts">
/**
 * OCS Inventory NG 整合 —— 端點資產盤點來源（唯讀拉取，不會更動 OCS）。
 *
 * 這一頁有兩件事跟其他整合不同，都直接呈現在畫面上：
 * 1. **帳密選用** —— OCS 的 REST 預設無驗證。連線診斷會主動測「沒帶憑證連不連得上」，
 *    連得上就跳一條紅色警告：這套 OCS 對任何能到達它的人都是開放的。
 * 2. **軟體區段預設關** —— 每台會從 ~2 KB 膨脹到 ~80 KB；先看規模再決定要不要開。
 */
import { computed, h, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import {
  NCard, NDataTable, NSpace, NButton, NTag, NIcon, NAlert, NModal, NForm,
  NFormItem, NInput, NInputNumber, NSwitch, NPopconfirm,
  useMessage, type DataTableColumns,
} from "naive-ui";
import {
  listOcs, createOcs, updateOcs, deleteOcs, testOcs, syncOcs,
  type OcsServer, type OcsDiagnosis,
} from "@/api/ocs";
import {
  PlusIcon, EditIcon, DeleteIcon, RefreshIcon, SyncIcon, TestIcon,
  SaveIcon, CancelIcon,
} from "@/icons";
import { fmtDateTime } from "@/utils/datetime";
import { apiErrMsg } from "@/api/client";

const { t } = useI18n();
const msg = useMessage();

const rows = ref<OcsServer[]>([]);
const loading = ref(false);
const show = ref(false);
const editing = ref<OcsServer | null>(null);
const busy = ref<string>("");

const diag = ref<OcsDiagnosis | null>(null);
const diagOpen = ref(false);

function blankForm() {
  return {
    name: "", base_url: "", api_username: "", api_password: "",
    enabled: true, verify_tls: true,
    sync_networks: true, sync_bios: true, sync_software: false,
    sync_interval_seconds: 3600, stale_after_days: 30,
    clear_credentials: false,
  };
}
const form = ref(blankForm());

async function load() {
  loading.value = true;
  try {
    rows.value = (await listOcs()).items;
  } catch (e) {
    msg.error(apiErrMsg(e));
  } finally {
    loading.value = false;
  }
}
onMounted(load);

function openCreate() {
  editing.value = null;
  form.value = blankForm();
  show.value = true;
}
function openEdit(row: OcsServer) {
  editing.value = row;
  form.value = {
    ...blankForm(),
    name: row.name, base_url: row.base_url ?? "",
    api_username: row.api_username ?? "", api_password: "",
    enabled: row.enabled, verify_tls: row.verify_tls,
    sync_networks: row.sync_networks, sync_bios: row.sync_bios,
    sync_software: row.sync_software,
    sync_interval_seconds: row.sync_interval_seconds,
    stale_after_days: row.stale_after_days,
  };
  show.value = true;
}

async function save() {
  const f = form.value;
  if (!f.name.trim() || !f.base_url.trim()) {
    msg.warning(t("ocs.name_url_required"));
    return;
  }
  try {
    const payload: Record<string, unknown> = {
      name: f.name.trim(), base_url: f.base_url.trim(),
      enabled: f.enabled, verify_tls: f.verify_tls,
      api_username: f.api_username.trim() || null,
      sync_networks: f.sync_networks, sync_bios: f.sync_bios,
      sync_software: f.sync_software,
      sync_interval_seconds: f.sync_interval_seconds,
      stale_after_days: f.stale_after_days,
    };
    if (f.api_password) payload.api_password = f.api_password;
    if (editing.value) {
      await updateOcs(editing.value.id, payload);
    } else {
      await createOcs(payload as never);
    }
    show.value = false;
    msg.success(t("common.saved"));
    await load();
  } catch (e) {
    msg.error(apiErrMsg(e));
  }
}

async function remove(row: OcsServer) {
  try {
    await deleteOcs(row.id);
    msg.success(t("common.deleted"));
    await load();
  } catch (e) {
    msg.error(apiErrMsg(e));
  }
}

async function test(row: OcsServer) {
  busy.value = row.id;
  try {
    diag.value = await testOcs(row.id);
    diagOpen.value = true;
  } catch (e) {
    msg.error(apiErrMsg(e));
  } finally {
    busy.value = "";
  }
}

async function sync(row: OcsServer) {
  busy.value = row.id;
  try {
    await syncOcs(row.id);
    msg.success(t("ocs.sync_started"));
    setTimeout(load, 1500);
  } catch (e) {
    msg.error(apiErrMsg(e));
  } finally {
    busy.value = "";
  }
}

const columns = computed<DataTableColumns<OcsServer>>(() => [
  { title: t("cols.name"), key: "name" },
  { title: "URL", key: "base_url" },
  {
    title: t("cols.status"), key: "enabled",
    render: (r) => h(NTag, { type: r.enabled ? "success" : "default", size: "small" },
      { default: () => (r.enabled ? t("common.enabled") : t("common.disabled")) }),
  },
  {
    title: t("ocs.version"), key: "detected_version",
    render: (r) => r.detected_version ?? "—",
  },
  {
    title: t("cols.last_sync"), key: "last_sync_at",
    render: (r) => (r.last_sync_at ? fmtDateTime(r.last_sync_at) : "—"),
  },
  {
    title: t("cols.last_error"), key: "last_error",
    render: (r) => (r.last_error
      ? h(NTag, { type: "error", size: "small" }, { default: () => r.last_error })
      : "—"),
  },
  {
    title: t("cols.actions"), key: "actions",
    render: (r) => h(NSpace, { size: 4 }, {
      default: () => [
        h(NButton, { size: "tiny", loading: busy.value === r.id, onClick: () => test(r) },
          { icon: () => h(NIcon, null, { default: () => h(TestIcon) }),
            default: () => t("ocs.test") }),
        h(NButton, { size: "tiny", loading: busy.value === r.id, onClick: () => sync(r) },
          { icon: () => h(NIcon, null, { default: () => h(SyncIcon) }),
            default: () => t("ocs.sync_now") }),
        h(NButton, { size: "tiny", onClick: () => openEdit(r) },
          { icon: () => h(NIcon, null, { default: () => h(EditIcon) }),
            default: () => t("common.edit") }),
        h(NPopconfirm, { onPositiveClick: () => remove(r) }, {
          trigger: () => h(NButton, { size: "tiny", type: "error" },
            { icon: () => h(NIcon, null, { default: () => h(DeleteIcon) }) }),
          default: () => t("ocs.confirm_delete", { name: r.name }),
        }),
      ],
    }),
  },
]);

</script>

<template>
  <NCard :title="t('ocs.title')">
    <template #header-extra>
      <NSpace>
        <NButton size="small" @click="load">
          <template #icon><NIcon><RefreshIcon /></NIcon></template>
          {{ t("common.refresh") }}
        </NButton>
        <NButton size="small" type="primary" @click="openCreate">
          <template #icon><NIcon><PlusIcon /></NIcon></template>
          {{ t("ocs.add") }}
        </NButton>
      </NSpace>
    </template>

    <NAlert type="info" :show-icon="true" style="margin-bottom: 12px">
      {{ t("ocs.intro") }}
    </NAlert>

    <NDataTable :columns="columns" :data="rows" :loading="loading" :bordered="false"
                :row-key="(r: OcsServer) => r.id" size="small" />
  </NCard>

  <!-- 新增／編輯 -->
  <NModal v-model:show="show" preset="card" style="max-width: 620px"
          :title="editing ? t('ocs.edit_title') : t('ocs.add')">
    <NForm label-placement="left" :label-width="140" size="small">
      <NFormItem :label="t('cols.name')">
        <NInput v-model:value="form.name" :placeholder="t('ocs.name_ph')" />
      </NFormItem>
      <NFormItem label="URL">
        <NInput v-model:value="form.base_url" placeholder="https://ocs.example.com" />
      </NFormItem>
      <NFormItem :label="t('ocs.username')">
        <NInput v-model:value="form.api_username" :placeholder="t('ocs.auth_optional')" />
      </NFormItem>
      <NFormItem :label="t('ocs.password')">
        <NInput v-model:value="form.api_password" type="password" show-password-on="click"
                :placeholder="editing?.has_password ? t('ocs.password_kept') : t('ocs.auth_optional')" />
      </NFormItem>
      <NFormItem :label="t('ocs.verify_tls')">
        <NSwitch v-model:value="form.verify_tls" />
      </NFormItem>
      <NFormItem :label="t('common.enabled')">
        <NSwitch v-model:value="form.enabled" />
      </NFormItem>
      <NFormItem :label="t('ocs.sync_networks')">
        <NSwitch v-model:value="form.sync_networks" />
      </NFormItem>
      <NFormItem :label="t('ocs.sync_bios')">
        <NSwitch v-model:value="form.sync_bios" />
      </NFormItem>
      <NFormItem :label="t('ocs.sync_software')">
        <NSpace vertical :size="2">
          <NSwitch v-model:value="form.sync_software" />
          <span style="font-size: 12px; opacity: .7">{{ t("ocs.sync_software_hint") }}</span>
        </NSpace>
      </NFormItem>
      <NFormItem :label="t('ocs.interval')">
        <NInputNumber v-model:value="form.sync_interval_seconds" :min="300" :max="86400"
                      style="width: 160px" />
      </NFormItem>
      <NFormItem :label="t('ocs.stale_days')">
        <NInputNumber v-model:value="form.stale_after_days" :min="1" :max="3650"
                      style="width: 160px" />
      </NFormItem>
    </NForm>
    <template #footer>
      <NSpace justify="end">
        <NButton @click="show = false">
          <template #icon><NIcon><CancelIcon /></NIcon></template>
          {{ t("common.cancel") }}
        </NButton>
        <NButton type="primary" @click="save">
          <template #icon><NIcon><SaveIcon /></NIcon></template>
          {{ t("common.save") }}
        </NButton>
      </NSpace>
    </template>
  </NModal>

  <!-- 連線診斷 -->
  <NModal v-model:show="diagOpen" preset="card" style="max-width: 560px"
          :title="t('ocs.diag_title')">
    <div v-if="diag">
      <NAlert v-if="diag.unauthenticated_access" type="warning" :show-icon="true"
              style="margin-bottom: 12px">
        {{ t("ocs.warn_unauthenticated") }}
      </NAlert>
      <NSpace vertical :size="6">
        <div>{{ t("ocs.diag_reachable") }}：
          <NTag :type="diag.reachable ? 'success' : 'error'" size="small">
            {{ diag.reachable ? t("common.yes") : t("common.no") }}
          </NTag>
        </div>
        <div>{{ t("ocs.diag_count") }}：{{ diag.computer_count ?? "—" }}</div>
        <div>{{ t("ocs.diag_incremental") }}：
          <NTag :type="diag.incremental ? 'success' : 'default'" size="small">
            {{ diag.incremental ? t("ocs.incremental_yes") : t("ocs.incremental_no") }}
          </NTag>
        </div>
        <div>{{ t("ocs.diag_auth") }}：
          <NTag :type="diag.auth_required ? 'success' : 'warning'" size="small">
            {{ diag.auth_required ? t("ocs.auth_on") : t("ocs.auth_off") }}
          </NTag>
        </div>
      </NSpace>
    </div>
  </NModal>
</template>
