/**
 * 拖曳進來的項目：挑出真正的檔案，排除資料夾。
 *
 * **不可以用 `File.size` 判斷型別** —— macOS 交出來的資料夾 size 是 256，
 * 用 `size > 0` 篩會把資料夾當成檔案，一路送到上傳流程才在讀內容時失敗。
 * 型別要看 `DataTransferItem.webkitGetAsEntry().isFile`。
 */

export interface EntryLike {
  isFile: boolean;
}

export interface DroppedPick<T> {
  files: T[];
  skippedDirs: number;
}

/**
 * `files` 與 `entries` 在檔案拖放時索引是對齊的。
 * 拿不到 entry（某些瀏覽器／來源）時保守放行 —— 上傳前還有一道可讀性檢查。
 */
export function pickDroppedFiles<T>(
  files: T[], entries: (EntryLike | null | undefined)[],
): DroppedPick<T> {
  const kept = files.filter((_f, i) => {
    const e = entries[i];
    return e ? e.isFile : true;
  });
  return { files: kept, skippedDirs: files.length - kept.length };
}
