<template>
  <n-modal :show="show" preset="card" style="width: min(900px, 96vw)" :mask-closable="!busy"
           :title="form.id ? (canEdit ? t('change_impact.svc.edit') : form.name) : t('change_impact.svc.new')"
           data-testid="svc-editor" @update:show="(v: boolean) => !v && emit('update:show', false)">
    <n-spin :show="loading">
      <n-form label-placement="top" :disabled="!canEdit || busy">
        <div class="svc-row">
          <n-form-item :label="t('change_impact.svc.f_name')" class="svc-grow">
            <n-input v-model:value="form.name" maxlength="160" data-testid="svc-name" />
          </n-form-item>
          <n-form-item :label="t('change_impact.svc.col_criticality')">
            <n-select v-model:value="form.criticality" :options="critOptions" style="width: 130px" />
          </n-form-item>
          <n-form-item :label="t('change_impact.svc.col_status')">
            <n-select v-model:value="form.status" :options="statusOptions" style="width: 120px" />
          </n-form-item>
        </div>
        <n-form-item :label="t('change_impact.svc.f_description')">
          <n-input v-model:value="form.description" type="textarea" :autosize="{ minRows: 1, maxRows: 4 }" maxlength="4000" />
        </n-form-item>

        <!-- 依賴群組：每個群組都必要；群組內至少 required_count 個成員可用才算滿足 -->
        <div class="svc-section">
          <div class="svc-section__head">
            <span class="svc-section__title">{{ t("change_impact.svc.groups") }}</span>
            <span class="svc-hint">{{ t("change_impact.svc.groups_hint") }}</span>
          </div>
          <div v-for="(g, gi) in form.groups" :key="gi" class="svc-group" data-testid="svc-group">
            <div class="svc-row svc-row--center">
              <n-input v-model:value="g.name" size="small" class="svc-grow" maxlength="120"
                       :placeholder="t('change_impact.svc.group_name')" />
              <span class="svc-hint">{{ t("change_impact.svc.required") }}</span>
              <n-input-number v-model:value="g.required_count" size="small" :min="1" :max="100" style="width: 90px"
                              data-testid="svc-required" />
              <n-checkbox v-model:checked="g.confirmed">{{ t("change_impact.svc.confirmed") }}</n-checkbox>
              <n-button v-if="canEdit" size="small" quaternary type="error" @click="form.groups.splice(gi, 1)">
                <template #icon><n-icon><DeleteIcon /></n-icon></template>
              </n-button>
            </div>
            <div v-for="(m, mi) in g.members" :key="mi" class="svc-member">
              <n-select v-model:value="m.object_type" size="small" :options="memberTypeOptions" style="width: 110px"
                        @update:value="() => { m.object_id = ''; m.label = null }" />
              <ServiceObjectSelect class="svc-grow" :type="m.object_type" :value="m.object_id || null" :label="m.label"
                                   :disabled="!canEdit" @pick="(id, label) => { m.object_id = id ?? ''; m.label = label }" />
              <n-select v-model:value="m.relation_type" size="small" :options="relationOptions" style="width: 180px" />
              <n-tag v-if="m.missing" size="small" type="warning" :bordered="false">{{ t("change_impact.svc.missing") }}</n-tag>
              <n-button v-if="canEdit" size="small" quaternary @click="g.members.splice(mi, 1)">
                <template #icon><n-icon><CancelIcon /></n-icon></template>
              </n-button>
            </div>
            <n-button v-if="canEdit" size="tiny" secondary data-testid="svc-add-member"
                      @click="g.members.push({ object_type: 'device', object_id: '', relation_type: 'hosted_on', label: null })">
              <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("change_impact.svc.add_member") }}
            </n-button>
          </div>
          <n-button v-if="canEdit" size="small" data-testid="svc-add-group"
                    @click="form.groups.push({ name: '', required_count: 1, confirmed: true, members: [] })">
            <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("change_impact.svc.add_group") }}
          </n-button>
        </div>

        <!-- 端點：既有物件或主機名稱＋埠（只是登錄資料，不會被拿去探測） -->
        <div class="svc-section">
          <div class="svc-section__head">
            <span class="svc-section__title">{{ t("change_impact.svc.endpoints") }}</span>
          </div>
          <div v-for="(e, ei) in form.endpoints" :key="ei" class="svc-member">
            <n-select :value="e.object_type ?? 'hostname'" size="small" :options="endpointTypeOptions" style="width: 120px"
                      @update:value="(v: string) => { e.object_type = v === 'hostname' ? null : (v as any); e.object_id = null; e.label = null }" />
            <ServiceObjectSelect v-if="e.object_type" class="svc-grow" :type="e.object_type" :value="e.object_id"
                                 :label="e.label" :disabled="!canEdit"
                                 @pick="(id, label) => { e.object_id = id; e.label = label }" />
            <template v-else>
              <n-input v-model:value="e.hostname" size="small" class="svc-grow" :placeholder="t('change_impact.svc.hostname')" />
              <n-input-number v-model:value="e.port" size="small" :min="1" :max="65535" style="width: 100px"
                              :placeholder="t('change_impact.svc.port')" />
              <n-input v-model:value="e.protocol" size="small" style="width: 80px" maxlength="8"
                       :placeholder="t('change_impact.svc.protocol')" />
            </template>
            <n-button v-if="canEdit" size="small" quaternary @click="form.endpoints.splice(ei, 1)">
              <template #icon><n-icon><CancelIcon /></n-icon></template>
            </n-button>
          </div>
          <n-button v-if="canEdit" size="small"
                    @click="form.endpoints.push({ object_type: 'ip', object_id: null, hostname: null, port: null, protocol: null, label: null })">
            <template #icon><n-icon><PlusIcon /></n-icon></template>{{ t("change_impact.svc.add_endpoint") }}
          </n-button>
        </div>

        <n-form-item :label="t('change_impact.svc.f_notes')">
          <n-input v-model:value="form.maintenance_notes" type="textarea" :autosize="{ minRows: 1, maxRows: 4 }" maxlength="4000" />
        </n-form-item>
      </n-form>
      <n-alert v-if="error" type="error" :bordered="false">{{ error }}</n-alert>
    </n-spin>
    <template #footer>
      <div class="svc-footer">
        <n-popconfirm v-if="canEdit && form.id" @positive-click="remove">
          <template #trigger>
            <n-button type="error" ghost :loading="busy" data-testid="svc-delete">
              <template #icon><n-icon><DeleteIcon /></n-icon></template>{{ t("common.delete") }}
            </n-button>
          </template>
          {{ t("change_impact.svc.delete_confirm") }}
        </n-popconfirm>
        <span class="svc-grow" />
        <n-button :disabled="busy" @click="emit('update:show', false)">
          <template #icon><n-icon><CancelIcon /></n-icon></template>{{ canEdit ? t("common.cancel") : t("common.close") }}
        </n-button>
        <n-button v-if="canEdit" type="primary" :loading="busy" :disabled="!form.name.trim()" data-testid="svc-save" @click="save">
          <template #icon><n-icon><SaveIcon /></n-icon></template>{{ t("common.save") }}
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import {
  NAlert, NButton, NCheckbox, NForm, NFormItem, NIcon, NInput, NInputNumber, NModal, NPopconfirm, NSelect, NSpin, NTag,
  useMessage,
} from "naive-ui";
import { apiErrMsg } from "@/api/client";
import {
  createService, deleteService, getService, updateService, type ImpactService, type MemberType, type RelationType,
} from "@/api/changeImpact";
import ServiceObjectSelect from "@/components/ServiceObjectSelect.vue";
import { CancelIcon, DeleteIcon, PlusIcon, SaveIcon } from "@/icons";

const props = defineProps<{ show: boolean; serviceId: string | null; canEdit: boolean }>();
const emit = defineEmits<{ (e: "update:show", v: boolean): void; (e: "saved"): void }>();
const { t } = useI18n();
const msg = useMessage();

interface MemberRow { object_type: MemberType; object_id: string; relation_type: RelationType; label: string | null; missing?: boolean }
interface GroupRow { name: string; required_count: number; confirmed: boolean; members: MemberRow[] }
interface EndpointRow { object_type: "ip" | "device" | "vm" | null; object_id: string | null; hostname: string | null;
  port: number | null; protocol: string | null; label: string | null }

const blank = () => ({
  id: "" as string, name: "", criticality: "normal" as ImpactService["criticality"],
  status: "active" as ImpactService["status"], description: "" as string | null, maintenance_notes: "" as string | null,
  version: 0, groups: [] as GroupRow[], endpoints: [] as EndpointRow[],
});
const form = reactive(blank());
const loading = ref(false);
const busy = ref(false);
const error = ref("");

const critOptions = computed(() => ["critical", "high", "normal", "low"]
  .map((v) => ({ label: t(`change_impact.param.criticality.${v}`), value: v })));
const statusOptions = computed(() => ["active", "retired"]
  .map((v) => ({ label: t(`change_impact.svc.status.${v}`), value: v })));
const memberTypeOptions = computed(() => ["device", "vm", "ip", "subnet", "service"]
  .map((v) => ({ label: t(`change_impact.svc.type.${v}`), value: v })));
const endpointTypeOptions = computed(() => ["ip", "device", "vm", "hostname"]
  .map((v) => ({ label: t(`change_impact.svc.type.${v}`), value: v })));
const relationOptions = computed(() => ["hosted_on", "requires_network", "requires_storage", "requires_power",
  "requires_service", "references", "observed_on"].map((v) => ({ label: t(`change_impact.svc.rel.${v}`), value: v })));

watch(() => props.show, async (v) => {
  if (!v) return;
  Object.assign(form, blank());
  error.value = "";
  if (!props.serviceId) return;
  loading.value = true;
  try {
    const s = await getService(props.serviceId);
    Object.assign(form, {
      id: s.id, name: s.name, criticality: s.criticality, status: s.status, description: s.description,
      maintenance_notes: s.maintenance_notes, version: s.version,
      groups: (s.groups ?? []).map((g) => ({ name: g.name, required_count: g.required_count, confirmed: g.confirmed,
        members: g.members.map((m) => ({ object_type: m.object_type, object_id: m.object_id, relation_type: m.relation_type,
          label: m.label ?? null, missing: m.missing })) })),
      endpoints: (s.endpoints ?? []).map((e) => ({ object_type: e.object_type, object_id: e.object_id, hostname: e.hostname,
        port: e.port, protocol: e.protocol, label: e.label ?? null })),
    });
  } catch (e) { error.value = apiErrMsg(e); } finally { loading.value = false; }
});

function payload(): Record<string, unknown> {
  return {
    name: form.name.trim(), criticality: form.criticality, status: form.status,
    description: form.description || null, maintenance_notes: form.maintenance_notes || null,
    expected_version: form.id ? form.version : undefined,
    groups: form.groups.map((g) => ({ name: g.name.trim() || "—", required_count: g.required_count, confirmed: g.confirmed,
      members: g.members.filter((m) => m.object_id).map((m) => ({ object_type: m.object_type, object_id: m.object_id,
        relation_type: m.relation_type })) })),
    endpoints: form.endpoints.filter((e) => e.object_id || e.hostname).map((e) => ({ object_type: e.object_type,
      object_id: e.object_id, hostname: e.object_type ? null : e.hostname, port: e.object_type ? null : e.port,
      protocol: e.object_type ? null : e.protocol })),
  };
}

async function save() {
  busy.value = true;
  error.value = "";
  try {
    if (form.id) await updateService(form.id, payload());
    else await createService(payload());
    msg.success(t("common.ok"));
    emit("saved");
    emit("update:show", false);
  } catch (e) { error.value = apiErrMsg(e); } finally { busy.value = false; }
}

async function remove() {
  busy.value = true;
  try {
    await deleteService(form.id);
    emit("saved");
    emit("update:show", false);
  } catch (e) { error.value = apiErrMsg(e); } finally { busy.value = false; }
}
</script>

<style scoped>
.svc-row { display: flex; gap: 10px; flex-wrap: wrap; }
.svc-row--center { align-items: center; margin-bottom: 6px; }
.svc-grow { flex: 1 1 200px; min-width: 0; }
.svc-section { margin: 4px 0 14px; display: flex; flex-direction: column; gap: 8px; }
.svc-section__head { display: flex; flex-direction: column; gap: 2px; }
.svc-section__title { font-weight: 600; }
.svc-hint { font-size: 12px; opacity: .65; }
.svc-group { border: 1px solid var(--n-border-color, rgba(128, 128, 128, .28)); border-radius: 10px; padding: 10px 12px;
  display: flex; flex-direction: column; gap: 6px; background: rgba(127, 127, 127, .03); }
.svc-member { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.svc-footer { display: flex; gap: 8px; align-items: center; }
</style>
