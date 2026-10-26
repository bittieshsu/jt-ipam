<template>
  <!-- 網址不存在（打錯、舊書籤、功能改名）：以前整頁空白，看起來像當掉（2026-10-25 實測） -->
  <n-card :bordered="false" data-testid="not-found">
    <n-result status="404" :title="t('not_found.title')" :description="t('not_found.desc', { path: route.fullPath })">
      <template #footer>
        <n-space justify="center">
          <n-button @click="router.back()">
            <template #icon><n-icon><ArrowLeftIcon /></n-icon></template>{{ t("common.back") }}
          </n-button>
          <n-button type="primary" @click="router.push({ name: 'dashboard' })">
            <template #icon><n-icon><HomeIcon /></n-icon></template>{{ t("not_found.home") }}
          </n-button>
        </n-space>
      </template>
    </n-result>
  </n-card>
</template>

<script setup lang="ts">
import { useI18n } from "vue-i18n";
import { useRoute, useRouter } from "vue-router";
import { NButton, NCard, NIcon, NResult, NSpace } from "naive-ui";
import { ArrowLeft as ArrowLeftIcon, Home as HomeIcon } from "@iconoir/vue";

const { t } = useI18n();
const route = useRoute();
const router = useRouter();
</script>
