<template>
  <!-- 審核關卡編輯（「申請審核設定」的兩個頁籤共用：IP 申請審核、IP 變更評估審核）。
       兩邊的畫面與欄位一模一樣，差別只在可選的模式（評估多一個「有修改權的人」）與說明文字 -->
  <n-space vertical :size="18" style="max-width: 760px">
    <n-form-item :label="t('req_policy.mode_label')" label-placement="top">
      <n-radio-group :value="modelValue.approver_mode" @update:value="(v: string) => patch({ approver_mode: v })">
        <n-space vertical>
          <n-radio v-for="m in modes" :key="m" :value="m" :data-testid="`approval-mode-${m}`">{{ modeLabel(m) }}</n-radio>
        </n-space>
      </n-radio-group>
    </n-form-item>

    <!-- designated：單一審核人集合 -->
    <template v-if="modelValue.approver_mode === 'designated'">
      <n-form-item :label="t('req_policy.designated_users')" label-placement="top">
        <n-select :value="modelValue.designated_user_ids" multiple filterable :options="userOpts"
                  :placeholder="t('req_policy.pick_users')"
                  @update:value="(v: string[]) => patch({ designated_user_ids: v })" />
      </n-form-item>
      <n-form-item :label="t('req_policy.designated_groups')" label-placement="top">
        <n-select :value="modelValue.designated_group_ids" multiple filterable :options="groupOpts"
                  :placeholder="t('req_policy.pick_groups')"
                  @update:value="(v: string[]) => patch({ designated_group_ids: v })" />
      </n-form-item>
    </template>

    <!-- parallel / stages：多關卡 -->
    <template v-if="isMultiStep">
      <n-alert type="default" :bordered="true" :show-icon="false" style="font-size: 12.5px">
        {{ modelValue.approver_mode === 'stages' ? txt("stages_note") : txt("parallel_note") }}
      </n-alert>
      <div v-for="(s, i) in modelValue.stages" :key="i" class="stage-box" data-testid="approval-stage">
        <div class="stage-head">
          <span class="stage-no">{{ i + 1 }}</span>
          <n-input :value="s.name" size="small" style="flex: 1" :placeholder="t('req_policy.stage_name_ph', { n: i + 1 })"
                   @update:value="(v: string) => setStage(i, { name: v })" />
          <n-button v-if="modelValue.approver_mode === 'stages'" size="tiny" quaternary :disabled="i === 0"
                    @click="moveStage(i, -1)">↑</n-button>
          <n-button v-if="modelValue.approver_mode === 'stages'" size="tiny" quaternary
                    :disabled="i === modelValue.stages.length - 1" @click="moveStage(i, 1)">↓</n-button>
          <n-button size="tiny" quaternary type="error" @click="removeStage(i)">
            <template #icon><n-icon><DeleteIcon /></n-icon></template>
          </n-button>
        </div>
        <n-space vertical :size="8" style="margin-top: 8px">
          <n-select :value="s.user_ids" multiple filterable size="small" :options="userOpts"
                    :placeholder="t('req_policy.pick_users')" @update:value="(v: string[]) => setStage(i, { user_ids: v })" />
          <n-select :value="s.group_ids" multiple filterable size="small" :options="groupOpts"
                    :placeholder="t('req_policy.pick_groups')" @update:value="(v: string[]) => setStage(i, { group_ids: v })" />
        </n-space>
      </div>
      <n-button dashed @click="addStage">
        <template #icon><n-icon><PlusIcon /></n-icon></template>
        {{ t("req_policy.add_stage") }}
      </n-button>
    </template>

    <n-divider style="margin: 4px 0" />

    <n-form-item :label="txt('self_approve')" label-placement="left">
      <n-switch :value="modelValue.allow_self_approve" @update:value="(v: boolean) => patch({ allow_self_approve: v })" />
      <span style="margin-left: 10px; font-size: 12.5px; opacity: .7">{{ selfHint || t("req_policy.self_approve_hint") }}</span>
    </n-form-item>
  </n-space>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { NAlert, NButton, NDivider, NFormItem, NIcon, NInput, NRadio, NRadioGroup, NSelect, NSpace, NSwitch } from "naive-ui";
import { DeleteIcon, PlusIcon } from "@/icons";

export interface ApprovalStep { name: string; user_ids: string[]; group_ids: string[] }
export interface ApprovalPolicy {
  approver_mode: string; designated_user_ids: string[]; designated_group_ids: string[];
  allow_self_approve: boolean; stages: ApprovalStep[];
}

const props = defineProps<{
  modelValue: ApprovalPolicy;
  modes: string[];
  userOpts: { label: string; value: string }[];
  groupOpts: { label: string; value: string }[];
  selfHint?: string;
  /** change_impact：模式、說明與「自己覆核」改用評估的說法（IP 申請的是「核准即配發」） */
  kind?: "ip_request" | "change_impact";
}>();
const emit = defineEmits<{ (e: "update:modelValue", v: ApprovalPolicy): void }>();
const { t } = useI18n();

const isMultiStep = computed(() => ["parallel", "stages"].includes(props.modelValue.approver_mode));

const ci = computed(() => props.kind === "change_impact");
function modeLabel(m: string): string {
  return ci.value ? t(`approval.ci_mode_${m}`) : t(`req_policy.mode_${m}`);
}
function txt(key: "parallel_note" | "stages_note" | "self_approve"): string {
  return ci.value ? t(`approval.ci_${key}`) : t(`req_policy.${key}`);
}
function patch(p: Partial<ApprovalPolicy>) { emit("update:modelValue", { ...props.modelValue, ...p }); }
function setStage(i: number, p: Partial<ApprovalStep>) {
  patch({ stages: props.modelValue.stages.map((s, j) => (j === i ? { ...s, ...p } : s)) });
}
function addStage() { patch({ stages: [...props.modelValue.stages, { name: "", user_ids: [], group_ids: [] }] }); }
function removeStage(i: number) { patch({ stages: props.modelValue.stages.filter((_s, j) => j !== i) }); }
function moveStage(i: number, d: number) {
  const j = i + d;
  const s = [...props.modelValue.stages];
  if (j < 0 || j >= s.length) return;
  [s[i], s[j]] = [s[j], s[i]];
  patch({ stages: s });
}
</script>

<style scoped>
.stage-box { border: 1px solid var(--n-border-color, #e0e0e6); border-radius: 8px; padding: 12px 14px; }
.stage-head { display: flex; align-items: center; gap: 8px; }
.stage-no {
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 22px; border-radius: 50%;
  background: var(--n-color-target, #36ad6a); color: #fff; font-size: 12px; flex: none;
}
</style>
