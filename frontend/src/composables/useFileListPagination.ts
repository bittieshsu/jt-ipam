import { computed, reactive, type Ref } from "vue";
import { useI18n } from "vue-i18n";

/**
 * 遠端檔案清單（SFTP、RustDesk）的分頁（使用者 10-22：一頁 100 筆時看不出下面還有檔案）。
 *
 * - 每頁預設 1000 筆：清單是虛擬捲動，筆數多也不會卡；不跟全站表格的每頁筆數偏好綁在一起
 *   （表格看 100 筆剛好，目錄一般都比較長）
 * - 頁碼受控：超過一頁時，清單左下角放「還有 N 筆在下一頁」的按鈕（`remaining`／`next`）
 */
export function useFileListPagination(total: Ref<number>) {
  const { t } = useI18n();
  const pg = reactive({
    page: 1,
    pageSize: 1000,
    showSizePicker: true,
    pageSizes: [100, 500, 1000, 5000],
    prefix: (info: { itemCount?: number }) => t("common.total_rows", { n: info.itemCount ?? 0 }),
    onUpdatePage: (p: number) => { pg.page = p; },
    onUpdatePageSize: (ps: number) => { pg.pageSize = ps; pg.page = 1; },
  });
  const remaining = computed(() => Math.max(0, total.value - pg.page * pg.pageSize));
  const nextCount = computed(() => Math.min(remaining.value, pg.pageSize));
  function next() { if (remaining.value > 0) pg.page += 1; }
  function reset() { pg.page = 1; }
  return { pagination: pg, remaining, nextCount, next, reset };
}
