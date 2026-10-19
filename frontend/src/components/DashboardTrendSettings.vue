<script setup lang="ts">
/**
 * 儀表板「近 14 日 稽核 / IP 異動 趨勢」的個人設定（使用者 2026-10-18）：排除子網路、排除 DHCP 範圍內的位址、
 * 只計入部分異動類型。IP 異動的量多半是 DHCP 範圍裡的機器上線／離線與 MAC 變化，真正在意的變化被淹掉。
 * 設定跟著帳號存（user_preferences.dashboard.trend），換電腦也一樣。
 */
import { ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NModal, NForm, NFormItem, NSelect, NCheckbox, NCheckboxGroup, NSpace, NButton, NIcon, useMessage,
} from "naive-ui";
import { SaveIcon, CancelIcon, SettingsIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import { listSubnets } from "@/api/subnets";
import {
  getPreferences, updatePreferences, TREND_EVENT_GROUPS, type TrendEventGroup,
} from "@/api/preferences";

const props = defineProps<{ show: boolean }>();
const emit = defineEmits<{ (e: "update:show", v: boolean): void; (e: "saved"): void }>();
const { t } = useI18n();
const msg = useMessage();

const subnetOptions = ref<{ label: string; value: string }[]>([]);
const excludeSubnets = ref<string[]>([]);
const excludeDhcp = ref(false);
const groups = ref<TrendEventGroup[]>([...TREND_EVENT_GROUPS]);
const saving = ref(false);

async function loadSubnets() {
  // 子網路可能上千個：分頁抓完（每頁 1000），選單本身會虛擬捲動
  const out: { label: string; value: string }[] = [];
  for (let page = 1; page <= 20; page++) {
    const r = await listSubnets({ page, pageSize: 1000 });
    for (const s of r.items) out.push({ label: s.description ? `${s.cidr} — ${s.description}` : s.cidr, value: s.id });
    if (out.length >= r.total || !r.items.length) break;
  }
  subnetOptions.value = out;
}

watch(() => props.show, async (v) => {
  if (!v) return;
  try {
    const [prefs] = await Promise.all([getPreferences(), loadSubnets()]);
    const tp = prefs.dashboard?.trend;
    excludeSubnets.value = tp?.exclude_subnet_ids ?? [];
    excludeDhcp.value = !!tp?.exclude_dhcp;
    groups.value = tp?.event_groups ? [...tp.event_groups] : [...TREND_EVENT_GROUPS];
  } catch (e) { msg.error(apiErrMsg(e)); }
});

async function save() {
  saving.value = true;
  try {
    const all = TREND_EVENT_GROUPS.every((g) => groups.value.includes(g));
    await updatePreferences({ dashboard: { trend: {
      exclude_subnet_ids: excludeSubnets.value, exclude_dhcp: excludeDhcp.value,
      event_groups: all ? null : TREND_EVENT_GROUPS.filter((g) => groups.value.includes(g)),
    } } });
    emit("saved");
    emit("update:show", false);
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { saving.value = false; }
}
</script>

<template>
  <n-modal :show="show" preset="card" style="width: 560px; max-width: calc(100vw - 32px)"
           data-testid="trend-settings" @update:show="(v: boolean) => emit('update:show', v)">
    <template #header>
      <n-space align="center" :wrap-item="false">
        <n-icon :size="20"><SettingsIcon /></n-icon>
        <span>{{ t("dashboard.trend_settings_title") }}</span>
      </n-space>
    </template>
    <div class="ts-intro">{{ t("dashboard.trend_settings_intro") }}</div>
    <n-form label-placement="top">
      <n-form-item :label="t('dashboard.trend_exclude_subnets')">
        <n-select v-model:value="excludeSubnets" :options="subnetOptions" multiple filterable clearable
                  data-testid="trend-exclude-subnets" :placeholder="t('dashboard.trend_exclude_subnets_ph')" />
      </n-form-item>
      <n-form-item :label="t('dashboard.trend_dhcp')" :show-feedback="false">
        <div style="width: 100%">
          <n-checkbox v-model:checked="excludeDhcp" data-testid="trend-exclude-dhcp">
            {{ t("dashboard.trend_exclude_dhcp") }}
          </n-checkbox>
          <div class="ts-hint">{{ t("dashboard.trend_exclude_dhcp_hint") }}</div>
        </div>
      </n-form-item>
      <n-form-item :label="t('dashboard.trend_event_groups')" style="margin-top: 12px">
        <n-checkbox-group v-model:value="groups" data-testid="trend-event-groups">
          <n-space :size="14">
            <n-checkbox v-for="g in TREND_EVENT_GROUPS" :key="g" :value="g">
              {{ t(`dashboard.trend_group_${g}`) }}
            </n-checkbox>
          </n-space>
        </n-checkbox-group>
      </n-form-item>
    </n-form>
    <n-space justify="end">
      <n-button @click="emit('update:show', false)">
        <template #icon><n-icon><CancelIcon /></n-icon></template>
        {{ t("common.cancel") }}
      </n-button>
      <n-button type="primary" :loading="saving" :disabled="saving || !groups.length" data-testid="trend-save"
                @click="save">
        <template #icon><n-icon><SaveIcon /></n-icon></template>
        {{ t("common.save") }}
      </n-button>
    </n-space>
  </n-modal>
</template>

<style scoped>
.ts-intro { font-size: 13px; opacity: .75; margin-bottom: 12px; }
.ts-hint { font-size: 12px; opacity: .65; margin-top: 4px; }
</style>
