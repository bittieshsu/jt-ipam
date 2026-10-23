<script setup lang="ts">
/** 戰情室的小圖示：大小跟著文字（1em），顏色跟著文字。見 wrIcons.ts。 */
import type { Component } from "vue";
defineProps<{ icon: Component }>();
</script>

<template>
  <component :is="icon" class="wr-ico" aria-hidden="true" />
</template>
