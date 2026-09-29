/**
 * 全站表格的欄寬都能拖拉（2026-09-29 使用者要求：「所有有表格欄位標頭的，都要支援拖拉改變欄位寬度」）。
 *
 * 做在元件這一層、一次套上：七十幾個畫面各自在欄位定義補 `resizable` 一定會漏，之後的新畫面也會漏。
 * `NDataTable` 的 `columns` 在進入元件前先經過 `withResizable`：沒寫 `resizable` 的資料欄預設可拖拉，
 * 真的不要的欄位寫 `resizable: false`。只換掉 `columns` 這個公開屬性的值，不碰元件內部。
 */
import { computed, type SetupContext } from "vue";

type Col = Record<string, unknown>;

function mapColumn(col: Col): Col {
  if (!col || typeof col !== "object") return col;
  // 勾選欄、展開欄不是資料欄
  if (col.type === "selection" || col.type === "expand") return col;
  if (Array.isArray(col.children)) {
    return col.children.length ? { ...col, children: withResizable(col.children as Col[]) } : col;
  }
  if (col.resizable !== undefined) return col;
  // 只加 resizable，寬度相關的設定一概不動：元件會拿 width 當標頭的最小寬度，
  // 曾經在這裡補一個 48px 的 minWidth 當拖拉下限，結果蓋掉了那個最小寬度，
  // 沒有固定排版的表格在手機上就把 IP 欄擠成直排（/advanced/connections）。
  return { ...col, resizable: true };
}

export function withResizable<T>(cols: T[] | undefined | null): T[] {
  if (!Array.isArray(cols)) return cols as unknown as T[];
  return cols.map((c) => mapColumn(c as unknown as Col) as unknown as T);
}

interface SetupComponent {
  setup?: (props: Record<string, unknown>, ctx: SetupContext) => unknown;
  __jtResizable?: boolean;
}

/**
 * 包住元件的 setup，讓它看到的 `props.columns` 是 `withResizable` 之後的版本。
 * 用 computed：父層就地改動欄位（響應式）時跟著重算，跟原本的行為一樣。
 */
export function installResizableColumns(component: unknown): void {
  const comp = component as SetupComponent;
  if (!comp || comp.__jtResizable || typeof comp.setup !== "function") return;
  const original = comp.setup;
  comp.setup = function (this: unknown, props, ctx) {
    const mapped = computed(() => withResizable(props.columns as Col[] | undefined));
    const proxied = new Proxy(props, {
      get(target, key, receiver) {
        return key === "columns" ? mapped.value : Reflect.get(target, key, receiver);
      },
    });
    return original.call(this, proxied, ctx);
  };
  comp.__jtResizable = true;
}
