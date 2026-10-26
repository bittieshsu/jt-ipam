<script setup lang="ts">
/**
 * 12 AI 情報的畫面效果層（點陣、掃描線、雜點、電影掃描帶與暗角）。
 * 使用者 10-26：「效果要全版面都有效，不是只播報區」、「掃描線很不明顯」→ 從播報區搬出來，蓋在整個戰情室畫布上
 * （頁首、指標、頁尾都有）；設定頁的預覽也用這一層。不接收滑鼠事件，按鍵說明（z-index 較高）蓋在它上面。
 * 掃描線是 CRT 式的暗線：在亮字上也看得出來，但不會讓字讀不到；降低動畫時沒有任何移動。
 */
defineProps<{ effects: string; scanlines: string; noise: string; reduce?: boolean }>();
</script>

<template>
  <div class="wr-fx" :class="[`fx-${effects}`, `sl-${scanlines}`, `nz-${noise}`, { reduce }]" aria-hidden="true"
       data-testid="wr-fx">
    <i class="wr-fx__dots" /><i class="wr-fx__lines" /><i class="wr-fx__noise" /><i class="wr-fx__band" /><i class="wr-fx__vig" />
  </div>
</template>

<style scoped>
.wr-fx { position: absolute; inset: 0; pointer-events: none; z-index: 20; overflow: hidden; }
.wr-fx > i { position: absolute; inset: 0; display: none; }
/* 點陣：標準、電影 */
.fx-standard .wr-fx__dots, .fx-cinema .wr-fx__dots { display: block; opacity: .5; mix-blend-mode: screen;
  background-image: radial-gradient(rgba(136, 235, 193, .16) 1px, transparent 1.5px); background-size: 22px 22px; }
/* 掃描線：低＝每 3px 一條 1px 暗線；中＝每 4px 一條 2px 更深的暗線，再加一條淡亮線 */
.sl-low .wr-fx__lines { display: block;
  background: repeating-linear-gradient(0deg, rgba(0, 0, 0, .26) 0 1px, transparent 1px 3px); }
.sl-medium .wr-fx__lines { display: block;
  background: repeating-linear-gradient(0deg, rgba(0, 0, 0, .42) 0 2px, rgba(255, 255, 255, .035) 2px 3px, transparent 3px 4px); }
/* 雜點：細顆粒，輕微跳動像底片顆粒（只換位置，不閃亮度） */
.nz-low .wr-fx__noise { display: block; inset: -60px; opacity: .1; mix-blend-mode: overlay;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='2' stitchTiles='stitch'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
  animation: wr-grain .9s steps(4) infinite; }
@keyframes wr-grain { 0% { transform: translate(0, 0); } 25% { transform: translate(-22px, 14px); }
  50% { transform: translate(18px, -26px); } 75% { transform: translate(-12px, -8px); } 100% { transform: translate(0, 0); } }
/* 電影：一道寬的亮帶由上往下慢慢掃過整個畫面，四周暗角 */
.fx-cinema .wr-fx__band { display: block; top: -30%; bottom: auto; height: 22%;
  background: linear-gradient(180deg, transparent, rgba(136, 235, 193, .07) 45%, rgba(136, 235, 193, .12) 50%, rgba(136, 235, 193, .07) 55%, transparent);
  animation: wr-band 7s linear infinite; }
@keyframes wr-band { to { top: 110%; } }
.fx-cinema .wr-fx__vig { display: block; background: radial-gradient(ellipse at center, transparent 58%, rgba(0, 0, 0, .45) 100%); }
.reduce .wr-fx__noise, .reduce .wr-fx__band { animation: none; }
.reduce .wr-fx__band { display: none; }
</style>
