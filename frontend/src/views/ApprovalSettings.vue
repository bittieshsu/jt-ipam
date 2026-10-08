<script setup lang="ts">
/**
 * 申請審核設定（使用者 2026-10-08：原「IP 申請審核設定」改名，分成兩個頁籤各自設定審核關卡）。
 * - IP 申請審核：/ip-requests/policy/config
 * - IP 變更評估審核：/change-impact/review-policy（評估送審就照這裡走審核）
 * 兩個頁籤用同一個編輯元件（ApprovalPolicyEditor），畫面與欄位一致。
 */
import { onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { NAlert, NButton, NCard, NIcon, NSpace, NTabPane, NTabs, useMessage } from "naive-ui";
import { RequestsIcon, SaveIcon } from "@/icons";
import { getRequestPolicy, setRequestPolicy, type IPRequestPolicy } from "@/api/ip_requests";
import { getReviewPolicy, setReviewPolicy, type ReviewPolicy } from "@/api/changeImpact";
import { listGroups, listUsers } from "@/api/admin";
import { apiErrMsg } from "@/api/client";
import ApprovalPolicyEditor, { type ApprovalPolicy } from "@/components/ApprovalPolicyEditor.vue";

const { t } = useI18n();
const msg = useMessage();
const route = useRoute();
const router = useRouter();

const TABS = ["ip_request", "change_impact"] as const;
const tab = ref<string>(TABS.includes(route.query.tab as never) ? String(route.query.tab) : "ip_request");
watch(tab, (v) => { router.replace({ query: { ...route.query, tab: v } }).catch(() => {}); });

const empty = (mode: string): ApprovalPolicy => ({
  approver_mode: mode, designated_user_ids: [], designated_group_ids: [], allow_self_approve: false, stages: [],
});
const ipPolicy = ref<ApprovalPolicy>(empty("admin"));
const ciPolicy = ref<ApprovalPolicy>(empty("editors"));
const ciAvailable = ref(true);
const userOpts = ref<{ label: string; value: string }[]>([]);
const groupOpts = ref<{ label: string; value: string }[]>([]);
const saving = ref("");

async function load() {
  try {
    const [users, groups] = await Promise.all([listUsers("", "", 500, 0), listGroups(500, 0)]);
    userOpts.value = users.items.map((u) => ({ label: `${u.display_name || u.username} (${u.username})`, value: u.id }));
    groupOpts.value = groups.items.map((g) => ({ label: g.name, value: g.id }));
  } catch { /* 選項載不到就只顯示已存的 id */ }
  try { const p = await getRequestPolicy(); ipPolicy.value = { ...p, stages: p.stages ?? [] }; }
  catch (e) { msg.error(apiErrMsg(e)); }
  try { const p = await getReviewPolicy(); ciPolicy.value = { ...p, stages: p.stages ?? [] }; }
  catch { ciAvailable.value = false; }
}

/** 多關卡至少一關、每關要有審核人；指定人員至少一位（後端也會擋） */
function invalid(p: ApprovalPolicy): string {
  if (["parallel", "stages"].includes(p.approver_mode)) {
    if (!p.stages.length) return t("req_policy.need_stage");
    if (p.stages.some((s) => !s.user_ids.length && !s.group_ids.length)) return t("req_policy.stage_need_approver");
  }
  if (p.approver_mode === "designated" && !p.designated_user_ids.length && !p.designated_group_ids.length) {
    return t("errors.approval_need_designated");
  }
  return "";
}

async function save(which: "ip_request" | "change_impact") {
  const p = which === "ip_request" ? ipPolicy.value : ciPolicy.value;
  const bad = invalid(p);
  if (bad) { msg.warning(bad); return; }
  saving.value = which;
  try {
    if (which === "ip_request") {
      const r = await setRequestPolicy(p as IPRequestPolicy);
      ipPolicy.value = { ...r, stages: r.stages ?? [] };
    } else {
      const r = await setReviewPolicy(p as ReviewPolicy);
      ciPolicy.value = { ...r, stages: r.stages ?? [] };
    }
    msg.success(t("common.saved"));
  } catch (e) { msg.error(apiErrMsg(e)); } finally { saving.value = ""; }
}

onMounted(load);
</script>

<template>
  <n-card>
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="22"><RequestsIcon /></n-icon>
        <span>{{ t("approval.title") }}</span>
      </n-space>
    </template>

    <n-tabs v-model:value="tab" type="line" animated>
      <n-tab-pane name="ip_request" :tab="t('approval.tab_ip_request')" data-testid="approval-tab-ip">
        <n-space vertical :size="18">
          <n-alert type="info" :show-icon="true" style="max-width: 760px">{{ t("req_policy.intro") }}</n-alert>
          <ApprovalPolicyEditor v-model="ipPolicy" :modes="['admin', 'designated', 'parallel', 'stages']"
                                :user-opts="userOpts" :group-opts="groupOpts" />
          <div>
            <n-button type="success" :loading="saving === 'ip_request'" data-testid="approval-save-ip" @click="save('ip_request')">
              <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
            </n-button>
          </div>
        </n-space>
      </n-tab-pane>

      <n-tab-pane name="change_impact" :tab="t('approval.tab_change_impact')" data-testid="approval-tab-ci">
        <n-alert v-if="!ciAvailable" type="warning" :bordered="false">{{ t("approval.ci_unavailable") }}</n-alert>
        <n-space v-else vertical :size="18">
          <n-alert type="info" :show-icon="true" style="max-width: 760px">{{ t("approval.ci_intro") }}</n-alert>
          <ApprovalPolicyEditor v-model="ciPolicy" :modes="['editors', 'admin', 'designated', 'parallel', 'stages']"
                                :user-opts="userOpts" :group-opts="groupOpts" :self-hint="t('approval.ci_self_hint')"
                                kind="change_impact" />
          <div>
            <n-button type="success" :loading="saving === 'change_impact'" data-testid="approval-save-ci"
                      @click="save('change_impact')">
              <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
            </n-button>
          </div>
        </n-space>
      </n-tab-pane>
    </n-tabs>
  </n-card>
</template>
