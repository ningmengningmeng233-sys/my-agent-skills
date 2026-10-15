# 上游单点依赖失效路径（自 SKILL.md 逐字移入，未删任何内容）

> 归属：douyin-auto-transcript SKILL.md「概述（核心原则）」一节的上游依赖条目。
> v2.0.1 收口迭代把全文搬到这里，SKILL.md 内保留一行式速读版与指针。以下为原文逐字。

- **上游单点依赖失效路径（已验证）**：锁定版本不可用/改版/停更时，采集入口不崩溃不抛栈，只回一句白话可处置文案——加 `--channel yt-dlp` 用降级通道重试，或检查上游仓库是否停更；降级仅在本机探测到 `yt-dlp` 时启用（`shutil.which`，绝不自动安装）。上游源码本地副本见 `third-party/douyin-ingest/`（`UPSTREAM-PIN.txt` 记录出处、锁定版本与重建命令）。

相关错误码（如 `ingest_contract_changed`）的逐条处置见 `troubleshooting.md`；
锁定与重建流程见 `SETUP.md`。
