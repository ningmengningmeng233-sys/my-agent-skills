<div align="center">

<img src="docs/assets/hero.svg" alt="my-agent-skills · 面向 Claude Code / Codex 的中文 Agent 技能库" width="100%">

<h3>从资料到检索，从小说到短剧，从需求到交付</h3>

<p><sub>16 套独立技能 · 中文使用指南 · 明确输入输出与能力边界 · 每一步都有质量闸门</sub></p>

<p>
<a href="docs/getting-started.md"><b>🚀 开始使用</b></a> &nbsp;·&nbsp;
<a href="#技能目录"><b>📚 浏览技能</b></a> &nbsp;·&nbsp;
<a href="PIPELINE.md"><b>🔗 组合流程</b></a> &nbsp;·&nbsp;
<a href="docs/faq.md"><b>❓ 常见问题</b></a> &nbsp;·&nbsp;
<a href="docs/verification.md"><b>🧪 验证范围</b></a>
</p>

[![CI](https://github.com/ningmengningmeng233-sys/my-agent-skills/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/ningmengningmeng233-sys/my-agent-skills/actions/workflows/ci.yml)
![Skills](https://img.shields.io/badge/skills-16-8b5cf6?style=flat-square)
![Tests](https://img.shields.io/badge/tests-4%2C588%20passing-10b981?style=flat-square)
![Python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-0ea5e9?style=flat-square)
![Claude Code](https://img.shields.io/badge/Claude%20Code-ready-d97757?style=flat-square&logo=anthropic&logoColor=white)
![Codex](https://img.shields.io/badge/Codex-ready-111827?style=flat-square&logo=openai&logoColor=white)
![Format](https://img.shields.io/badge/format-SKILL.md-f59e0b?style=flat-square)

<br>

[**⬇️ 下载完整 ZIP**](https://codeload.github.com/ningmengningmeng233-sys/my-agent-skills/zip/refs/heads/main) · [查看版本清单](catalog.json)

</div>

## 📊 核心数据

| 🧰 精选技能 | ✅ 自动化测试 | 🖥️ CI 矩阵 | 🎯 精选比例 |
| :---: | :---: | :---: | :---: |
| **16 套** | **4,588 项**通过 | **13 包 × 2 平台** | **192 → 16** |
| 独立安装、独立验证 | 2026-10 Linux 实测 | Windows / Linux | 只收录边界清晰的技能 |

## ✨ 亮点

<table>
<tr>
<td width="33%" valign="top">

### 🧪 测试先行
13 个包自带 pytest 套件，**4,588 项**用例在 Windows / Linux 双平台 CI 矩阵中全绿，改动不靠感觉靠证据。

</td>
<td width="33%" valign="top">

### 🛡️ 质量闸门
篇幅、节奏、纯度、原创性等闸门把关输出；终验前由**独立上下文的评审者**对抗式挑错，问题修复复验后才放行。

</td>
<td width="33%" valign="top">

### 🧠 多 Agent 协作
需求拷问、原子工单、TDD 推进、终验交付四阶段协议，配合**跨客户端共享记忆**，让多个 Agent 按同一套规矩干活。

</td>
</tr>
</table>

## 💡 设计理念

<img src="docs/assets/protocol.svg" alt="柠檬思考四阶段研发协议" width="100%">

- **四阶段研发协议（柠檬思考）**：Grilling 拷问对齐需求与设计 → 拆解原子工单 → 按 TDD 红绿推进 → 终验把关与交付，按工作区物理文件证据判定所处阶段，未经人类同意不写业务代码。
- **交付前对抗式评审**：终验前由独立的新上下文评审者通读整条分支挑错，Critical / Important 问题修复并复验后才放行。
- **质量闸门 + 自动化回归测试**：各包自带 pytest 套件，根目录 CI 按「包 × Windows/Linux」矩阵运行；13 个包合计 4588 项用例通过（2026-10 Linux 实测）。
- **由 192 套技能精简、择优开源**：只收录边界清晰、可独立安装与验证的技能。
- **跨客户端记忆 UMH**：本地共享记忆中枢，Codex、Qoder、Cherry 等客户端可共享与召回同一份记忆。

> **先选一套，用小样本跑通。** 每套技能独立安装；账号、模型、客户端与本地数据按对应指南准备。目录收录和工程检查不等于完整真实业务验收。

## 🔗 技能如何串联

各技能独立安装与验证，没有统一运行时；下图展示常见的输入输出衔接方式，详见[组合流程](PIPELINE.md)。

```mermaid
flowchart LR
    subgraph S1["资料采集与转录"]
        A1["B 站转录"]
        A2["抖音转录"]
        A3["Telegram 归档"]
    end
    subgraph S2["索引检索"]
        B1["知识库转录"]
        B2["小说索引"]
        B3["知识转录索引"]
    end
    subgraph S3["文本与改编"]
        C1["去 AI 味"]
        C2["日系小说本地化"]
        C3["小说转短剧"]
    end
    subgraph S4["提示词与资产"]
        D1["MiniMax H3 提示词"]
        D2["资产提取"]
    end
    E1["成片合成"]
    S1 --> S2 --> S3 --> S4 --> E1
    subgraph DEV["研发底座"]
        F1["柠檬思考"]
        F2["技能优化"]
        F3["全局记忆 UMH"]
        F4["RTK 压缩器"]
    end
    DEV -.-> S1
    DEV -.-> S3
```

> 实线为常见交接方向，虚线表示研发底座为各技能的开发与日常使用提供辅助；UMH、RTK 需要各自的服务或钩子安装，不是流程的强制依赖。真实视频生成需自备授权素材与服务。

## 🚀 从这里开始

| 你的目标 | 推荐路线 |
| --- | --- |
| 视频、消息转成可整理资料 | B 站转录 / 抖音转录 / Telegram 归档 |
| 在原文里定位知识、情节和作者风格 | 知识库转录 / 小说索引 |
| 修改中文表达、组织小说改编 | 去 AI 味 / 日系小说本地化 / 小说转短剧 |
| 准备视频提示词、资产与成片计划 | MiniMax H3 / 资产提取 / 成片合成 |
| 管理研发流程与本地工具能力 | 柠檬思考 / 技能优化 / UMH / RTK |

<a id="技能目录"></a>

## 📚 技能目录

**使用指南**面向使用者，包含准备条件、步骤、示例、边界和排错；**技能入口**供支持 Agent Skills 的宿主读取。版本与状态以 catalog 为准。

### 🎙️ 资料采集与检索

| 产品与使用指南 | 版本 | 用途 / 提醒 | 技能入口 |
| --- | --- | --- | --- |
| [B 站转录](docs/skills/bilibili-auto-transcript.md) | 5.5.5 | 音视频 · 字幕与识别 | [SKILL.md](B站转录_v5.5.5/SKILL.md) |
| [抖音转录](docs/skills/douyin-auto-transcript.md) | 2.0.6 | 音视频 · 批次与恢复 | [SKILL.md](抖音转录_v2.0.6/SKILL.md) |
| [Telegram 归档](docs/skills/tg-transcript.md) | 1.0.2 | 消息归档 · 附件整理 | [SKILL.md](tg转录_v1.0.2/SKILL.md) |
| [知识库转录](docs/skills/knowledge-index.md) | 5.4.8 | 非虚构资料 · 可追溯问答 | [SKILL.md](知识库转录_v5.4.8/SKILL.md) |
| [小说索引](docs/skills/novel-index.md) | 4.6.2 | 小说资料 · 分段与检索 | [SKILL.md](小说索引_v4.6.2/SKILL.md) |
| [知识转录索引](docs/skills/transcript-index.md) | 1.5.0 | 转录资料 · 待独立核验 | [SKILL.md](知识转录索引_v1.5.0/SKILL.md) |

### 🎬 文本与短剧制作

| 产品与使用指南 | 版本 | 用途 / 提醒 | 技能入口 |
| --- | --- | --- | --- |
| [去 AI 味](docs/skills/lieflat-less-ai-tone.md) | 2.0.2 | 文本编辑 · 保留作者声音 | [SKILL.md](去AI味_v2.0.2/SKILL.md) |
| [日系小说本地化](docs/skills/jp-novel-localization.md) | 4.5.6 | 中文编辑 · 连载流程 | [SKILL.md](日系小说本地化_v4.5.6/SKILL.md) |
| [小说转短剧](docs/skills/dramacraft.md) | 1.5.5 | 创作流程 · 大纲到分镜 | [SKILL.md](小说转短剧_v1.5.5/SKILL.md) |
| [MiniMax H3 提示词](docs/skills/minimax-h3-craft.md) | 7.4.6 | 视频提示词 · 规则审计 | [SKILL.md](MiniMaxH3提示词_v7.4.6/SKILL.md) |
| [资产提取](docs/skills/asset-extract.md) | 2.17.0-rc.7 | 文本资产 · 公开能力受限 | [SKILL.md](资产提取_v2.17.0-rc.7/SKILL.md) |
| [成片合成](docs/skills/media-assembly.md) | 0.1.4 | 媒体工程 · 计划与组装 | [SKILL.md](成片合成_v0.1.4/SKILL.md) |

### 🛠️ 研发与客户端辅助

| 产品与使用指南 | 版本 | 用途 / 提醒 | 技能入口 |
| --- | --- | --- | --- |
| [柠檬思考](docs/skills/lemon-thinking.md) | 1.1.5 | 研发流程 · 阶段对齐 | [SKILL.md](柠檬思考_v1.1.5/SKILL.md) |
| [技能优化](docs/skills/skill-optimization.md) | 1.0.1 | 工程审查 · 受控精简 | [SKILL.md](技能优化_v1.0.1/SKILL.md) |
| [全局记忆 UMH](docs/skills/universal-memory-hub.md) | 1.0.2 | 跨客户端 · 本地记忆 | [SKILL.md](全局记忆UMH_v1.0.2/SKILL.md) |
| [RTK 压缩器](docs/skills/rtk-token-reducer.md) | 92cc75f-r2 | 开发工具 · 输出压缩 | [SKILL.md](rtk-token-reducer_v92cc75f-r2/SKILL.md) |

## 📦 下载后怎么用

1. 下载 ZIP 并解压，或克隆仓库；选择需要的**完整技能目录**。
2. 阅读上表的使用指南，按原始安装文档准备宿主、依赖与配置。
3. 先检查环境，再用一个小样本核对状态和实际产物，最后才扩大批次。

详细步骤、安装与脚本的区别、更新和备份方法见[从下载到第一次使用](docs/getting-started.md)。无需安装全部 16 套，也不要只复制一个 SKILL.md。

## ⚠️ 使用前需要知道

<details>
<summary><strong>外部服务与费用 · 数据与恢复 · 资产提取公开档 · 知识转录索引 · 系统与宿主（点击展开）</strong></summary>

- **外部服务与费用**：索引、改写、识别和生成按实际配置调用本地或外部能力；先确认账号、内容授权、隐私和预算。
- **数据与恢复**：密钥、Cookie、会话、原稿和数据库留在本机；改写、批处理和更新前备份数据及状态。
- **资产提取公开档**：公开版不含部分图片素材，相关视觉能力不可用；完整发布校验仍有缺口，不能按完整视觉发行包理解。
- **知识转录索引**：历史来源与独立验收尚未闭合，保留待核验状态，只建议隔离试用。
- **系统与宿主**：完整跨系统、多宿主和真实账号链路尚无统一全部通过结论；请按接收者环境验证。

</details>

更多已知问题见[验证范围与限制](docs/verification.md)，下载或运行失败先看[常见问题](docs/faq.md)。

<details>
<summary><strong>许可、第三方组件与引用</strong></summary>

各产品和第三方组件分别遵循自己的许可、来源与分发声明，本仓库不统一授予 MIT。公开可下载不自动表示可商用、可转授权或可重新分发素材。B 站转录的 README 与入口许可表述冲突仍待裁决。使用具体组件前请查看所在包的许可文件、UPSTREAM、PROVENANCE 或 THIRD_PARTY_NOTICES。

</details>

## 👤 关于作者

**柠檬才不萌** · AI 应用 / Agent 工作流实践者。工程造价与施工现场出身，把工序拆解、质量验收、过程留痕和风险控制带进 AI 工作流：让 Agent 的每一步都可拆、可测、可追溯。

如果这个仓库对你有帮助，欢迎点一个 ⭐ Star。


---

介绍页更新：2026-10-16。当前目录身份见 [catalog.json](catalog.json)；本页完善不改变技能运行代码和既有验收状态。
