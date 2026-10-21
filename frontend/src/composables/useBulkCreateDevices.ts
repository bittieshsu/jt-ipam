import { ref } from "vue";
import { useI18n } from "vue-i18n";
import { useMessage } from "naive-ui";
import { bulkCreateDevices, type BulkCreateDevicesResult } from "@/api/addresses";

/**
 * 「批次增加裝置」（使用者 2026-10-20）：IP 位址頁與子網路的 IP 頁共用同一份邏輯與訊息。
 * 已經有裝置的 IP 跳過；沒有的，對得到既有裝置就連過去，否則以主機名稱建一台新裝置。
 * 用法：按鈕 `:loading="creatingDevices"`，確認後呼叫 `createDevices(ids, 重新載入)`。
 */
export function useBulkCreateDevices() {
  const { t } = useI18n();
  const msg = useMessage();
  const creatingDevices = ref(false);

  async function createDevices(ids: string[], after?: () => unknown): Promise<BulkCreateDevicesResult | null> {
    if (!ids.length || creatingDevices.value) return null;
    creatingDevices.value = true;
    try {
      const res = await bulkCreateDevices(ids);
      const text = t("addresses.bulk_devices_done", {
        created: res.created, linked: res.linked_existing, skipped: res.skipped,
      });
      if (res.failed) msg.warning(`${text}${t("addresses.bulk_devices_failed", { n: res.failed })}`, { duration: 8000 });
      else msg.success(text, { duration: 6000 });
      await after?.();
      return res;
    } catch (e: any) {
      msg.error(e?.response?.data?.detail ?? t("errors.server"));
      return null;
    } finally {
      creatingDevices.value = false;
    }
  }

  return { creatingDevices, createDevices };
}
