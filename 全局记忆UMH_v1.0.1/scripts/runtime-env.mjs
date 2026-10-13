import { existsSync } from 'node:fs';
import { join } from 'node:path';

// PowerShell 7 启动的Node可能只继承Core模块目录，而核心ACL检查调用Windows PowerShell。
// 仅修正本次子进程，不写系统环境变量，不降低ACL检查。
export function runtimeEnv(source = process.env) {
  const env = { ...source };
  if (process.platform === 'win32') {
    const systemRoot = source.SystemRoot || source.WINDIR;
    if (systemRoot) {
      const modules = join(systemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'Modules');
      const parts = (source.PSModulePath || '').split(';').filter(Boolean);
      if (existsSync(modules)) {
        // 同名Core模块不可用于Windows PowerShell；其系统模块必须优先。
        env.PSModulePath = [modules, ...parts.filter(part => part.toLowerCase() !== modules.toLowerCase())].join(';');
      }
    }
  }
  return env;
}
