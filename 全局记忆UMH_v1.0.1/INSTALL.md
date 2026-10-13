# 安装与卸载（全局记忆UMH / `universal-memory-hub`）

> 安装身份：`universal-memory-hub`（稳定机器ID，含中文显示名 `全局记忆UMH`；版本见 `metadata.version`，不进入安装目录名）。
> 本文件是发布候选随包说明；安装动作只写入你自己的宿主技能目录，不改系统全局配置。

## 安装步骤（散根形态，必读）

本包是**散根形态**：`SKILL.md`、`DISTRIBUTION.json`、`package/`、`scripts/` 等直接在 ZIP 根，
没有单层包裹目录。**禁止直接解压到宿主技能根**（会把 4000+ 发行文件撒进共享根目录）。

1. 新建独立自选产品目录，如 `<自选>/universal-memory-hub/`。
2. 解压本包到该产品目录，得到 `<自选>/universal-memory-hub/SKILL.md`（散根：SKILL.md 在产品目录根）。
3. 技能入口注册（二选一）：(a) 把整个产品目录放入宿主技能根，保持 `universal-memory-hub/SKILL.md`
   相对结构；(b) 宿主支持目录外技能时，按宿主方式指向该产品目录。发行运行目录（`package/`）
   与技能入口是同一产品目录的两面，不要拆散、不要只拷 `SKILL.md`。
4. 自验（产品目录内执行）：`node scripts/verify-bundle.mjs`（应输出 valid）；
   `node scripts/setup.mjs --check`（只读检查；不要执行 `--init`，不要启动真实记忆服务）。
5. 按宿主机制重载/重启会话后，用显式点名验证可发现：对宿主说「加载 全局记忆UMH」或按宿主的技能调用方式点名 `universal-memory-hub`。

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

- Node.js（最低版本见包内 `package/DEPENDENCIES-PIN.json`；随包含离线依赖，不需联网安装）。
- Python（下限声明见包内 README/DEPENDENCIES；接收侧按说明自备）。
- 记忆服务与各客户端接入由接收者按 README 完成初始化；本包不带任何账号或会话。
- 用户数据位置：接收者配置的数据根（setup 流程会询问/生成，不使用作者路径）。

## 卸载

1. 删除宿主技能根下的产品目录（`universal-memory-hub/` 或你的自选产品目录；仅本产品目录，不动其它技能与宿主配置）。
2. 用户数据（见上「用户数据位置」）默认保留，不随卸载自动删除；如需清理，请先备份再手动删除你点名的目录。
3. 如安装过宿主 hook（仅 rtk 等明确说明的产品），按该产品 README 的 hook 卸载步骤移除。
