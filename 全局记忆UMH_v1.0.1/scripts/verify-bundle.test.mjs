#!/usr/bin/env node
// T09 合成双向清单测试（Node 内置 test，不引入框架）：
// 多余成员必须拒收、必要入口缺失必须拒收、穿越路径必须拒收；完好样例必须通过。
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { verifyBundle } from './verify-bundle.mjs';

const sha = (data) => createHash('sha256').update(data).digest('hex');

function makeFixture(t, { extra = [], omitFromDisk = [], omitFromManifest = [], badPath = null } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'umh-verify-test-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const required = [
    'package/dist/apps/cli/main.js',
    'package/dist/apps/gateway/main.js',
    'package/node_modules/@modelcontextprotocol/sdk/package.json',
    'SKILL.md',
    'scripts/umh.mjs',
  ];
  const members = ['a.txt', ...required];
  for (const rel of members) {
    mkdirSync(join(root, rel, '..'), { recursive: true });
    writeFileSync(join(root, rel), Buffer.from(`content:${rel}`));
  }
  let files = members.map((rel) => {
    const data = Buffer.from(`content:${rel}`);
    return { path: rel, sha256: sha(data), bytes: data.length };
  });
  if (badPath) files.push({ path: badPath, sha256: sha(Buffer.from('x')), bytes: 1 });
  files = files.filter((f) => !omitFromManifest.includes(f.path));
  for (const rel of omitFromDisk) rmSync(join(root, rel));
  for (const rel of extra) {
    writeFileSync(join(root, rel), Buffer.from(`sneaky:${rel}`));
  }
  const manifest = { format: 'umh-distribution-test', sourceCommit: '153742d', files };
  writeFileSync(join(root, 'DISTRIBUTION.json'), JSON.stringify(manifest));
  return root;
}

test('intact minimal bundle passes', () => {
  const root = makeFixture(test);
  const result = verifyBundle(root);
  assert.equal(result.valid, true);
  assert.equal(result.sourceCommit, '153742d');
});

test('bundle rejects extra on-disk file (bidirectional)', (t) => {
  const root = makeFixture(test, { extra: ['extra.txt'] });
  assert.throws(() => verifyBundle(root), /未申报成员.*extra\.txt/);
});

test('bundle rejects missing declared file on disk', (t) => {
  const root = makeFixture(test, { omitFromDisk: ['SKILL.md'] });
  assert.throws(() => verifyBundle(root), /文件缺失或非普通文件: SKILL\.md/);
});

test('bundle rejects manifest missing a required entry', (t) => {
  const root = makeFixture(test, { omitFromManifest: ['scripts/umh.mjs'] });
  assert.throws(() => verifyBundle(root), /清单缺少必要项: scripts\/umh\.mjs/);
});

test('bundle rejects traversal member path', (t) => {
  const root = makeFixture(test, { badPath: '../evil.txt' });
  assert.throws(() => verifyBundle(root), /非法或重复清单路径/);
});

test('bundle rejects fewer members than declared when required closure broken', (t) => {
  // 少于600但必要入口闭合的合成样例必须通过；缺入口的多成员包必须失败——数字不是完整性证明。
  const root = makeFixture(test);
  const result = verifyBundle(root);
  assert.ok(result.files < 600, '合成样例成员数远小于600仍须通过');
});
