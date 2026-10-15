<script setup lang="ts">
/**
 * SNMP 憑證的新增／編輯（唯一一份：SNMP 頁的「憑證」頁籤與收集目標表單裡的「＋新增憑證」都用這個）。
 *
 * 機密（社群字串、v3 密碼）存了就不回傳：編輯時欄位是空的，旁邊標「已設定」，留空＝不變。
 * noAuthNoPriv 刻意不開放（沒有驗證，比 v2c 好不到哪去，卻讓人以為用了 v3 很安全）。
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NModal, NForm, NFormItem, NInput, NInputNumber, NCheckbox, NSelect, NRadioGroup, NRadioButton,
  NAlert, NButton, NIcon, NSpace, NTag, NCollapse, NCollapseItem, useMessage,
} from "naive-ui";
import { SaveIcon, CancelIcon, KeyIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import {
  createSnmpCredential, updateSnmpCredential, SNMP_AUTH_PROTOCOLS, SNMP_PRIV_PROTOCOLS,
  type SnmpCredential,
} from "@/api/snmp";
import {
  blankCredentialForm, buildCredentialPayload, credentialFormFrom, type CredentialForm,
} from "@/utils/snmp";
import { useCustomers } from "@/composables/useCustomers";

const props = defineProps<{ show: boolean; credential?: SnmpCredential | null }>();
const emit = defineEmits<{
  (e: "update:show", v: boolean): void;
  (e: "saved", c: SnmpCredential): void;
}>();

const { t } = useI18n();
const msg = useMessage();
const { options: customerOptions, ensureLoaded: ensureCustomers } = useCustomers();

const form = ref<CredentialForm>(blankCredentialForm());
const saving = ref(false);
const editing = computed(() => props.credential ?? null);

watch(() => props.show, (v) => {
  if (!v) return;
  form.value = editing.value ? credentialFormFrom(editing.value) : blankCredentialForm();
  void ensureCustomers();
}, { immediate: true });

// 不建議的協定照樣可以選（舊設備只支援它們），但選單上要講清楚
const authOptions = computed(() => SNMP_AUTH_PROTOCOLS.map((p) => ({
  value: p, label: p === "MD5" ? t("snmp.proto_not_recommended", { p }) : p,
})));
const privOptions = computed(() => SNMP_PRIV_PROTOCOLS.map((p) => ({
  value: p, label: p === "DES" ? t("snmp.proto_not_recommended", { p }) : p,
})));
const levelOptions = computed(() => [
  { value: "authPriv", label: t("snmp.level_authpriv") },
  { value: "authNoPriv", label: t("snmp.level_authnopriv") },
]);

/** 編輯時：這個機密已經存過了 → 欄位留空＝不變 */
function isSet(field: "community" | "auth_passphrase" | "priv_passphrase"): boolean {
  const c = editing.value;
  if (!c) return false;
  if (field === "community") return c.has_community;
  if (field === "auth_passphrase") return c.has_auth_passphrase;
  return c.has_priv_passphrase;
}
function secretPh(field: "community" | "auth_passphrase" | "priv_passphrase"): string {
  if (isSet(field)) return t("snmp.secret_keep");
  return field === "community" ? "" : t("snmp.passphrase_min");
}

function close() { emit("update:show", false); }

async function save() {
  if (!form.value.name.trim()) {
    msg.warning(t("common.name_required"));
    return;
  }
  saving.value = true;
  try {
    const payload = buildCredentialPayload(form.value);
    const saved = editing.value
      ? await updateSnmpCredential(editing.value.id, payload)
      : await createSnmpCredential(payload);
    msg.success(t("common.saved"));
    emit("saved", saved);
    close();
  } catch (e) {
    msg.error(apiErrMsg(e));
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <n-modal :show="show" preset="card" style="width: 600px; max-width: calc(100vw - 32px)"
           data-testid="snmp-credential-form" @update:show="(v: boolean) => emit('update:show', v)">
    <template #header>
      <n-space align="center" :size="8" :wrap-item="false">
        <n-icon :size="20"><KeyIcon /></n-icon>
        <span>{{ editing ? t("snmp.cred_edit_title") : t("snmp.cred_create_title") }}</span>
      </n-space>
    </template>
    <n-form label-placement="top">
      <n-form-item :label="t('common.name')">
        <n-input v-model:value="form.name" :maxlength="128" data-testid="snmp-cred-name" />
      </n-form-item>
      <n-form-item :show-label="false" style="margin-top: -8px">
        <div>
          <n-checkbox v-model:checked="form.is_default" data-testid="snmp-cred-default">
            {{ t("snmp.cred_default") }}
          </n-checkbox>
          <div class="snmp-hint">{{ t("snmp.cred_default_hint") }}</div>
        </div>
      </n-form-item>
      <n-form-item :label="t('cols.unit')">
        <div style="width: 100%">
          <n-select v-model:value="form.customer_id" :options="customerOptions" filterable clearable
                    :placeholder="t('snmp.cred_all_units')" />
          <div class="snmp-hint">{{ t("snmp.cred_unit_hint") }}</div>
        </div>
      </n-form-item>
      <n-form-item :label="t('snmp.version')">
        <n-radio-group v-model:value="form.version" data-testid="snmp-cred-version">
          <n-radio-button value="v2c">SNMPv2c</n-radio-button>
          <n-radio-button value="v3">SNMPv3</n-radio-button>
        </n-radio-group>
      </n-form-item>

      <template v-if="form.version === 'v2c'">
        <n-alert type="warning" :bordered="false" style="margin-bottom: 12px" data-testid="snmp-v2c-warning">
          {{ t("snmp.v2c_warning") }}
        </n-alert>
        <n-form-item>
          <template #label>
            <span>{{ t("snmp.community") }}</span>
            <n-tag v-if="isSet('community')" size="tiny" type="success" :bordered="false" class="snmp-set-tag">
              {{ t("snmp.secret_set") }}
            </n-tag>
          </template>
          <n-input v-model:value="form.community" type="password" show-password-on="click" :maxlength="128"
                   :placeholder="secretPh('community')" :input-props="{ autocomplete: 'new-password' }"
                   data-testid="snmp-cred-community" />
        </n-form-item>
      </template>

      <template v-else>
        <n-form-item :label="t('snmp.username')">
          <n-input v-model:value="form.username" :maxlength="64" :input-props="{ autocomplete: 'off' }"
                   data-testid="snmp-cred-username" />
        </n-form-item>
        <n-form-item :label="t('snmp.security_level')">
          <div style="width: 100%">
            <n-select v-model:value="form.security_level" :options="levelOptions" />
            <div class="snmp-hint">{{ t("snmp.level_hint") }}</div>
          </div>
        </n-form-item>
        <div class="snmp-pair">
          <n-form-item :label="t('snmp.auth_protocol')">
            <n-select v-model:value="form.auth_protocol" :options="authOptions" />
          </n-form-item>
          <n-form-item>
            <template #label>
              <span>{{ t("snmp.auth_passphrase") }}</span>
              <n-tag v-if="isSet('auth_passphrase')" size="tiny" type="success" :bordered="false" class="snmp-set-tag">
                {{ t("snmp.secret_set") }}
              </n-tag>
            </template>
            <n-input v-model:value="form.auth_passphrase" type="password" show-password-on="click" :maxlength="128"
                     :placeholder="secretPh('auth_passphrase')" :input-props="{ autocomplete: 'new-password' }"
                     data-testid="snmp-cred-auth-pass" />
          </n-form-item>
        </div>
        <div v-if="form.security_level === 'authPriv'" class="snmp-pair">
          <n-form-item :label="t('snmp.priv_protocol')">
            <n-select v-model:value="form.priv_protocol" :options="privOptions" />
          </n-form-item>
          <n-form-item>
            <template #label>
              <span>{{ t("snmp.priv_passphrase") }}</span>
              <n-tag v-if="isSet('priv_passphrase')" size="tiny" type="success" :bordered="false" class="snmp-set-tag">
                {{ t("snmp.secret_set") }}
              </n-tag>
            </template>
            <n-input v-model:value="form.priv_passphrase" type="password" show-password-on="click" :maxlength="128"
                     :placeholder="secretPh('priv_passphrase')" :input-props="{ autocomplete: 'new-password' }"
                     data-testid="snmp-cred-priv-pass" />
          </n-form-item>
        </div>
      </template>
      <div v-if="editing" class="snmp-hint" style="margin: -6px 0 10px">{{ t("snmp.secret_keep_hint") }}</div>

      <n-collapse style="margin-bottom: 12px">
        <n-collapse-item :title="t('snmp.advanced')" name="adv">
          <div class="snmp-pair">
            <n-form-item :label="t('snmp.timeout')">
              <n-input-number v-model:value="form.timeout_s" :min="1" :max="30" style="width: 100%">
                <template #suffix>{{ t("scan_probes.secs") }}</template>
              </n-input-number>
            </n-form-item>
            <n-form-item :label="t('snmp.retries')">
              <n-input-number v-model:value="form.retries" :min="0" :max="5" style="width: 100%" />
            </n-form-item>
          </div>
        </n-collapse-item>
      </n-collapse>

      <n-form-item :label="t('common.description')">
        <n-input v-model:value="form.description" type="textarea" :rows="2" :maxlength="2048" />
      </n-form-item>
    </n-form>
    <n-space justify="end">
      <n-button @click="close">
        <template #icon><n-icon><CancelIcon /></n-icon></template>
        {{ t("common.cancel") }}
      </n-button>
      <n-button type="primary" :loading="saving" :disabled="saving" data-testid="snmp-cred-save" @click="save">
        <template #icon><n-icon><SaveIcon /></n-icon></template>
        {{ t("common.save") }}
      </n-button>
    </n-space>
  </n-modal>
</template>

<style scoped>
.snmp-hint { font-size: 12px; opacity: .7; margin-top: 4px; line-height: 1.5; }
.snmp-set-tag { margin-left: 6px; }
.snmp-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
@media (max-width: 560px) { .snmp-pair { grid-template-columns: 1fr; } }
</style>
