#!/usr/bin/env node
import { spawnSync } from 'node:child_process';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { verifyBundle } from './verify-bundle.mjs';
import { runtimeEnv } from './runtime-env.mjs';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
try {
  if (args.some(arg => arg.startsWith('--') && !['--check', '--init', '--umh-home', '--python'].includes(arg))) throw new Error('仅支持 --check / --init / --umh-home / --python');
  if (!args.includes('--check') && !args.includes('--init')) throw new Error('先运行 --check；需要初始化时显式使用 --init。');
  const [major, minor] = process.versions.node.split('.').map(Number);
  if (major < 24 || (major === 24 && minor < 2)) throw new Error('需要Node >=24.2');
  const pyIndex = args.indexOf('--python');
  const python = pyIndex >= 0 ? args[pyIndex + 1] : 'python';
  if (!python || python.startsWith('--')) throw new Error('--python缺少路径');
  const py = spawnSync(python, ['-X', 'utf8', '-c', 'import sys; print("%s.%s" % sys.version_info[:2])'], { encoding: 'utf8', windowsHide: true, timeout: 10000, env: runtimeEnv() });
  const match = (py.stdout || '').trim().match(/^(\d+)\.(\d+)$/);
  if (py.status !== 0 || !match || Number(match[1]) < 3 || (Number(match[1]) === 3 && Number(match[2]) < 12)) throw new Error('需要可用的Python >=3.12（依 DEPENDENCIES-PIN）；本脚本不会下载安装。');
  if (process.platform === 'win32') {
    const acl = spawnSync('powershell.exe', ['-NoProfile','-NonInteractive','-Command','Get-Command Get-Acl -ErrorAction Stop | Out-Null'], { encoding:'utf8', windowsHide:true, timeout:10000, env:runtimeEnv() });
    if (acl.status !== 0) throw new Error('Windows PowerShell ACL模块不可用，无法安全启动服务。');
  }
  const bundle = verifyBundle(root);
  const homeIndex = args.indexOf('--umh-home');
  if (homeIndex >= 0 && (!args[homeIndex + 1] || !isAbsolute(args[homeIndex + 1]))) throw new Error('--umh-home必须是绝对路径');
  console.log(JSON.stringify({ ...bundle, node: process.versions.node, python: match[0], initialized: false }));
  if (args.includes('--init')) {
    const cmd = [join(root, 'scripts', 'umh.mjs'), 'init'];
    if (homeIndex >= 0) cmd.push('--umh-home', args[homeIndex + 1]);
    const result = spawnSync(process.execPath, cmd, { stdio: 'inherit', windowsHide: true });
    process.exitCode = result.status ?? 1;
  }
} catch (err) { console.error(err.message); process.exitCode = 1; }
