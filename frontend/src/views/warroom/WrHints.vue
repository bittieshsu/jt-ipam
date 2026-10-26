<script setup lang="ts">
/** 判斷線索：圖示＋文字逐項排開，一項不從中間折斷；最多兩排。見 hints.ts。 */
import { computed } from "vue";
import { useI18n } from "vue-i18n";
import { hintItems, type IpHints } from "./hints";
import WrIco from "./WrIco.vue";

const props = defineProps<{ hints?: IpHints }>();
const { t, te } = useI18n();
const items = computed(() => hintItems(props.hints, t, te));
// 製造商與 MAC 是同一張網卡的資訊：綁成一組不拆開，放不下就一起換到下一行（10-24 使用者）
const NIC = new Set(["vendor", "mac"]);
const lead = computed(() => items.value.filter((h) => !NIC.has(h.key)));
const nic = computed(() => items.value.filter((h) => NIC.has(h.key)));
</script>

<template>
  <div v-if="items.length" class="wr-hints" data-testid="wr-hints">
    <span v-for="h in lead" :key="h.key" class="wr-hint" :class="{ 'wr-hint--strong': h.strong, 'wr-hint--mono': h.mono }">
      <WrIco :icon="h.icon" /><span class="wr-hint__t">{{ h.text }}</span>
    </span>
    <span v-if="nic.length" class="wr-hint-group" data-testid="wr-hint-nic">
      <span v-for="h in nic" :key="h.key" class="wr-hint" :class="{ 'wr-hint--mono': h.mono }">
        <WrIco :icon="h.icon" /><span class="wr-hint__t">{{ h.text }}</span>
      </span>
    </span>
  </div>
</template>

<style>
.wr .wr-hint-group { display: inline-flex; gap: 22px; min-width: 0; max-width: 100%; white-space: nowrap; }
</style>
