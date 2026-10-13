/**
 * packages/scope/identity — 项目身份解析（C03）。
 *
 * 设计 §6 身份规则（冻结）：
 *   优先级：规范化 Git remote →（无 remote）规范化 Git common root →（非 Git）规范化绝对路径。
 *   - realpath 归一；Windows 统一大小写与分隔符（Windows 默认 FS 大小写不敏感，整串小写）。
 *   - Git remote：去用户凭据、统一 HTTPS/SSH host 形态、去尾 `.git` 与斜杠。
 *   - remote 不可安全解析 / .git 损坏 → 明确 UmhError，绝不把损坏 authority 静默降级为路径身份。
 *   - projectId = 'prj_' + SHA256(PROJECT_ID_NAMESPACE + normalizedIdentity) 的小写 hex（64 位）。
 *
 * normalizedIdentity 带 source 域标签（git-remote:/git-root:/path:），
 * 保证 remote 形态、git-root 形态与普通路径形态的身份空间互不混同：
 * 同一目录的 remote 身份与路径身份不会因字符串巧合而碰撞。
 *
 * PROJECT_ID_NAMESPACE 是固定 ASCII 常量，只在身份配方升级时整体变更（届时历史 projectId 迁移由管理入口处理）。
 *
 * 绑定不变量（本模块配合 C07/C08 落实）：项目身份一旦绑定到活动 Gateway 会话即固定，
 * 配置重编辑不得把活动会话悄悄重绑到另一项目；显式拆分/合并身份只由 admin 经管理入口执行。
 */
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { realpath, stat } from 'node:fs/promises';
import { dirname, isAbsolute, join, resolve as resolvePath } from 'node:path';
import process from 'node:process';
import { UmhError } from '../contracts/index.js';
/**
 * 项目身份哈希命名空间（固定 ASCII 常量）。
 * projectId = 'prj_' + SHA256(PROJECT_ID_NAMESPACE + normalizedIdentity)，UTF-8 编码，hex 小写。
 */
export const PROJECT_ID_NAMESPACE = 'umh-project-identity-v1';
const IS_WIN = process.platform === 'win32';
/** 身份读取专用的 git 配置隔离：不读用户/系统全局配置，身份不受 insteadOf 等 host 配置影响。 */
const GIT_CONFIG_DISABLE = IS_WIN ? 'nul' : '/dev/null';
/**
 * 会劫持 git 仓库发现的宿主 env 变量（评审 Important 修复）：
 * 任一存活都会让身份子进程无视 cwd 去读另一个仓库的 remote/common-dir ——
 * 与 GIT_CONFIG_* 同一 env 通道，同样必须中性化，否则 resolveProject 可能静默绑定到错误仓库。
 */
const HOSTILE_GIT_ENV = ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE'];
/** 构造身份子进程 env：父进程 env 深拷贝后剥离劫持性 git 变量，再叠加配置中性化。 */
function identityGitEnv() {
    const env = { ...process.env };
    for (const key of HOSTILE_GIT_ENV)
        delete env[key];
    env.GIT_CONFIG_GLOBAL = GIT_CONFIG_DISABLE;
    env.GIT_CONFIG_SYSTEM = GIT_CONFIG_DISABLE;
    env.GIT_CONFIG_NOSYSTEM = '1';
    return env;
}
/**
 * 规范化身份路径字符串（纯函数，无 FS 访问）。
 * Windows：整串小写（默认 FS 大小写不敏感，drive/UNC host 因此一并归一）并统一分隔符为 `\`；
 * POSIX：统一分隔符为 `/`，保留大小写。
 * 连续分隔符合并为一个；去尾部分隔符（单字符根除外）。
 */
export function normalizeWorkspacePath(value) {
    if (typeof value !== 'string') {
        throw new UmhError('INVALID_ARGUMENT', 'workspace path must be a string');
    }
    if (IS_WIN) {
        const unified = value.toLowerCase().replace(/[\\/]+/g, '\\');
        return unified.length > 1 && unified.endsWith('\\') ? unified.slice(0, -1) : unified;
    }
    const unified = value.replace(/\\+/g, '/').replace(/\/{2,}/g, '/');
    return unified.length > 1 && unified.endsWith('/') ? unified.slice(0, -1) : unified;
}
/** 去路径尾部的 `.git` 后缀与斜杠（大小写不敏感）。 */
function stripGitSuffixes(rawPath) {
    let p = rawPath.replace(/\\/g, '/').replace(/\/{2,}/g, '/').replace(/^\//, '').replace(/\/+$/, '');
    if (/\.git$/i.test(p))
        p = p.slice(0, -4).replace(/\/+$/, '');
    return p;
}
/**
 * scheme:// 形态的 authority 处理：去 userinfo、去 SSH 默认端口 22、host 统一小写。
 * 空 host / 非数字端口 → INVALID_GIT_REMOTE（明确 error，不静默降级）。
 * 错误消息固定措辞，不回显原始 URL —— remote URL 可能内嵌凭据，禁止进入错误信息。
 */
function normalizeAuthority(authority, scheme) {
    let rest = authority;
    const at = rest.lastIndexOf('@');
    if (at !== -1)
        rest = rest.slice(at + 1);
    const bracket = /^\[([^\]]+)\](?::(\d+))?$/.exec(rest);
    if (bracket) {
        const host = `[${bracket[1]}]`;
        const port = bracket[2];
        if (port !== undefined && !(scheme === 'ssh' && port === '22')) {
            return `${host}:${port}`.toLowerCase();
        }
        return host.toLowerCase();
    }
    const colon = rest.indexOf(':');
    if (colon !== -1) {
        const host = rest.slice(0, colon);
        const port = rest.slice(colon + 1);
        if (host === '') {
            throw new UmhError('INVALID_GIT_REMOTE', 'git remote URL has an empty host');
        }
        if (!/^\d+$/.test(port)) {
            throw new UmhError('INVALID_GIT_REMOTE', 'git remote URL has a malformed host:port authority');
        }
        if (scheme === 'ssh' && port === '22')
            return host.toLowerCase();
        return `${host}:${port}`.toLowerCase();
    }
    if (rest === '') {
        throw new UmhError('INVALID_GIT_REMOTE', 'git remote URL has an empty host');
    }
    return rest.toLowerCase();
}
/**
 * 规范化 Git remote URL（纯函数）。
 * 归一形态：`host/path`（host 小写、凭据剥离、SSH 默认端口 22 剥离、尾 `.git`/斜杠去除）；
 * file:// 与本地路径 remote 归一为本地路径形态。
 * 无法安全解析（空 URL、控制字符、空 host、坏 authority）→ UmhError('INVALID_GIT_REMOTE')。
 */
export function normalizeGitRemote(value) {
    if (typeof value !== 'string') {
        throw new UmhError('INVALID_GIT_REMOTE', 'git remote URL must be a string');
    }
    const url = value.trim();
    if (url === '') {
        throw new UmhError('INVALID_GIT_REMOTE', 'git remote URL is empty');
    }
    if (/[\u0000-\u001f\u007f]/.test(url)) {
        throw new UmhError('INVALID_GIT_REMOTE', 'git remote URL contains control characters');
    }
    const schemeMatch = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/(.*)$/s.exec(url);
    if (schemeMatch) {
        const scheme = schemeMatch[1].toLowerCase();
        const rest = schemeMatch[2];
        if (scheme === 'file') {
            // file://host/path 的 host 部分极少使用；整体按路径处理
            return normalizeWorkspacePath(rest);
        }
        const slash = rest.indexOf('/');
        const authority = slash === -1 ? rest : rest.slice(0, slash);
        const rawPath = slash === -1 ? '' : rest.slice(slash + 1);
        const host = normalizeAuthority(authority, scheme);
        return rawPath ? `${host}/${stripGitSuffixes(rawPath)}` : host;
    }
    // SCP 形态：[user@]host:path（scheme:// 已在上面分支处理）。
    // Windows 盘符本地路径（C:\…、C:/…）不是 SCP：host 为单字母且冒号后紧跟分隔符时按本地路径处理。
    const scp = /^(?:([^@]*)@)?([^@:/]+):(.*)$/s.exec(url);
    if (scp) {
        const host = scp[2].trim();
        const afterColon = scp[3];
        const isDriveLetterPath = /^[a-zA-Z]$/.test(host) && /^[\\/]/.test(afterColon);
        if (!isDriveLetterPath) {
            return afterColon ? `${host.toLowerCase()}/${stripGitSuffixes(afterColon)}` : host.toLowerCase();
        }
    }
    // 本地路径 remote（相对或绝对路径形态）
    return normalizeWorkspacePath(url);
}
/**
 * 由 source 与规范化值构造哈希前的身份串（纯函数）。
 * 域标签保证三种身份形态的哈希空间互不混同。
 */
export function projectIdentity(source, normalized) {
    return `${source}:${normalized}`;
}
/** projectId = 'prj_' + SHA256(PROJECT_ID_NAMESPACE + normalizedIdentity) 的小写 hex。 */
export function computeProjectId(normalizedIdentity) {
    const digest = createHash('sha256')
        .update(PROJECT_ID_NAMESPACE + normalizedIdentity, 'utf8')
        .digest('hex');
    return `prj_${digest}`;
}
/**
 * 身份读取专用的 git 子进程（argv 数组、shell:false、GIT_CONFIG_GLOBAL/SYSTEM 置空）。
 * 仅执行只读命令（rev-parse / remote）；非 0 退出视为 .git authority 不可用 → INVALID_GIT_DIR。
 */
function runGitForIdentity(cwd, args) {
    const result = spawnSync('git', args, {
        cwd,
        shell: false,
        windowsHide: true,
        encoding: 'utf8',
        env: identityGitEnv(),
    });
    if (result.error) {
        const code = result.error?.code;
        if (code === 'ENOENT') {
            throw new UmhError('GIT_UNAVAILABLE', 'git executable is not available on PATH');
        }
        throw new UmhError('GIT_UNAVAILABLE', `git invocation failed: ${result.error.message}`);
    }
    if (result.status !== 0) {
        // 消息固定措辞，不回显 git stderr（可能包含本地路径细节）；损坏 .git 不降级为路径身份
        throw new UmhError('INVALID_GIT_DIR', `git ${args[0]} failed inside the workspace; the .git entry is not a usable repository authority`);
    }
    return result.stdout ?? '';
}
/** 选择身份权威 remote：origin 优先；唯一 remote 次之；多 remote 无 origin 属于歧义 authority → 显式 error。 */
function pickRemoteName(cwd) {
    const out = runGitForIdentity(cwd, ['remote']);
    const names = out
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter((line) => line.length > 0);
    if (names.length === 0)
        return null;
    if (names.includes('origin'))
        return 'origin';
    if (names.length === 1)
        return names[0];
    throw new UmhError('AMBIGUOUS_GIT_REMOTE', 'repository has multiple remotes and no origin; project identity is ambiguous until origin is configured');
}
/**
 * 解析工作区项目身份。
 * `.git` 存在时 git 是唯一权威：git 拒绝的 `.git`（损坏文件/空目录/非法 gitfile）→ INVALID_GIT_DIR，
 * 绝不静默降级为路径身份；remote 不可安全解析 → INVALID_GIT_REMOTE。
 */
export async function resolveProject(workspace) {
    if (typeof workspace !== 'string' || workspace.trim() === '') {
        throw new UmhError('INVALID_ARGUMENT', 'workspace must be a non-empty string');
    }
    let realWorkspace;
    try {
        realWorkspace = await realpath(workspace);
    }
    catch {
        throw new UmhError('WORKSPACE_NOT_FOUND', `workspace does not exist or cannot be resolved: ${workspace}`);
    }
    const canonicalWorkspace = normalizeWorkspacePath(realWorkspace);
    // junction/symlink 已被 realpath 解开；.git 自身也可能是链接，stat 跟随
    const dotGit = join(realWorkspace, '.git');
    let gitState = 'none';
    try {
        const s = await stat(dotGit);
        if (s.isDirectory())
            gitState = 'dir';
        else if (s.isFile())
            gitState = 'file';
    }
    catch {
        gitState = 'none';
    }
    if (gitState === 'none') {
        return {
            projectId: computeProjectId(projectIdentity('path', canonicalWorkspace)),
            canonicalWorkspace,
            source: 'path',
        };
    }
    const remoteName = pickRemoteName(realWorkspace);
    if (remoteName !== null) {
        const url = runGitForIdentity(realWorkspace, ['remote', 'get-url', remoteName]).trim();
        const normalizedRemote = normalizeGitRemote(url); // 不可解析 → INVALID_GIT_REMOTE，不降级
        return {
            projectId: computeProjectId(projectIdentity('git-remote', normalizedRemote)),
            canonicalWorkspace,
            source: 'git-remote',
        };
    }
    // 无 remote：git common root 为权威（linked worktree 与主仓归一到同一 common dir）
    const commonDir = runGitForIdentity(realWorkspace, ['rev-parse', '--git-common-dir']).trim();
    const absoluteCommonDir = isAbsolute(commonDir) ? commonDir : resolvePath(realWorkspace, commonDir);
    let realCommonDir;
    try {
        realCommonDir = await realpath(absoluteCommonDir);
    }
    catch {
        throw new UmhError('INVALID_GIT_DIR', 'git common directory cannot be resolved');
    }
    // bare 仓库的 common dir 就是工作区自身；普通仓库/linked worktree 取 common dir 的父目录
    const realWorkspaceNormalized = normalizeWorkspacePath(realWorkspace);
    const commonDirNormalized = normalizeWorkspacePath(realCommonDir);
    const gitRoot = commonDirNormalized === realWorkspaceNormalized
        ? commonDirNormalized
        : normalizeWorkspacePath(dirname(realCommonDir));
    return {
        projectId: computeProjectId(projectIdentity('git-root', gitRoot)),
        canonicalWorkspace,
        source: 'git-root',
    };
}
