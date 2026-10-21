import { describe, expect, it, vi } from "vitest";
import { installSelectionCellClick } from "../selectionCellClick";

function table(disabled = false) {
  document.body.innerHTML = `
    <table><tbody><tr id="row">
      <td class="n-data-table-td n-data-table-td--selection" id="cell">
        <div class="n-checkbox${disabled ? " n-checkbox--disabled" : ""}" id="box"></div>
      </td>
      <td class="n-data-table-td" id="ip">10.20.0.33</td>
    </tr></tbody></table>`;
  const rowClick = vi.fn();
  const boxClick = vi.fn();
  // 跟各清單頁的 row-props 一樣：來自勾選框的點擊不開詳細頁
  document.getElementById("row")!.addEventListener("click", (e) => {
    if (!(e.target as HTMLElement).closest(".n-checkbox")) rowClick();
  });
  document.getElementById("box")!.addEventListener("click", boxClick);
  return { rowClick, boxClick };
}

describe("表格勾選欄整格可點（使用者 2026-10-20）", () => {
  installSelectionCellClick(document);
  it("點勾選格的空白處：勾選，不觸發整列點擊", () => {
    const { rowClick, boxClick } = table();
    document.getElementById("cell")!.click();
    expect(boxClick).toHaveBeenCalledTimes(1);
    expect(rowClick).not.toHaveBeenCalled();
  });
  it("點勾選框本身照舊交給元件", () => {
    const { boxClick } = table();
    document.getElementById("box")!.click();
    expect(boxClick).toHaveBeenCalledTimes(1);
  });
  it("點其他欄照舊是整列點擊", () => {
    const { rowClick, boxClick } = table();
    document.getElementById("ip")!.click();
    expect(rowClick).toHaveBeenCalledTimes(1);
    expect(boxClick).not.toHaveBeenCalled();
  });
  it("勾選框停用時，點旁邊也不會變成進頁面", () => {
    const { rowClick, boxClick } = table(true);
    document.getElementById("cell")!.click();
    expect(boxClick).not.toHaveBeenCalled();
    expect(rowClick).not.toHaveBeenCalled();
  });
});
