<script setup lang="ts">
/**
 * 全站搜尋的設定（管理 → 系統設定，使用者 2026-10-13）：
 *  - 隨打即找：邊打邊出結果；關掉就按 Enter 才搜尋
 *  - 搜哪些類型、每類比對哪些欄位（只能從允許清單挑）
 *  - 結果的排列順序
 * 權限不變：看不到的資料照樣看不到。AI 對話的搜尋工具不受影響。
 *
 * 排序照既有的做法，不另做一套（使用者 2026-10-13）：外觀是來源優先序頁的排序清單（序號、把手、名稱、
 * 右邊的啟用開關），操作是欄位選擇器的拖拉（pointer events，觸控也拖得動；把手聚焦後按上下方向鍵移動一格）。
 * 欄位用固定寬度的格線排，各列的欄位上下對齊。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import { useI18n } from "vue-i18n";
import { NButton, NCheckbox, NCheckboxGroup, NIcon, NSpace, NSwitch, NAlert, useMessage } from "naive-ui";
import { DragHandleIcon, RestoreIcon, SaveIcon } from "@/icons";
import { apiErrMsg } from "@/api/client";
import { getSearchSettings, putSearchSettings, type SearchTypeSetting } from "@/api/search";
import { moveItem } from "@/utils/columnOrder";

const { t } = useI18n();
const msg = useMessage();

const instant = ref(true);
const types = ref<SearchTypeSetting[]>([]);
const catalog = ref<Record<string, string[]>>({});
const saving = ref(false);
const loaded = ref(false);

onMounted(async () => {
  try {
    const s = await getSearchSettings();
    instant.value = s.instant;
    types.value = s.types.map((x) => ({ ...x, fields: [...x.fields] }));
    catalog.value = s.catalog;
    loaded.value = true;
  } catch (e) { msg.error(apiErrMsg(e)); }
});

function resetDefaults() {
  instant.value = true;
  types.value = Object.entries(catalog.value).map(([type, fields]) => ({ type, enabled: true, fields: [...fields] }));
}

// ── 排序：跟 ColumnPicker 同一套（拖拉中只改暫定順序，放開才寫回） ─────────────────
const listEl = ref<HTMLElement | null>(null);
const dragType = ref<string | null>(null);
const draft = ref<SearchTypeSetting[] | null>(null);
const shown = computed(() => draft.value ?? types.value);

// 移動與放開聽在 window 上，不靠把手的 setPointerCapture：拖拉中清單即時重排，往上拖時被拖的那一列會被
// 搬到新位置，元素一被搬動，瀏覽器就取消它的指標擷取 —— 之後的移動與放開都收不到，只移一半、放開也沒寫回
//（寫 e2e 時抓到）。
function onPointerDown(e: PointerEvent, type: string) {
  if (e.pointerType === "mouse" && e.button !== 0) return;
  e.preventDefault();
  dragType.value = type;
  draft.value = types.value.slice();
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerUp);
  window.addEventListener("pointercancel", onPointerCancel);
}
function stopListening() {
  window.removeEventListener("pointermove", onPointerMove);
  window.removeEventListener("pointerup", onPointerUp);
  window.removeEventListener("pointercancel", onPointerCancel);
}
onBeforeUnmount(stopListening);
// 清單比畫面高：拖到上下邊緣時頁面跟著捲（比照欄位選擇器的 autoScroll，這裡捲的是整個頁面）
function autoScroll(clientY: number) {
  if (clientY < 48) window.scrollBy(0, -16);
  else if (clientY > window.innerHeight - 48) window.scrollBy(0, 16);
}
function onPointerMove(e: PointerEvent) {
  if (dragType.value === null || !draft.value || !listEl.value) return;
  autoScroll(e.clientY);
  const rows = Array.from(listEl.value.querySelectorAll<HTMLElement>("[data-search-type]"));
  if (!rows.length) return;
  let over = rows.length - 1;
  for (let i = 0; i < rows.length; i++) {
    if (e.clientY < rows[i].getBoundingClientRect().bottom) { over = i; break; }
  }
  const from = draft.value.findIndex((x) => x.type === dragType.value);
  if (from >= 0 && over !== from) draft.value = moveItem(draft.value, from, over);
}
function onPointerUp() {
  stopListening();
  if (draft.value) types.value = draft.value;
  dragType.value = null;
  draft.value = null;
}
function onPointerCancel() {
  stopListening();
  dragType.value = null;
  draft.value = null;
}
// 鍵盤：把手聚焦後按上／下方向鍵移動一格
function onHandleKey(e: KeyboardEvent, type: string) {
  const delta = e.key === "ArrowUp" ? -1 : e.key === "ArrowDown" ? 1 : 0;
  if (!delta) return;
  e.preventDefault();
  const from = types.value.findIndex((x) => x.type === type);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= types.value.length) return;
  types.value = moveItem(types.value, from, to);
  void nextTick(() => listEl.value?.querySelector<HTMLElement>(`[data-search-type="${type}"] .gss-drag`)?.focus());
}

// 存不了的情況當場講：一個類型都沒開、開了的類型一個欄位都沒勾（後端也會擋）
const noType = computed(() => loaded.value && !types.value.some((x) => x.enabled));
const noField = computed(() => types.value.filter((x) => x.enabled && !x.fields.length));

async function save() {
  saving.value = true;
  try {
    const s = await putSearchSettings({ instant: instant.value, types: types.value });
    types.value = s.types.map((x) => ({ ...x, fields: [...x.fields] }));
    msg.success(t("search_settings.saved"));
  } catch (e) { msg.error(apiErrMsg(e)); }
  finally { saving.value = false; }
}
</script>

<template>
  <div class="gss" data-testid="search-settings">
    <div class="fld">
      <label>{{ t("search_settings.instant") }}</label>
      <n-switch v-model:value="instant" data-testid="search-settings-instant" />
      <div class="hint">{{ t("search_settings.instant_hint") }}</div>
    </div>

    <div class="fld">
      <label>{{ t("search_settings.types") }}</label>
      <div class="hint">{{ t("search_settings.types_hint") }}</div>
      <ul ref="listEl" class="rank-list" :class="{ 'is-dragging': dragType !== null }">
        <li v-for="(x, i) in shown" :key="x.type" class="rank-item"
            :class="{ 'is-drag-source': x.type === dragType, disabled: !x.enabled }"
            :data-search-type="x.type" :data-testid="`search-type-${x.type}`">
          <span class="rank-num">{{ i + 1 }}</span>
          <button type="button" class="gss-drag"
                  :title="t('search_settings.drag_hint')"
                  :aria-label="t('column_picker.drag_aria', { label: t(`search_settings.type.${x.type}`) })"
                  @pointerdown="onPointerDown($event, x.type)"
                  @keydown="onHandleKey($event, x.type)">
            <n-icon :size="14"><DragHandleIcon /></n-icon>
          </button>
          <span class="rank-label">{{ t(`search_settings.type.${x.type}`) }}</span>
          <n-checkbox-group v-model:value="x.fields" :disabled="!x.enabled" class="gss-fields">
            <n-checkbox v-for="f in catalog[x.type] || []" :key="f" :value="f"
                        :label="t(`search_settings.field.${f}`)" />
          </n-checkbox-group>
          <n-switch v-model:value="x.enabled" size="small" class="gss-switch"
                    :data-testid="`search-type-enabled-${x.type}`">
            <template #checked>{{ t("hostnameSrc.enabled") }}</template>
            <template #unchecked>{{ t("hostnameSrc.disabled") }}</template>
          </n-switch>
        </li>
      </ul>
    </div>

    <n-alert v-if="noType" type="warning" :show-icon="true" data-testid="search-settings-no-type">
      {{ t("search_settings.need_type") }}
    </n-alert>
    <n-alert v-else-if="noField.length" type="warning" :show-icon="true" data-testid="search-settings-no-field">
      {{ t("search_settings.need_field", { types: noField.map((x) => t(`search_settings.type.${x.type}`)).join("、") }) }}
    </n-alert>

    <n-space justify="end">
      <!-- 設定讀回來之前不可以按：允許清單還是空的，會還原成空清單，接著讀取完成又被蓋回舊設定 -->
      <n-button :disabled="!loaded" data-testid="search-settings-reset" @click="resetDefaults">
        <template #icon><n-icon :component="RestoreIcon" /></template>{{ t("search_settings.reset") }}
      </n-button>
      <n-button type="primary" :loading="saving" :disabled="saving || !loaded || noType || noField.length > 0"
                data-testid="search-settings-save" @click="save">
        <template #icon><n-icon :component="SaveIcon" /></template>{{ t("common.save") }}
      </n-button>
    </n-space>
  </div>
</template>

<style scoped>
.gss { display: flex; flex-direction: column; gap: 14px; }
.fld { display: flex; flex-direction: column; gap: 4px; }
.fld > label { font-weight: 600; }
.fld > :deep(.n-switch) { align-self: flex-start; }
.hint { font-size: 12px; opacity: 0.7; }

/* 排序清單：比照來源優先序頁（HostnamePrecedence.vue）的 rank-list */
.rank-list { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-direction: column; gap: 6px; }
.rank-list.is-dragging { user-select: none; cursor: grabbing; }
.rank-item {
  display: grid;
  /* 序號、把手、類型名稱、欄位、開關：每一列同樣的欄寬，欄位才會上下對齊 */
  grid-template-columns: 22px 20px 9.5em 1fr auto;
  align-items: center; gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--n-border-color, rgba(127, 127, 127, 0.25));
  border-radius: 6px;
  background: var(--n-color, transparent);
}
.rank-item.is-drag-source { background: rgba(127, 127, 127, 0.16); }
.rank-item.disabled .rank-label, .rank-item.disabled .gss-fields { opacity: 0.45; }
.rank-item.disabled .rank-num { background: var(--n-text-color-3, #888); }
.rank-num {
  width: 22px; height: 22px;
  display: inline-flex; align-items: center; justify-content: center;
  border-radius: 50%;
  background: var(--primary-color, #18a058); color: #fff;
  font-size: 12px; font-weight: 600;
}
.rank-label { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
/* 把手：比照欄位選擇器（ColumnPicker.vue）的 .col-drag */
.gss-drag {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; padding: 0; border: 0; border-radius: 4px;
  background: transparent; color: inherit; opacity: 0.5;
  cursor: grab; touch-action: none; user-select: none;
}
.gss-drag:hover, .gss-drag:focus-visible { opacity: 1; background: rgba(127, 127, 127, 0.14); }
.gss-drag:focus-visible { outline: 2px solid rgba(24, 160, 88, 0.6); outline-offset: 1px; }
.rank-list.is-dragging .gss-drag { cursor: grabbing; }
/* 欄位：固定寬度的格子，各列同一個位置就是同一欄 */
.gss-fields { display: grid; grid-template-columns: repeat(auto-fill, 7.5em); row-gap: 4px; column-gap: 8px; }
@media (max-width: 720px) {
  /* 手機：第一行序號、把手、名稱、開關；欄位放第二行橫跨整列 */
  .rank-item { grid-template-columns: 22px 20px 1fr auto; }
  .gss-switch { grid-column: 4; grid-row: 1; }
  .gss-fields { grid-column: 1 / -1; grid-row: 2; }
}
</style>
