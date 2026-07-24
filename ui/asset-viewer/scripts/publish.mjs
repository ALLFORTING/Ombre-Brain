import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";


const source = fileURLToPath(new URL("../dist/index.html", import.meta.url));
const destination = fileURLToPath(
  new URL("../../../asset_viewer.html", import.meta.url),
);
const html = await readFile(source, "utf8");
const normalized = html.replace(/^[ \t]+$/gm, "");

await writeFile(destination, normalized, "utf8");