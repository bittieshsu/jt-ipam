<template>
  <!-- 服務的成員／端點物件選擇：依類型遠端搜尋（後端只回看得到的），已選的名稱記著，搜尋時不會變回 id -->
  <n-select :value="value" filterable remote clearable :loading="loading" :options="options" :disabled="disabled"
            size="small" :placeholder="t('change_impact.svc.member_object_ph')" @search="search"
            @update:value="onPick" @focus="() => { if (!opts.length) void search('') }" />
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { NSelect } from "naive-ui";
import { searchServiceObjects, type MemberType } from "@/api/changeImpact";

const props = defineProps<{ type: MemberType; value: string | null; label?: string | null; disabled?: boolean }>();
const emit = defineEmits<{ (e: "pick", id: string | null, label: string | null): void }>();
const { t } = useI18n();

const opts = ref<{ label: string; value: string }[]>([]);
const loading = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;

const options = computed(() => {
  const out = new Map(opts.value.map((o) => [o.value, o]));
  if (props.value && !out.has(props.value)) out.set(props.value, { value: props.value, label: props.label || props.value });
  return [...out.values()];
});

function search(q: string) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(async () => {
    loading.value = true;
    try {
      opts.value = (await searchServiceObjects(props.type, q)).map((r) => ({ label: r.label, value: r.id }));
    } catch { opts.value = []; } finally { loading.value = false; }
  }, 200);
}

function onPick(v: string | null) {
  emit("pick", v, options.value.find((o) => o.value === v)?.label ?? null);
}

watch(() => props.type, () => { opts.value = []; });
</script>
