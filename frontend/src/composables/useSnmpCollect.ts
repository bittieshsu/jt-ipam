/**
 * 「立即收集」按鈕（SNMP 頁每一列、裝置詳情的 SNMP 區塊共用）。
 *
 * 送出後輪詢工作直到代理回報，再用一句話講結果（每區段讀到幾列／為什麼失敗）。
 * 每個收集目標各自記忙碌狀態：按下去的那一列反灰＋轉圈，其他列照常可按。
 */
import { getCurrentInstance, onBeforeUnmount, ref } from "vue";
import { useI18n } from "vue-i18n";
import { useMessage } from "naive-ui";
import { apiErrMsg } from "@/api/client";
import { startSnmpCollect, SNMP_SECTIONS, type SnmpJob } from "@/api/snmp";
import { useSnmpJob } from "@/composables/useSnmpJob";
import { errText, sectionLabel } from "@/utils/snmp";

export function useSnmpCollect(onDone?: (targetId: string) => void) {
  const { t } = useI18n();
  const msg = useMessage();
  const busy = ref<Record<string, boolean>>({});
  const active = new Set<{ stop: () => void }>();

  function summary(j: SnmpJob): string {
    const secs = j.run?.sections ?? {};
    return SNMP_SECTIONS.filter((s) => secs[s])
      .map((s) => `${sectionLabel(s, t)} ${t("snmp.sec_rows", { n: secs[s]?.rows ?? 0 })}`)
      .join(t("snmp.list_sep"));
  }

  function report(j: SnmpJob | null) {
    if (!j) return;
    if (j.status === "expired") { msg.warning(t("snmp.collect_expired")); return; }
    if (j.status === "failed") { msg.error(t("snmp.collect_failed", { reason: j.error || "—" })); return; }
    if (j.status !== "done") { msg.warning(t("snmp.test_no_answer")); return; }
    const run = j.run;
    if (!run || run.result === "running") { msg.info(t("snmp.collect_pending_result")); return; }
    if (run.result === "success") { msg.success(t("snmp.collect_done", { summary: summary(j) || "—" })); return; }
    const reason = errText(run.error_code, run.error) || "—";
    if (run.result === "partial") msg.warning(t("snmp.collect_partial", { reason }), { duration: 8000 });
    else msg.error(t("snmp.collect_failed", { reason }), { duration: 8000 });
  }

  async function collect(tg: { id: string; device_name?: string | null }) {
    if (busy.value[tg.id]) return;
    busy.value = { ...busy.value, [tg.id]: true };
    const job = useSnmpJob();
    active.add(job);
    try {
      const j = await job.run(async () => {
        const s = await startSnmpCollect(tg.id);
        msg.info(t("snmp.collect_started", { agent: s.agent_name ?? "—", device: tg.device_name ?? "" }));
        return s;
      });
      report(j);
      if (j) onDone?.(tg.id);
    } catch (e) {
      msg.error(apiErrMsg(e));
    } finally {
      active.delete(job);
      const next = { ...busy.value };
      delete next[tg.id];
      busy.value = next;
    }
  }

  if (getCurrentInstance()) onBeforeUnmount(() => { for (const j of active) j.stop(); });

  return { busy, collect };
}
