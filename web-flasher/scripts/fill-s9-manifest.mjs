// Usage: node scripts/fill-s9-manifest.mjs <out-dir> <model> [release]   (model: starlte | star2lte | crownlte)
// Fills size + sha256 of that model's images in site/s9-manifest.js from files in <out-dir>
// (hashes the files itself, so no SHA256 file format assumptions; compare with it by eye).
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, stat, writeFile } from 'node:fs/promises';
import { models } from '../site/s9-manifest.js';

const [dir, model, release] = process.argv.slice(2);
if (!dir || !models[model]) throw Error(`usage: fill-s9-manifest.mjs <out-dir> <${Object.keys(models).join('|')}> [release]`);
const path = new URL('../site/s9-manifest.js', import.meta.url);
const source = await readFile(path, 'utf8');
// Only edit this model's block: boot.img exists in every model.
const start = source.indexOf(`\n  ${model}: {`);
const end = source.indexOf('\n  }', start) + 4;
let block = source.slice(start, end);
for (const image of models[model].images) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(`${dir}/${image.name}`)) hash.update(chunk);
  const { size } = await stat(`${dir}/${image.name}`);
  block = block.replace(
    new RegExp(`(name: '${image.name.replace('.', '\\.')}', partition: '${image.partition}', )size: \\d+, sha256: '[0-9a-f]*'`),
    `$1size: ${size}, sha256: '${hash.digest('hex')}'`);
}
if (release) block = block.replace(/release: '[^']*'/, `release: '${release}'`);
await writeFile(path, source.slice(0, start) + block + source.slice(end));
console.log(`updated site/s9-manifest.js (${model})`);
