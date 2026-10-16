[← 技能柜首页](../../README.md) · [开始使用](../getting-started.md) · [常见问题](../faq.md)

# 抖音转录

> 音视频 · 批次与恢复 · **2.0.6** · [原始技能入口](../../抖音转录_v2.0.6/SKILL.md)

将抖音链接、分享文案或清单整理为转录文本、摘要与时间戳，保存原始记录、数据库状态和重试信息。

[使用流程](#使用流程) · [示例](#示例) · [边界与注意事项](#边界与注意事项) · [遇到问题](#遇到问题)

## 输入与交付

输入：可访问的抖音链接或分享文案；批次用文本清单。

输出：主 TXT、原始转录记录、批次状态和按配置完成的增强结果。

## 开始之前

以 Python ≥ 3.12 和当前 doctor 要求准备；需要 FFmpeg/FFprobe、识别模型及按用途准备的浏览器登录。历史说明提到的 requirements.lock 未随当前包提供，不能假称可按该锁一键安装；先核对 requirements.txt 和上游依赖。

安装时完整复制 `抖音转录_v2.0.6/`，由宿主按入口的 `name` 识别。宿主加载与脚本依赖是两件事；先看[通用安装说明](../getting-started.md)，再按下方原始文档准备。

## 使用流程

1. 先阅读 KNOWN-LIMITS，准备环境并运行 doctor，缺项保留阻塞。
2. 选择一个短视频，确认账号可访问，先完成基础转录。
3. 核对时间戳、原始文本与摘要，增强/OCR 另行确认模型和开销。
4. 再处理批次；超过阈值先看预览，失败按 batch_id 恢复，不重复拉取整批。

## 示例

可向已加载该技能的代理这样描述任务；请替换成自己的资料和范围：

> 用抖音转录先处理这一个链接。先检查环境和登录，只完成基础转录；标出不确定内容。需要下载模型、OCR 或外部摘要服务时先说明，不把待处理项记作完成。

### 命令入口

在技能根目录执行，使用已准备好的对应运行环境。`<…>` 是必须替换的占位符；下面的业务命令不是安装完成后的自动动作，也不代表本轮已执行真实业务。

```powershell
.venv/Scripts/python.exe scripts/doctor.py --json
# 环境就绪后，以下命令会处理视频
.venv/Scripts/python.exe scripts/douyin_transcript.py "<分享文案或链接>" --json
.venv/Scripts/python.exe scripts/batch_transcribe.py --file "清单.txt" --json
```

## 批次恢复与模型准备

单条入口是 `scripts/douyin_transcript.py`；批量清单使用 `scripts/batch_transcribe.py`。恢复和重试也走批次入口：

```powershell
.venv/Scripts/python.exe scripts/batch_transcribe.py --resume "<batch_id>" --json
.venv/Scripts/python.exe scripts/batch_transcribe.py --retry-failed "<batch_id>" --json
```

`--confirm` 用于已审阅预览并明确同意的批次，不应默认加到每条命令。Faster-Whisper、OCR 和浏览器会话分别准备；下载模型前检查磁盘、网络与权限。

## 边界与注意事项

- 依赖固定上游能力，账号权限、限流与平台变化会影响成功率。不要假装绕过登录。
- 新增超过 10 条或预计超过 30 分钟的批次需按预览规则确认；exit 2 不直接等于程序崩溃。
- awaiting_ai、login_required、needs_review 等状态不是完成；原始 ASR 文本需保留。
- 说话人标签是匿名区分，不证明真实身份；OCR、摘要和校正文均需抽查。

## 遇到问题

| 现象 | 建议处理 |
| --- | --- |
| 缺上游依赖或锁文件 | 按实际文件与上游说明补齐；记录阻塞，不创建空锁或报告已安装。 |
| 要求登录或被限流 | 暂停批次，恢复合法会话；不要无限重试。 |
| 批次中断 | 保留数据库、原始文本和 batch_id，按 README 的 resume / retry-failed 工序恢复。 |

恢复操作前保留原始输入、配置（脱敏）、日志和状态库；不要先删除现场。备份至少包含用户数据和工序状态，不能只备份技能文件。

## 原始说明与验收状态

- [README.md](../../抖音转录_v2.0.6/README.md)
- [INSTALL.md](../../抖音转录_v2.0.6/INSTALL.md)
- [KNOWN-LIMITS.md](../../抖音转录_v2.0.6/KNOWN-LIMITS.md)
- [ROLLBACK.md](../../抖音转录_v2.0.6/ROLLBACK.md)
- [UPSTREAM.md](../../抖音转录_v2.0.6/UPSTREAM.md)

本页提供阅读导航，不改写包内契约或分发身份。完整工程与真实使用的区别见[验证范围](../verification.md)。遇到包内历史说明冲突时，以对应版本的实际入口、当前文件和检查器为依据，并保留冲突记录。
