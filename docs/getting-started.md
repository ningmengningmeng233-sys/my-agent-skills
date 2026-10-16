[← 技能柜首页](../README.md) · [常见问题](faq.md) · [验证范围](verification.md)

# 从下载到第一次使用

先选择一套技能，用一个小样本跑通，再考虑组合。这里没有必须统一安装的“大运行时”。

## 1 · 下载完整目录

- [下载 main 分支 ZIP](https://codeload.github.com/ningmengningmeng233-sys/my-agent-skills/zip/refs/heads/main)，解压后选择所需技能的完整文件夹。
- 或克隆仓库，便于保留版本与后续更新：

```powershell
git clone https://github.com/ningmengningmeng233-sys/my-agent-skills.git
cd my-agent-skills
```

目录中的 `catalog.json` 记录版本。只复制 `SKILL.md` 会漏掉脚本、配置、规则与运行依赖。UMH 等带完整分发清单的包尤其不能删减文件后继续当作原始分发。

## 2 · 找到适合的入口

| 你要做的事 | 建议入口 |
| --- | --- |
| B 站或抖音视频转文字 | [B 站转录](skills/bilibili-auto-transcript.md) / [抖音转录](skills/douyin-auto-transcript.md) |
| Telegram 消息与附件归档 | [Telegram 归档](skills/tg-transcript.md) |
| 非虚构书籍、手册检索 | [知识库转录](skills/knowledge-index.md) |
| 小说原文检索、作者风格资料 | [小说索引](skills/novel-index.md) / [成人文学索引](skills/adult-literature-index.md) |
| 小说改编、视频提示词、合成 | [小说转短剧](skills/dramacraft.md) → [组合流程](../PIPELINE.md) |
| 中文文本编辑 | [去 AI 味](skills/lieflat-less-ai-tone.md) / [日系小说本地化](skills/jp-novel-localization.md) |
| 研发任务与代码精简 | [柠檬思考](skills/lemon-thinking.md) / [技能优化](skills/skill-optimization.md) |
| 跨客户端记忆、工具输出压缩 | [UMH](skills/universal-memory-hub.md) / [RTK](skills/rtk-token-reducer.md) |

## 3 · 安装到你的宿主

1. 确认客户端是否支持目录型 Agent Skills，以及它实际使用的技能根目录。
2. 先阅读所选技能的 `INSTALL.md`；没有该文件的包读 `SKILL.md` 和专用配置指南。
3. 复制完整目录，按宿主要求使用入口 frontmatter 的 `name`。版本文件夹名、中文产品名和 `name` 可能不同，不要改写入口来强行匹配。
4. 按宿主说明刷新或重新加载；先显式点名该技能，检查是否真的读取了对应入口。
5. 再安装该技能自己的脚本依赖、服务或模型。宿主能看到技能不代表这些依赖已经就绪。

RTK 另需安装客户端钩子；UMH 另需本地服务和逐端接入。并非把文件夹放进去就能完成这两类工具的安装。当前没有所有系统、所有宿主通用的一键安装承诺。

## 4 · 配置数据、账号与费用

- 为运行环境和用户数据各设独立目录；不要把真实资料、Cookie、会话和 API Key 放进仓库。
- `.env.example` 是字段模板，不能直接当作已经可用的账号配置。
- 有分发校验清单的包先验证原始包，再按说明创建运行副本；不要在封存包里改依赖和清单。
- 模型、字幕、OCR、转码、下载器和向量模型按需准备，不要一次安装 17 套依赖。
- 索引、摘要、改写和生图可能向所配置的服务发送输入并计费。先确认内容可传输、服务提供方和调用上限。
- Windows 示例中的 `.venv/Scripts/python.exe` 不适用于直接照抄到 macOS/Linux；后两者还需自行验证对应入口与依赖。

## 5 · 完成一个小样本

先环境检查或 dry-run，再处理一条视频、一份短文、一章小说或一条测试记忆。检查**原始输入 → 状态 → 实际产物**三者一致。准备扩批前，确认失败如何恢复、原始数据如何备份。

> 环境检查有时包含临时探针或缓存操作。需要严格只读时，先审阅对应检查器；不要把所有叫 doctor 的命令都当成零写入命令。

## 6 · 更新与回退

记录当前提交和所用技能版本，备份数据、配置与工序状态。在新目录试用新版，验证后再替换；不要把新旧包混拷。Git 回退只恢复代码，不能替代数据库、附件、客户端设置和原稿的备份。

在 GitHub 网页可复制本次提交的固定链接；`main` 会继续变化，协作或复现时应附具体提交号。
