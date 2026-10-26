/**
 * 戰情室大數字的字體（10-24 使用者：「大數字可以換成科幻字體、設定中可以挑我們預選的幾種」）。
 *
 * 預設是 Exo 2（10-24 使用者指定）；Inter 是載不到字檔時的退路。
 * 字檔用 @fontsource 打包進前端（只取 latin 子集，大數字只有數字與 % ,），不連外部字型服務：
 * 戰情室常在沒有對外網路的機房電視上開。`scale` 補償字寬：Orbitron 這類寬體同樣字級會比 Inter 寬很多，
 * 七位數的指標會擠出版面，所以縮一點讓寬度跟 Inter 差不多（e2e 逐字體檢查不溢出）。
 */
import orbitronUrl from "@fontsource-variable/orbitron/files/orbitron-latin-wght-normal.woff2?url";
import exo2Url from "@fontsource-variable/exo-2/files/exo-2-latin-wght-normal.woff2?url";
import rajdhaniUrl from "@fontsource/rajdhani/files/rajdhani-latin-500-normal.woff2?url";
import shareTechUrl from "@fontsource/share-tech-mono/files/share-tech-mono-latin-400-normal.woff2?url";
import audiowideUrl from "@fontsource/audiowide/files/audiowide-latin-400-normal.woff2?url";

export interface BigFont { family: string; url: string | null; weight: string; scale: number }

export const NUMBER_FONTS: Record<string, BigFont> = {
  inter: { family: "WR Inter", url: null, weight: "100 900", scale: 1 },
  orbitron: { family: "WR Orbitron", url: orbitronUrl, weight: "400 900", scale: 0.78 },
  exo2: { family: "WR Exo 2", url: exo2Url, weight: "100 900", scale: 0.95 },
  rajdhani: { family: "WR Rajdhani", url: rajdhaniUrl, weight: "500", scale: 1.08 },
  "share-tech-mono": { family: "WR Share Tech Mono", url: shareTechUrl, weight: "400", scale: 0.86 },
  audiowide: { family: "WR Audiowide", url: audiowideUrl, weight: "400", scale: 0.8 },
};

const loaded = new Set<string>();

/** 載入（只載一次）並回傳 CSS 用的字體名；載不到就退回 Inter */
export async function loadNumberFont(key: string): Promise<BigFont> {
  const f = NUMBER_FONTS[key] ?? NUMBER_FONTS.inter;
  if (!f.url || loaded.has(key)) return f;
  try {
    const face = new FontFace(f.family, `url(${f.url})`, { weight: f.weight });
    (document as unknown as { fonts: FontFaceSet }).fonts.add(await face.load());
    loaded.add(key);
    return f;
  } catch {
    return NUMBER_FONTS.inter;
  }
}
