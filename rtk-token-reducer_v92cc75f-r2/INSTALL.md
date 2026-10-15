# 安装与卸载（RTK Token Reducer / `rtk-token-reducer`）

> 安装身份：`rtk-token-reducer`（稳定机器ID，含中文显示名 `RTK Token Reducer`；版本见 `metadata.version`，不进入安装目录名）。
> 本文件是发布候选随包说明；安装动作只写入你自己的宿主技能目录，不改系统全局配置。

## 安装步骤（通用）

1. 解压本包，得到 `<id>/` 目录（内含 `SKILL.md`）。
2. 将整个 `<id>/` 目录复制到宿主的技能根目录，保持 `<id>/SKILL.md` 相对结构。
3. 按宿主机制重载/重启会话后，用显式点名验证可发现：对宿主说「加载 RTK Token Reducer」或按宿主的技能调用方式点名 `rtk-token-reducer`。

## 宿主技能根（首批宿主；路径以官方文档为准）

| 宿主 | 技能根 | 官方文档 |
|---|---|---|
| Claude Code | `~/.claude/skills/` | https://code.claude.com/docs/en/skills |
| Codex | 以 [OpenAI 技能文档](https://learn.chatgpt.com/docs/build-skills) 为准 | 同左 |
| Qoder CLI | 以 [Qoder CLI 技能文档](https://docs.qoder.com/cli/Skills) 为准（与 IDE 是两套目录与重载机制） | 同左 |
| Qoder IDE | 以 [Qoder IDE 技能文档](https://docs.qoder.com/extensions/skills) 为准 | 同左 |
| Cursor | 以 [Cursor 技能文档](https://cursor.com/docs/skills) 为准（name 字符/目录有要求） | 同左 |

## 验证与边界

- **显式加载成功 ≠ 自然触发**：点名能加载只证明可发现；中文自然语言正例、不适用负例让位、缺依赖安全行为属验收矩阵内容（见包内 SUPPORT/验收说明）。
- 缺依赖时技能应明确停下并说明先决条件，不得回退到作者机器路径或凭据。
## 依赖

- **Python ≥ 3.8**（hook 与自检的运行解释器；代码仅标准库、无版本敏感语法，已在 3.12 实测，3.8–3.11 未实测——低版本先跑自检再启用）。
- **rtk 命令行工具**（外部 CLI，需已在 PATH；本技能不绑定任何作者盘符路径）。
- 数据根：优先环境变量 `RTK_REDUCER_HOME`；未设置时用当前用户目录 `~/.qoder/rtk-reducer`。
- 宿主 hook 支持：压缩能力需要宿主支持 Bash 输出 hook（Qoder/Claude Code 等）；不支持 hook 的宿主只能加载技能说明，压缩自动进行与否以 `NOT_RUN/UNSUPPORTED` 如实登记。
- 用户数据位置：`$RTK_REDUCER_HOME` 或 `~/.qoder/rtk-reducer`（spool 原文、统计、自学规则）。

## 公开自验（随包可执行）

解包根执行（需 Python）：

`python hooks/selftest.py`

25 项断言全过为通过。便携路径另由
`python -m pytest hooks/test_portable_paths.py -q` 覆盖（4 项）。

## 卸载

1. 删除宿主技能根下的 `<id>/` 目录（仅本技能目录；不动其它技能与宿主配置）。
2. 用户数据（见上「用户数据位置」）默认保留，不随卸载自动删除；如需清理，请先备份再手动删除你点名的目录。
3. 如安装过宿主 hook（仅 rtk 等明确说明的产品），按该产品 README 的 hook 卸载步骤移除。
