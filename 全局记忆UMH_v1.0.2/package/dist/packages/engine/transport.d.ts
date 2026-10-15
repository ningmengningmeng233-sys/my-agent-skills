/**
 * packages/engine/transport — md_cg worker 进程通信（C04）。
 *
 * Boot 配方逐字来自 C01 冻结合同（docs/upstream-audit/engine-contract.json
 * environment 节，源自 .lemon/standalone 独立运行探针）：
 *   argv  = [<python>, -X, utf8, -m, md_cg.mcp_server]（数组 spawn，shell=false）
 *   cwd   = stage（vendored md_cg 运行单元：md_cg/、utf8_boot.py、
 *           data/policy.json、LICENSE —— vendor/mdcg，由 scripts/vendor-mdcg.py
 *           按 C01 清单逐字节复制并验证）
 *   env   = MDCG_ROOT / MDCG_STATE_ROOT / MDCG_DATA_ROOT / MDCG_AUX_ROOT /
 *           MDCG_POLICY_FILE / MDCG_SUSTAIN=0 / MDCG_MCP_SURFACE=full /
 *           MDCG_TENANT / MDCG_TOKEN_FILE / PYTHONUTF8 / PYTHONIOENCODING，
 *           HOME/USERPROFILE 指向实例私有合成目录。
 *
 * 身份（生产形态，C01 identity-* 探针）：经 md_cg.tokens.issue 按会话签发限权
 * 令牌（role 映射 admin→designer / recorder→record / reader→output），明文只走
 * MDCG_TOKEN 进程环境，绝不落合同/日志/持久层；MDCG_LEGACY_ENV_AUTH 恒不启用。
 * 传输层使用官方 MCP SDK v1（@modelcontextprotocol/sdk 1.31.0）Client +
 * StdioClientTransport（C01 DEPENDENCIES-PIN 固定代际，深路径入口）；
 * Python 上游 PROTOCOL_VERSION=2024-11-05 在 SDK SUPPORTED_PROTOCOL_VERSIONS
 * 内，initialize 协商兼容（tests/engine 实测）。
 *
 * supervisor 纪律（设计 §5）：一个作用域实例独占一个 worker，stdin 请求由上层
 * 引擎串行化（FIFO）；本模块只提供单连接调用与关闭。
 */
/** UMH 引擎角色 → 上游令牌角色（C01 roles.umhMapping，探针证据固定）。 */
export declare const UPSTREAM_ROLE: Record<EngineRole, string>;
export type EngineRole = 'reader' | 'recorder' | 'admin';
/** 引擎 worker 的完整配置；根/aux 全部由调用方解析（C03 resolveEngineRoot）。 */
export interface WorkerConfig {
    python: string;
    stage: string;
    root: string;
    auxRoot: string;
    stateRoot: string;
    dataRoot: string;
    policyFile: string;
    role: EngineRole;
    tenant: string;
    actor: string;
}
/**
 * 经上游 md_cg.tokens.issue 签发一枚限权令牌（C01 identity-* 探针配方）。
 * 明文只在进程内存传递；令牌记录落 MDCG_TOKEN_FILE（aux/_tokens.json，0600）。
 */
export declare function issueToken(cfg: WorkerConfig): {
    token: string;
    tokenId: string;
};
/** 一个已连接的 md_cg MCP worker 会话。 */
export declare class WorkerConnection {
    private client;
    private transport;
    private stderr;
    private closed;
    private constructor();
    /** 按 C01 boot 配方 spawn worker 并完成 MCP initialize 协商。 */
    static spawn(cfg: WorkerConfig, token: string): Promise<WorkerConnection>;
    /**
     * 调用 native 工具并解析 results.content[0].text 的 JSON。
     * 上游工具错误以 isError:true + {error: "..."} 返回 —— 在此映射为 UmhError。
     */
    call(name: string, args: Record<string, unknown>): Promise<Record<string, unknown>>;
    /** 关闭会话并终止 worker 进程（t.after / close 的清理责任）。 */
    close(): Promise<void>;
    diagnostics(): string;
}
/** 建立引擎实例所需的目录（根、aux、state、data、合成 HOME/tmp）。 */
export declare function ensureDirectories(cfg: WorkerConfig): Promise<void>;
