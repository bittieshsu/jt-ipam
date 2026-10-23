<script setup lang="ts">
/** 判斷線索：圖示＋文字逐項排開，一項不從中間折斷；最多兩排。見 hints.ts。 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { hintItems, type IpHints } from "./hints";
import WrIco from "./WrIco.vue";

const props = defineProps<{ hints?: IpHints }>();
const { t, te } = useI18n();
const items = computed(() => hintItems(props.hints, t, te));
</script>

<template>
  <div v-if="items.length" class="wr-hints" data-testid="wr-hints">
    <span v-for="h in items" :key="h.key" class="wr-hint" :class="{ 'wr-hint--strong': h.strong, 'wr-hint--mono': h.mono }">
      <WrIco :icon="h.icon" /><span class="wr-hint__t">{{ h.text }}</span>
    </span>
  </div>
</template>
