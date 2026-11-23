#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { existsSync, readdirSync, readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { runtimeEnv } from './runtime-env.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const entry = join(root, 'package', 'dist', 'apps', 'cli', 'main.js');
const args = process.argv.slice(2);
if (!existsSync(entry)) { console.error('缺少运行程序，请重新解压完整技能目录。'); process.exit(1); }
if (!args.length || ['--help', '-h'].includes(args[0])) {
  console.log('用法: node scripts/umh.mjs <init|start|stop|status|doctor|connect|disconnect|review|backup|restore|...> [参数]');
  console.log('默认数据根: 当前用户家目录/.umh；可用 --umh-home <绝对路径> 或 UMH_HOME 覆盖。');
  process.exit(0);
}
const homeIndex = args.indexOf('--umh-home');
if (homeIndex >= 0 && (!args[homeIndex + 1] || !isAbsolute(args[homeIndex + 1]))) {
  console.error('--umh-home 必须是绝对路径。'); process.exit(2);
}
// apply 从已审阅plan读取根，避免额外参数改变其契约。
if (homeIndex < 0 && !(args[0] === 'connect' && args[1] === 'apply')) {
  const home = process.env.UMH_HOME || join(homedir(), '.umh');
  if (!isAbsolute(home)) { console.error('UMH_HOME必须是绝对路径。'); process.exit(2); }
  args.push('--umh-home', home);
}
// 当前核心preview会替换同一连接的登记；在可分发入口阻止丢失活动回执。
if (args[0] === 'connect' && args.includes('--dry-run')) {
  const wi = args.indexOf('--workspace');
  const hi = args.indexOf('--umh-home');
  const workspace = args[wi + 1];
  const home = args[hi + 1];
  const canonical = value => {
    let path = resolve(value);
    if (existsSync(path)) path = realpathSync(path);
    return process.platform === 'win32' ? path.toLowerCase() : path;
  };
  const records = join(home, 'runtime', 'connections');
  if (wi >= 0 && workspace && existsSync(records)) {
    try {
      for (const file of readdirSync(records).filter(name => name.endsWith('.json'))) {
        const record = JSON.parse(readFileSync(join(records, file), 'utf8'));
        if (record.clientId !== args[1] || ['removed','prepared'].includes(record.state)) continue;
        const binding = JSON.parse(readFileSync(record.bindingPath, 'utf8'));
        if (binding.workspace && canonical(binding.workspace) === canonical(workspace)) {
          console.error(`已有该客户端/工作区连接 ${record.connectionId}，请先安全disconnect，再生成新计划；拒绝覆盖活动回执。`);
          process.exit(2);
        }
      }
    } catch (err) { console.error(`无法核实已有连接，停止preview: ${err.message}`); process.exit(2); }
  }
}
const child = spawn(process.execPath, [entry, ...args], { stdio: 'inherit', windowsHide: true, env: runtimeEnv() });
child.on('error', err => { console.error(err.message); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
