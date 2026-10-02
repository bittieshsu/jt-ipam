<script setup lang="ts">
/**
 * 儀表板的「機櫃」卡片（放在最下面，2026-10-01 使用者要求）：直接看機櫃圖。
 *
 * 可以設定看哪個機房（那一間的全部機櫃）或挑幾個機櫃。設定跟著帳號存（user_preferences.pinned 的
 * `dash_rack_room`／`dash_racks` 兩個 namespace），換瀏覽器也一樣。沒設定時顯示一個「設定」按鈕。
 * 一次最多畫 MAX 個機櫃：機房很大時其餘的連到機櫃頁看。
 */
import { computed, ref, watch } from "vue";
import { useI18n } from "vue-i18n";
import { useRouter } from "vue-router";
import {
  NButton, NCard, NEmpty, NForm, NFormItem, NIcon, NModal, NRadio, NRadioGroup, NSelect, NSpace, NSpin, NTag,
} from "naive-ui";
import RackDiagram from "@/components/RackDiagram.vue";
import CardTitle from "@/components/CardTitle.vue";
import { getRackDiagram } from "@/api/racks";
import { usePinned } from "@/composables/usePinned";
import { RACK_DEVICE_TYPES, rackTypeColor } from "@/utils/rackColors";
import { RacksIcon, SettingsIcon } from "@/icons";

const props = defineProps<{
  locations: { id: string; name: string }[];
  racks: { id: string; name: string; location_id: string | null }[];
}>();
const { t } = useI18n();
const router = useRouter();
const MAX = 12;

const roomPin = usePinned("dash_rack_room");
const rackPin = usePinned("dash_racks");
const roomId = computed(() => roomPin.ids.value[0] ?? null);

/** 要畫的機櫃：設了機房就是那一間的全部（依名稱），否則是挑的那幾個（依挑的順序） */
const wanted = computed(() => {
  if (roomId.value) {
    return props.racks.filter((r) => r.location_id === roomId.value)
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  }
  const byId = new Map(props.racks.map((r) => [r.id, r]));
  return rackPin.ids.value.map((id) => byId.get(id)).filter(Boolean) as typeof props.racks;
});
const shown = computed(() => wanted.value.slice(0, MAX));
const configured = computed(() => !!roomId.value || rackPin.ids.value.length > 0);
const subtitle = computed(() => {
  if (roomId.value) return props.locations.find((l) => l.id === roomId.value)?.name ?? "";
  return rackPin.ids.value.length ? t("dashboard.racks_picked", { n: wanted.value.length }) : "";
});

const diagrams = ref<any[]>([]);
const loading = ref(false);
let seq = 0;
watch(() => shown.value.map((r) => r.id).join(","), async (key) => {
  const mine = ++seq;
  if (!key) { diagrams.value = []; return; }
  loading.value = true;
  const res = await Promise.allSettled(shown.value.map((r) => getRackDiagram(r.id)));
  if (mine !== seq) return;
  diagrams.value = res.flatMap((x) => (x.status === "fulfilled" ? [x.value] : []));
  loading.value = false;
}, { immediate: true });

function openRack(id: string) {
  void router.push({ name: "racks", query: { rack: id } });
}
function openRacksPage() {
  void router.push({ name: "racks" });
}

// ── 設定 ──
const show = ref(false);
const mode = ref<"room" | "racks">("room");
const formRoom = ref<string | null>(null);
const formRacks = ref<string[]>([]);
function openSettings() {
  mode.value = roomId.value || !rackPin.ids.value.length ? "room" : "racks";
  formRoom.value = roomId.value;
  formRacks.value = [...rackPin.ids.value];
  show.value = true;
}
function save() {
  if (mode.value === "room") {
    roomPin.setAll(formRoom.value ? [formRoom.value] : []);
    rackPin.setAll([]);
  } else {
    roomPin.setAll([]);
    rackPin.setAll(formRacks.value);
  }
  show.value = false;
}
const locOptions = computed(() => props.locations.map((l) => ({ label: l.name, value: l.id })));
const rackOptions = computed(() => props.racks.map((r) => ({
  label: `${r.name}${r.location_id ? `（${props.locations.find((l) => l.id === r.location_id)?.name ?? ""}）` : ""}`,
  value: r.id,
})));
</script>

<template>
  <n-card data-testid="dash-racks">
    <!-- 標題列只放標題＋數量（使用者要求：儀表板卡片標題不放按鈕、不放標題以外的文字）；
         顯示哪個機房／幾個機櫃與「設定」按鈕放內文最上方的控制列，比照 AI 巡檢卡 -->
    <template #header>
      <CardTitle :icon="RacksIcon" :text="t('dashboard.racks_title')">
        <n-tag v-if="configured && wanted.length" size="small" round :bordered="false">
          {{ wanted.length }}
        </n-tag>
      </CardTitle>
    </template>
    <div v-if="configured" class="dr-ctrl">
      <span class="dr-sub" data-testid="dash-racks-sub">{{ subtitle }}</span>
      <n-button size="small" data-testid="dash-racks-settings" @click="openSettings">
        <template #icon><n-icon><SettingsIcon /></n-icon></template>
        {{ t("dashboard.racks_settings") }}
      </n-button>
    </div>

    <n-empty v-if="!configured" :description="t('dashboard.racks_empty')">
      <template #extra>
        <n-button size="small" type="primary" data-testid="dash-racks-settings" @click="openSettings">{{ t("dashboard.racks_settings") }}</n-button>
      </template>
    </n-empty>
    <n-empty v-else-if="!wanted.length" :description="t('dashboard.racks_none')" />
    <n-spin v-else :show="loading">
      <div class="dr-row">
        <div v-for="d in diagrams" :key="d.rack_id" class="dr-rack">
          <a class="dr-name" @click="openRack(d.rack_id)">{{ d.name }}</a>
          <!-- 儀表板空間有限：精簡列高、縮小比例，一排機櫃一眼看得完 -->
          <RackDiagram :diagram="d" :show-legend="false" :controls="false" :shared-zoom="0.55" compact bare />
        </div>
      </div>
      <div class="dr-foot">
        <span class="dr-legend">
          <span v-for="ty in RACK_DEVICE_TYPES" :key="ty" class="dr-chip" :style="{ background: rackTypeColor(ty) }">{{ ty }}</span>
        </span>
        <a v-if="wanted.length > shown.length" class="dr-more" @click="openRacksPage">
          {{ t("dashboard.racks_more", { n: wanted.length - shown.length }) }}
        </a>
      </div>
    </n-spin>

    <n-modal v-model:show="show" preset="card" :title="t('dashboard.racks_settings_title')"
             style="width: 520px; max-width: calc(100vw - 32px)">
      <n-form label-placement="top">
        <n-form-item :label="t('dashboard.racks_mode')">
          <n-radio-group v-model:value="mode">
            <n-space>
              <n-radio value="room">{{ t("dashboard.racks_mode_room") }}</n-radio>
              <n-radio value="racks">{{ t("dashboard.racks_mode_racks") }}</n-radio>
            </n-space>
          </n-radio-group>
        </n-form-item>
        <n-form-item v-if="mode === 'room'" :label="t('dashboard.racks_room')">
          <n-select v-model:value="formRoom" :options="locOptions" filterable clearable data-testid="dash-racks-room" />
        </n-form-item>
        <n-form-item v-else :label="t('dashboard.racks_pick')">
          <n-select v-model:value="formRacks" :options="rackOptions" multiple filterable clearable
                    data-testid="dash-racks-pick" />
        </n-form-item>
        <div class="dr-hint">{{ t("dashboard.racks_hint", { n: MAX }) }}</div>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="show = false">{{ t("common.cancel") }}</n-button>
          <n-button type="primary" data-testid="dash-racks-save" @click="save">{{ t("common.save") }}</n-button>
        </n-space>
      </template>
    </n-modal>
  </n-card>
</template>

<style scoped>
.dr-ctrl { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.dr-sub { font-size: 12.5px; opacity: .65; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 一整排靠下對齊（落地），超出寬度左右捲 */
.dr-row { display: flex; flex-wrap: nowrap; gap: 18px; align-items: flex-end; overflow-x: auto; padding: 0 2px 8px; }
.dr-rack { flex: 0 0 auto; display: flex; flex-direction: column; align-items: center; }
.dr-name { font-weight: 600; font-size: 13px; margin-bottom: 6px; cursor: pointer;
           color: var(--primary-color, #18a058); white-space: nowrap; }
.dr-name:hover { text-decoration: underline; }
.dr-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-top: 8px; }
.dr-legend { display: flex; flex-wrap: wrap; gap: 6px; }
.dr-chip { font-size: 11px; color: #fff; padding: 1px 6px; border-radius: 4px; font-family: var(--jt-mono, monospace); }
.dr-more { font-size: 12.5px; cursor: pointer; color: var(--primary-color, #18a058); }
.dr-hint { font-size: 12px; opacity: .65; }
</style>
