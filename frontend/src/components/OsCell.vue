<script setup lang="ts">
/**
 * 共用 OS 顯示格：OS 圖示 + 在地化家族名 +（來源）標註，滑鼠移上顯示原始偵測字串。
 * 與 IP 詳細資料頁完全一致（IPAddressEditModal 也用同套呈現）；OS 值來自後端 os_precedence
 * 依來源順序算出的有效 OS（os_family / os_guess / os_source）。
 */
import { NTooltip } from "naive-ui";
import { useI18n } from "vue-i18n";
import { useScanProbes, osDisplayLabel } from "@/api/scanProbes";
import OsIcon from "@/components/OsIcon.vue";

defineProps<{
  family?: string | null;
  guess?: string | null;
  source?: string | null;
  size?: number;
}>();
const { t, locale } = useI18n();
const { catalog } = useScanProbes();
</script>

<template>
  <span v-if="family" class="os-cell">
    <n-tooltip :disabled="!guess">
      <template #trigger>
        <span class="os-cell-in">
          <OsIcon :family="family" :size="size || 16" />
          <!-- 家族對不到但有原文（例如某個對照表還不認得的發行版）：顯示原文，比「未知」有用（2026-10-16） -->
          <span class="os-cell-raw">{{ osDisplayLabel(catalog.os_families, family, guess, locale) }}</span>
          <span v-if="source" class="os-cell-src">{{
            "（" + t("os_precedence.source_label") + ": " + t("os_precedence.src_" + source) + "）"
          }}</span>
        </span>
      </template>
      {{ guess }}
    </n-tooltip>
  </span>
  <span v-else class="os-cell-none">—</span>
</template>

<style scoped>
.os-cell { display: inline-flex; align-items: center; white-space: nowrap; }
.os-cell-in { display: inline-flex; align-items: center; gap: 5px; }
.os-cell-src { opacity: .6; font-size: .85em; }
.os-cell-raw { max-width: 22em; overflow: hidden; text-overflow: ellipsis; }
.os-cell-none { opacity: .5; }
</style>
