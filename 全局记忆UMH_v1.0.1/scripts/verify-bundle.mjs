#!/usr/bin/env node
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync, lstatSync, readdirSync } from 'node:fs';
import { dirname, resolve, relative, isAbsolute } from 'node:path';
import { fileURLToPath } from 'node:url';

// 必要运行入口：缺任何一个，接收者拿到的就不是可运行的 UMH（成员数多少都不构成完整）。
const REQUIRED_ENTRIES = [
  'package/dist/apps/cli/main.js',
  'package/dist/apps/gateway/main.js',
  'package/node_modules/@modelcontextprotocol/sdk/package.json',
  'SKILL.md',
  'scripts/umh.mjs',
];

// 自描述例外（精确点名，不做整类豁免）：清单无法把自己的哈希写进自己。
const SELF_DESCRIBING = new Set(['DISTRIBUTION.json']);
const SKIP_WALK = new Set(['.git', '__pycache__', '.pytest_cache']);

export function verifyBundle(root = resolve(dirname(fileURLToPath(import.meta.url)), '..')) {
  const manifest = JSON.parse(readFileSync(resolve(root, 'DISTRIBUTION.json'), 'utf8'));
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) throw new Error('发行清单缺失或不完整');
  const base = realpathSync(root);
  const seen = new Set();
  for (const item of manifest.files) {
    if (!item.path || isAbsolute(item.path) || item.path.includes('\\') || item.path.split('/').includes('..') || /^[A-Za-z]:/.test(item.path) || seen.has(item.path)) throw new Error('非法或重复清单路径');
    seen.add(item.path);
    const path = resolve(root, item.path);
    let st;
    try { st = lstatSync(path); }
    catch { throw new Error(`文件缺失或非普通文件: ${item.path}`); }
    if (!st.isFile()) throw new Error(`文件缺失或非普通文件: ${item.path}`);
    const rel = relative(base, realpathSync(path));
    if (isAbsolute(rel) || rel === '..' || rel.startsWith('../') || rel.startsWith('..\\')) throw new Error(`路径越界: ${item.path}`);
    const hash = createHash('sha256').update(readFileSync(path)).digest('hex');
    if (hash !== item.sha256) throw new Error(`内容校验失败: ${item.path}`);
  }
  for (const required of REQUIRED_ENTRIES) {
    if (!seen.has(required)) throw new Error(`清单缺少必要项: ${required}`);
  }
  // 双向清单：盘上未申报成员同样失败（旧尺只查声明→盘上，多出文件会静默漏网）。
  const onDisk = [];
  const walk = (dir, relBase) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      if (SKIP_WALK.has(entry.name)) continue;
      const rel = relBase ? `${relBase}/${entry.name}` : entry.name;
      if (entry.isDirectory()) walk(resolve(dir, entry.name), rel);
      else if (entry.isFile()) onDisk.push(rel);
    }
  };
  walk(base, '');
  const undeclared = onDisk.filter((p) => !seen.has(p) && !SELF_DESCRIBING.has(p));
  if (undeclared.length) throw new Error(`盘上存在未申报成员: ${undeclared.slice(0, 5).join(', ')}${undeclared.length > 5 ? ` 等 ${undeclared.length} 项` : ''}`);
  return { valid: true, files: manifest.files.length, sourceCommit: manifest.sourceCommit };
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(verifyBundle())); }
  catch (err) { console.error(err.message); process.exitCode = 1; }
}
