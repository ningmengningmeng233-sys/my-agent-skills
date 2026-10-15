# 安装与卸载（日系小说本地化 / `jp-novel-localization`）

> 安装身份：`jp-novel-localization`（稳定机器ID，含中文显示名 `日系小说本地化`；版本见 `metadata.version`，不进入安装目录名）。
> 本文件是发布候选随包说明；安装动作只写入你自己的宿主技能目录，不改系统全局配置。

## 安装步骤（通用）

1. 解压本包，得到 `<id>/` 目录（内含 `SKILL.md`）。
2. 将整个 `<id>/` 目录复制到宿主的技能根目录，保持 `<id>/SKILL.md` 相对结构。
3. 按宿主机制重载/重启会话后，用显式点名验证可发现：对宿主说「加载 日系小说本地化」或按宿主的技能调用方式点名 `jp-novel-localization`。

## 宿主技能根（首批宿主；路径以官方文档为准）

| 宿主 | 技能根 | 官方文档 |
|---|---|---|
| Claude Code | `~/.claude/skills/` | https://code.claude.com/docs/en/skills |
| Codex | 以 [OpenAI 技能文档](https://learn.chatgpt.com/docs/build-skills) 为准 | 同左 |
| Qoder CLI | 以 [Qoder CLI 技能文档](https://docs.qoder.com/cli/Skills) 为准（与 IDE 是两套目录与重载机制） | 同左 |
| Qoder IDE | 以 [Qoder IDE 技能文档](https://docs.qoder.com/extensions/skills) 为准 | 同左 |
| Cursor | 以 [Cursor 技能文档](https://cursor.com/docs/skills) 为准（name 字符/目录有要求） | 同左 |

## 验证与边界

- **显式加载成功 ≠ 自然触发**：点名能加载只证明可发现；中文自然语言正例、不适用负例让位、缺依赖安全行为属验收矩阵内容（见随附 SUPPORT/验收说明）。
- 缺依赖时技能应明确停下并说明先决条件，不得回退到作者机器路径或凭据。
## 依赖

- Python ≥ 3.12（`v4/pyproject.toml` 要求；按其声明安装依赖）。
- 模型 API 配置由接收者提供（rewrite 流水线调用模型；缺配置明确停下，不使用任何默认转发地址）。
- 卡库/风格资源按包内说明放置；本包不含作者私人卡库与语料。
- 用户数据位置：任务工作目录。

## 公开自验（随包可执行）

解包根执行（需 Python，并已装 flake8 与 pytest）：

`python scripts/run_package_checks.py`

10/11 为通过；`tree_drift` 一项 NOT_RUN（需发布清单做逐成员比对，
属维护侧验收，不影响接收者使用）。

## 卸载

1. 删除宿主技能根下的 `<id>/` 目录（仅本技能目录；不动其它技能与宿主配置）。
2. 用户数据（见上「用户数据位置」）默认保留，不随卸载自动删除；如需清理，请先备份再手动删除你点名的目录。
3. 如安装过宿主 hook（仅 rtk 等明确说明的产品），按该产品 README 的 hook 卸载步骤移除。
