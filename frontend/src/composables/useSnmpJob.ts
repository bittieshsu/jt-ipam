/**
 * SNMP 測試／立即收集：送出之後輪詢 GET /snmp/jobs/{id}，直到代理回報（done／failed）或工作過期。
 *
 * 伺服器沒辦法主動連代理，工作要等代理下一次輪詢才領走（在線的代理一兩秒內）；
 * 按鈕在這段期間必須反灰＋轉圈（memory: feedback_busy_buttons），否則使用者會一直按。
 */
import { getCurrentInstance, onBeforeUnmount, ref } from "vue";
import { getSnmpJob, type SnmpJob, type SnmpJobStart } from "@/api/snmp";

export const SNMP_JOB_POLL_MS = 1500;
const FINISHED = new Set(["done", "failed", "expired"]);

export function isJobFinished(status: string | null | undefined): boolean {
  return FINISHED.has(String(status ?? ""));
}

export function useSnmpJob(pollMs = SNMP_JOB_POLL_MS) {
  const running = ref(false);
  const job = ref<SnmpJob | null>(null);
  const started = ref<SnmpJobStart | null>(null);
  let seq = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let wake: (() => void) | null = null;

  /** 停止輪詢：等待中的那一次立刻醒來並結束（run 回傳 null），不會留下永遠不會完成的 promise */
  function stop() {
    seq += 1;
    if (timer) clearTimeout(timer);
    timer = null;
    wake?.();
    wake = null;
    running.value = false;
  }

  function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      wake = resolve;
      timer = setTimeout(() => { wake = null; resolve(); }, ms);
    });
  }

  /**
   * 送出工作並等它結束。回傳最後一次讀到的工作（被 stop() 中斷時回 null）；送出失敗照樣丟出例外。
   * 工作過期時伺服器會把狀態改成 expired（GET 時順便處理），所以不必在這裡自己算時限 ——
   * 但仍設一個上限，避免伺服器沒回應時無限輪詢。
   */
  async function run(start: () => Promise<SnmpJobStart>): Promise<SnmpJob | null> {
    stop();
    const my = ++seq;
    running.value = true;
    job.value = null;
    try {
      const s = await start();
      if (my !== seq) return null;
      started.value = s;
      const deadline = Date.now() + Math.max(60_000, new Date(s.expires_at).getTime() - Date.now() + 60_000);
      for (;;) {
        const j = await getSnmpJob(s.job_id);
        if (my !== seq) return null;
        job.value = j;
        if (isJobFinished(j.status) || Date.now() > deadline) return j;
        await sleep(pollMs);
        if (my !== seq) return null;
      }
    } finally {
      if (my === seq) running.value = false;
    }
  }

  if (getCurrentInstance()) onBeforeUnmount(stop);

  return { running, job, started, run, stop };
}
