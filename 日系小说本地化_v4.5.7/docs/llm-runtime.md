# LLM 运行说明

使用 OpenAI 兼容的 `/chat/completions`。配置 `NOVEL_LLM_PROVIDER`、`<PROVIDER>_API_KEY`、`<PROVIDER>_BASE_URL`、`<PROVIDER>_MODEL` 和超时/重试变量。密钥只放环境变量，不写入仓库或审计。

本地 OpenAI 兼容网关示例（把地址、模型与 `<API_KEY>` 换成你自己的网关，不要写入文件）：

```powershell
$env:NOVEL_LLM_PROVIDER="gateway"
$env:GATEWAY_BASE_URL="http://127.0.0.1:<端口>/v1"
$env:GATEWAY_MODEL="<你的模型名>"
$env:GATEWAY_API_KEY="<API_KEY>"
```

`doctor.ps1` 会检查配置但不会打印密钥。默认后端没有 key 时拒绝正式 rewrite；占位后端只用于测试。

## ContextCompiler 与 PromptManifest（v4.5.0-dev.3）

正式改写 Prompt 不再手工拼字符串，统一经 `v4/core/context_compiler.py` 编译：

- 上下文块带权威级与优先级：系统规则 > 源稿段落 > 事件图 > 已接受决策 > 锁定词典 > 连续性 > 风格卡 > 章节索引 > 写作指导；
- 输入 Token 预算 = 上下文窗口 − 输出预留（`max_tokens`）− 安全边际；必选块（源稿/事件图/决策/词典）合计超预算直接抛 `PromptBudgetExceeded`，绝不静默截断；
- 参考资产配额：普通章 1+1，复杂章 2+2（多场景/长章/事件密集），由 `compute_prompt_budget` 判定；
- 旧的字符级截断（`_MAX_REFERENCE_CHARS`）已移除，块级取舍由 Token 预算与权威级裁决。

每次正式 Prompt 运行（含分段调用）在调用 LLM 前原子落盘不可变 `PromptManifest`：

- 路径：`chapters/<chapter-id>/prompts/pr-<run_id>.json`；
- 内容：块元数据（id/kind/authority/pinned/tokens/sha256）、系统与用户消息哈希、预算、段落区间、时间戳；
- 用途：与 `checks/vNNN.json` 的 `prompt_run_id` 绑定，实现任意一次正式生成的回放审计（LLM 失败时 manifest 同样保留）。
