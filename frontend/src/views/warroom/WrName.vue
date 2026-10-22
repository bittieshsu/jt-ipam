<script setup lang="ts">
/**
 * 物件名稱：固定兩行高度（短名稱也保留兩行，同一列的圖與數字才對齊）；FQDN 在句點後、英文在連字號後
 * 可以換行（<wbr>，不改字串本身，複製出來一字不差）；兩行還放不下就省略，title 留完整名稱。
 */
import { computed } from "vue";
import { breakPoints } from "@/utils/warroom";

const props = withDefaults(defineProps<{ text: string; lines?: number }>(), { lines: 2 });
const parts = computed(() => breakPoints(props.text || ""));
</script>

<template>
  <div class="wr-name" :style="{ WebkitLineClamp: lines, minHeight: `${lines * 1.22}em` }" :title="text">
    <template v-for="(p, i) in parts" :key="i">{{ p }}<wbr v-if="i < parts.length - 1"></template>
  </div>
</template>

<style scoped>
.wr-name {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  overflow: hidden;
  overflow-wrap: anywhere;
  line-height: 1.22;
}
</style>
