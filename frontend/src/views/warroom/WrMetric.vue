<script setup lang="ts">
/** 大指標：細字重的大數字＋標籤。value 為 null＝沒有權限，顯示小字「無權限」而不是 0
 *  （以前是一條大「—」，比標籤高、看起來像是另一種東西，使用者 10-22 指出）。
 *  naText：null 的原因不是權限時（例如「未設定」），改顯示這段文字。
 *
 *  數字變了就翻牌（使用者 10-26）：變大時新的數字由下往上翻上來，變小時由上往下翻下來；只翻有變的那幾位，
 *  由右往左錯開一點。換主題時元件會重建（父層的 key 帶主題），不會把別的主題的數字當成變化。降低動畫時直接換。 */
import { computed, inject, onBeforeUnmount, ref, watch, type Ref } from "vue";
import { useI18n } from "vue-i18n";
const props = defineProps<{ value: number | string | null | undefined; label: string; tone?: "accent" | "lilac" | "coral" | "ink";
  naText?: string }>();
const { t } = useI18n();
const reduce = inject<Ref<boolean>>("wrReduceMotion", ref(false));

const fmt = (v: number) => v.toLocaleString();
const prev = ref<string | null>(null);
const dir = ref<"up" | "down">("up");
const roll = ref(0);
let clear: ReturnType<typeof setTimeout> | null = null;
watch(() => props.value, (v, old) => {
  if (typeof v !== "number" || typeof old !== "number" || v === old || reduce.value) return;
  prev.value = fmt(old);
  dir.value = v > old ? "up" : "down";
  roll.value += 1;
  if (clear) clearTimeout(clear);
  clear = setTimeout(() => { prev.value = null; }, 1400);
});
onBeforeUnmount(() => { if (clear) clearTimeout(clear); });

/** 每一位：跟舊的數字靠右對齊比較，不一樣的那一位帶著舊字一起翻 */
const cells = computed(() => {
  const now = typeof props.value === "number" ? fmt(props.value) : String(props.value ?? "");
  if (prev.value == null) return [...now].map((c) => ({ c, o: null as string | null, d: 0 }));
  const a = [...now], b = [...prev.value];
  const n = Math.max(a.length, b.length);
  const out: { c: string; o: string | null; d: number }[] = [];
  for (let i = 0; i < n; i++) {
    const c = a[a.length - n + i] ?? "", o = b[b.length - n + i] ?? "";
    if (!c) continue;                                       // 位數變少：多出來的舊位數直接消失
    out.push({ c, o: c === o ? null : o, d: (n - 1 - i) * 60 });
  }
  return out;
});
</script>

<template>
  <div class="wr-metric" :class="`t-${tone ?? 'ink'}`">
    <div v-if="value == null" class="wr-metric__na">{{ naText ?? t("warroom.no_access_short") }}</div>
    <!-- 「—」＝沒有可用的結果（例如 AI 巡檢未執行或失敗）：同樣用小字說明，不畫一條大橫線、也不是 0 -->
    <div v-else-if="value === '—'" class="wr-metric__na" data-testid="wr-metric-none">{{ t("warroom.ai_no_result") }}</div>
    <div v-else class="wr-metric__v" :class="`roll-${dir}`" data-testid="wr-metric-v"><template v-for="(x, i) in cells" :key="`${roll}:${i}`"><span v-if="x.o !== null" class="wr-d" :style="{ '--d': `${x.d}ms` }"><span class="wr-d__o" aria-hidden="true">{{ x.o || "\u00a0" }}</span><span class="wr-d__n">{{ x.c }}</span></span><template v-else>{{ x.c }}</template></template></div>
    <div class="wr-metric__l">{{ label }}</div>
  </div>
</template>

<style scoped>
.wr-metric { display: flex; align-items: flex-end; gap: 16px; min-width: 0; }
.wr-metric__v { font-family: var(--wr-num); font-weight: 300; font-size: 104px; line-height: .9;
  font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
/* 平均斷行：不會只剩一個字掉到第二行（「DNS 不一致群 / 組」） */
.wr-metric__l { font-size: 28px; font-weight: 600; color: var(--wr-ink); max-width: 280px; min-width: 4.5em; line-height: 1.15;
  overflow-wrap: break-word; text-wrap: balance; }
.wr-metric__na { font-size: 28px; color: var(--wr-muted); line-height: 1.15; white-space: nowrap; }
.t-accent .wr-metric__v { color: var(--wr-accent); }
.t-lilac .wr-metric__v { color: var(--wr-lilac); }
.t-coral .wr-metric__v { color: var(--wr-coral); }
.t-ink .wr-metric__v { color: var(--wr-ink); }
/* 翻牌：每一位是一個裁切窗，舊字與新字一起移動；帶一下光暈像機械翻牌被點亮 */
.wr-d { display: inline-block; position: relative; overflow: hidden; vertical-align: top; line-height: inherit; }
.wr-d__n, .wr-d__o { display: inline-block; animation-duration: .7s; animation-delay: var(--d, 0ms);
  animation-timing-function: cubic-bezier(.2, .8, .2, 1); animation-fill-mode: both; }
.wr-d__o { position: absolute; left: 0; top: 0; }
.roll-up .wr-d__n { animation-name: wr-in-up; }
.roll-up .wr-d__o { animation-name: wr-out-up; }
.roll-down .wr-d__n { animation-name: wr-in-down; }
.roll-down .wr-d__o { animation-name: wr-out-down; }
@keyframes wr-in-up { from { transform: translateY(100%); filter: blur(2px); } 70% { text-shadow: 0 0 22px currentColor; filter: none; }
  to { transform: none; text-shadow: none; } }
@keyframes wr-out-up { from { transform: none; opacity: 1; } to { transform: translateY(-100%); opacity: 0; } }
@keyframes wr-in-down { from { transform: translateY(-100%); filter: blur(2px); } 70% { text-shadow: 0 0 22px currentColor; filter: none; }
  to { transform: none; text-shadow: none; } }
@keyframes wr-out-down { from { transform: none; opacity: 1; } to { transform: translateY(100%); opacity: 0; } }
</style>
