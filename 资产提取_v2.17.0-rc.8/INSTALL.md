# 安装与卸载（素材提取器 / `asset-extract`）

> 安装身份：`asset-extract`（稳定机器ID，含中文显示名 `素材提取器`；版本见 `metadata.version`，不进入安装目录名）。
> 本文件是发布候选随包说明；安装动作只写入你自己的宿主技能目录，不改系统全局配置。

## 安装步骤（通用）

1. 解压本包，得到 `资产提取-v2.17.0-rc.5-shareable/` 目录（内含 `SKILL.md`）；将其重命名为 `asset-extract/` 作为安装根（稳定机器ID，不含版本号与中文名前缀）。
2. 将整个 `asset-extract/` 目录复制到宿主的技能根目录，保持 `asset-extract/SKILL.md` 相对结构。
3. 按宿主机制重载/重启会话后，用显式点名验证可发现：对宿主说「加载 素材提取器」或按宿主的技能调用方式点名 `asset-extract`。

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
## 子入口（随包注册角色）

| 包内路径 | 注册角色 | 安装ID/说明 |
|---|---|---|
| `服饰设计/SKILL.md` | registered-skill（独立服饰能力） | `drama-costume-studio`（中文显示`短剧服装设计`）；如需独立触发，把 `服饰设计/` 目录复制到宿主技能根并改名为 `drama-costume-studio`，再点名触发（留在主包内嵌位置不会被宿主发现） |
| 风格包 `private/SKILL.md` | reference-only（画风装载说明） | 随包（风格包 manifest 登记 required；位于 5 层深处，不会被宿主发现为独立技能）；内容由主技能按需读取 |

子入口宿主触发证据未随包（验收矩阵 NOT_RUN）：显式点名可发现≠自然触发已验证。

## 依赖

- Python ≥ 3.10（按 pyproject 声明安装依赖）。
- 生图平台凭据由接收者配置；缺凭据/缺参考图时签发门明确阻断，不空资源凑通过。
- **shareable 分发档的视觉资源按 share-policy 受限**：能力缺口在包内如实声明（未随包的私有素材不补齐），完整视觉能力需接收者合法取得相应素材。
- 用户数据位置：任务工作目录与你的资产库根。

## 公开自验（随包可执行）

解包根执行（需 Python，离线可跑，约数分钟）：

`python 测试/run_all_checks.py`

分享包预期 15/17：`unit tests` 与 `e2e checks` 两项不通过——均为媒体钉单
2 项测试失败所致（钉单登记完整 36 项，盘上为分享子集：12 项禁分享媒体缺失；
构建声明 `VISUAL_STYLE_INCOMPLETE_FOR_SHARE`）；`git diff --check` 在非 git
解包副本中跳过（计通过）。完整 17/17 只在开发树成立，不作为接收者通过标准。

## 卸载

1. 删除宿主技能根下的 `<id>/` 目录（仅本技能目录；不动其它技能与宿主配置）。
2. 用户数据（见上「用户数据位置」）默认保留，不随卸载自动删除；如需清理，请先备份再手动删除你点名的目录。
3. 如安装过宿主 hook（仅 rtk 等明确说明的产品），按该产品 README 的 hook 卸载步骤移除。

## 本次公开分享版

必读 CAPABILITIES.md：32张受限风格图未入包，相关视觉能力不可用；只提供已声明的文本/离线能力。
