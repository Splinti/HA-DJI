// Rendert assets/icon.svg in die PNG-Größen, die Home Assistant im brand/-Ordner erwartet.
// Aufruf: npm install && npm run brand
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { Resvg } from "@resvg/resvg-js";

const root = new URL("../", import.meta.url);
const outDir = new URL("custom_components/dji_flightlog/brand/", root);

const targets = [
  { file: "icon.png", size: 256 },
  { file: "icon@2x.png", size: 512 },
];

const svg = await readFile(new URL("assets/icon.svg", root));
await mkdir(outDir, { recursive: true });

for (const { file, size } of targets) {
  const png = new Resvg(svg, { fitTo: { mode: "width", value: size } }).render().asPng();
  await writeFile(new URL(file, outDir), png);
  console.log(`${file} (${size}x${size}, ${png.length} bytes)`);
}
