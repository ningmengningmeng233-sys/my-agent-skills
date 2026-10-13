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
/**
 * 项目身份哈希命名空间（固定 ASCII 常量）。
 * projectId = 'prj_' + SHA256(PROJECT_ID_NAMESPACE + normalizedIdentity)，UTF-8 编码，hex 小写。
 */
export declare const PROJECT_ID_NAMESPACE = "umh-project-identity-v1";
export type ProjectIdentitySource = 'git-remote' | 'git-root' | 'path';
export type ProjectResolution = {
    /** 'prj_' + 64 位小写 hex。 */
    projectId: string;
    /** 工作区 realpath 归一后的规范路径（与身份来源无关，始终指向真实目录）。 */
    canonicalWorkspace: string;
    /** 身份权威来源：git remote 优先，其次 git common root，最后普通路径。 */
    source: ProjectIdentitySource;
};
/**
 * 规范化身份路径字符串（纯函数，无 FS 访问）。
 * Windows：整串小写（默认 FS 大小写不敏感，drive/UNC host 因此一并归一）并统一分隔符为 `\`；
 * POSIX：统一分隔符为 `/`，保留大小写。
 * 连续分隔符合并为一个；去尾部分隔符（单字符根除外）。
 */
export declare function normalizeWorkspacePath(value: string): string;
/**
 * 规范化 Git remote URL（纯函数）。
 * 归一形态：`host/path`（host 小写、凭据剥离、SSH 默认端口 22 剥离、尾 `.git`/斜杠去除）；
 * file:// 与本地路径 remote 归一为本地路径形态。
 * 无法安全解析（空 URL、控制字符、空 host、坏 authority）→ UmhError('INVALID_GIT_REMOTE')。
 */
export declare function normalizeGitRemote(value: string): string;
/**
 * 由 source 与规范化值构造哈希前的身份串（纯函数）。
 * 域标签保证三种身份形态的哈希空间互不混同。
 */
export declare function projectIdentity(source: ProjectIdentitySource, normalized: string): string;
/** projectId = 'prj_' + SHA256(PROJECT_ID_NAMESPACE + normalizedIdentity) 的小写 hex。 */
export declare function computeProjectId(normalizedIdentity: string): string;
/**
 * 解析工作区项目身份。
 * `.git` 存在时 git 是唯一权威：git 拒绝的 `.git`（损坏文件/空目录/非法 gitfile）→ INVALID_GIT_DIR，
 * 绝不静默降级为路径身份；remote 不可安全解析 → INVALID_GIT_REMOTE。
 */
export declare function resolveProject(workspace: string): Promise<ProjectResolution>;
