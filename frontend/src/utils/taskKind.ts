/**
 * 作業類型的顯示名稱（使用者 2026-10-09 看到作業頁「checkpoint_gaia.sync／checkpoint.sync」問差在哪）。
 * 類型是程式內部的名稱，畫面上一律顯示翻好的名字；沒有翻譯的（新的類型漏補）就照原樣顯示，不會空白。
 * 新增作業類型時要在三個語系的 tasks.kinds 補上名稱（backend/tests/test_task_kind_labels.py 守門）。
 */
export function taskKindKey(kind: string): string {
  return `tasks.kinds.${kind.replace(/\./g, "_")}`;
}

export function taskKindLabel(kind: string, t: (k: string) => string, te: (k: string) => boolean): string {
  const key = taskKindKey(kind);
  return te(key) ? t(key) : kind;
}
