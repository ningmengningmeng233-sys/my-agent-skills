# 安装与卸载（MiniMax H3制作 / `minimax-h3-craft`）

> 安装身份：`minimax-h3-craft`（稳定机器ID，含中文显示名 `MiniMax H3制作`；版本见 `metadata.version`，不进入安装目录名）。
> 本文件是发布候选随包说明；安装动作只写入你自己的宿主技能目录，不改系统全局配置。

## 安装步骤（通用）

1. 解压本包，得到 `<id>/` 目录（内含 `SKILL.md`）。
2. 将整个 `<id>/` 目录复制到宿主的技能根目录，保持 `<id>/SKILL.md` 相对结构。
3. 按宿主机制重载/重启会话后，用显式点名验证可发现：对宿主说「加载 MiniMax H3制作」或按宿主的技能调用方式点名 `minimax-h3-craft`。

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

- Python ≥ 3.10（脚本与校验器；无第三方运行时依赖）。
- 无模型调用、无外部服务请求；提示词审计与时长门为本地确定性逻辑。
- 用户数据位置：无（纯函数式技能，不写用户目录）。

## 公开自验（随包可执行）

解包根执行（需 Python）：

`python scripts/verification_protocol.py check`

`python scripts/audit_cli.py <提示词文件> --mode auto --warn-policy report`
（返回 0 合格，非 0 阻断交付；命令口径见 SKILL.md）。

## 卸载

1. 删除宿主技能根下的 `<id>/` 目录（仅本技能目录；不动其它技能与宿主配置）。
2. 用户数据（见上「用户数据位置」）默认保留，不随卸载自动删除；如需清理，请先备份再手动删除你点名的目录。
3. 如安装过宿主 hook（仅 rtk 等明确说明的产品），按该产品 README 的 hook 卸载步骤移除。

## 持续集成（CI）

包内 `.github/workflows/ci.yml`（`CI-MiniMaxH3提示词_v7.4.5`）仅作历史/上游模板保留；实际门禁由仓库根 `.github/workflows/ci.yml` 矩阵 CI 统一接管，按包目录运行本包 `_审查测试` 既有 pytest。
