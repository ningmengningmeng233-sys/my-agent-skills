---
name: universal-memory-hub
description: Use when 用户要安装、接入或管理 UMH 全局共享记忆，跨 Cherry、Qoder、ZCode、Codex、MiniMax、DSH 共享新记忆，或处理候选审核、跨端召回、备份恢复和撤销。Claude 原生兼容性未验证；普通创作任务不触发。
metadata:
  display_name: 全局记忆UMH

  version: "1.0.1"
  distribution: portable
---

# UMH 全局共享记忆（可分发版）

本目录包含技能、已编译运行程序、固定版本 Node 依赖和 vendored Python 引擎。复制整个目录即可交给其他人；不依赖作者电脑、开发工作树、现有绑定、个人备份或演练数据。

## 找到本机路径

先以 **本 SKILL.md 所在目录**解析技能根，不以聊天当前工作区猜路径。以下 `<技能目录>` 必须换成实际绝对路径；Node 使用本机安装的 `node`。脚本会自行定位相邻 `package`，默认数据根为当前用户家目录下 `.umh`，也可通过 `--umh-home <绝对路径>`或 `UMH_HOME` 显式指定。

Node >=24.2、Python >=3.10 必须已安装。此分发包不自动安装解释器、不联网下载依赖、不自动改客户端或注册自启动。先读 [安装与迁移](references/install.md)。Windows 是已验证平台；其他系统及其他客户端版本仍需本机验证，不能继承作者的现场 PASS。

## 从零安装与启动

```text
node "<技能目录>/scripts/setup.mjs" --check
node "<技能目录>/scripts/setup.mjs" --init --umh-home "<本机数据目录>"
node "<技能目录>/scripts/umh.mjs" start --umh-home "<本机数据目录>"
```

`--check` 校验整包、依赖与解释器，不初始化数据。`--init` 只初始化指定数据根。服务使用期间必须运行；start 为前台运行，保持该终端或另行设置本用户后台启动。安装或配置变更须在用户任务授权范围内执行。

## 日常管理

```text
node "<技能目录>/scripts/umh.mjs" status --umh-home "<本机数据目录>"
node "<技能目录>/scripts/umh.mjs" review --umh-home "<本机数据目录>" list
node "<技能目录>/scripts/umh.mjs" review --umh-home "<本机数据目录>" decide --proposal <proposalId> --decision accept --reason "用户确认"
node "<技能目录>/scripts/umh.mjs" stop --umh-home "<本机数据目录>"
node "<技能目录>/scripts/umh.mjs" backup --umh-home "<本机数据目录>"
```

项目候选审核需加 `--project <实际projectId>`。backup 必须在服务停止后执行。remember 产生候选，不等于入库；只有真实审核后才能承诺可召回。读取工具包括 memory_status、memory_recall、memory_search、memory_get；写入工具为 memory_remember。

## 客户端从零接入

依 [接入与撤销](references/clients.md) 先识别本机客户端、实际变体与配置作用域，再生成 preview、审阅并备份、apply。手工 UI 连接还需要显式 `connect trust --confirm-reviewed` 发布受信绑定，再按本次 plan 的命令/参数配置客户端。

不要直接编辑数据库、复制他人的 bindingId、执行作者的配置脚本，或假设客户端已经接入。绑定对应实际受信工作区，环境字段为空；凭据仅保存在数据根私有管理面。复制技能本身不等于客户端连接已建立。

每台接收电脑都要在真实客户端新会话中验证工具发现、提交候选、审核及另一客户端新会话召回。SDK 协议测试只证明运行包功能，不作为原生客户端验收。

## 迁移、更新与撤销

只分享技能目录或配套 ZIP，不分享 `.umh`、客户端配置、日志、记忆或备份。移动前保留旧目录和回执，先逐连接安全disconnect并确认removed，再搬移并从零生成新计划；实际launch使用本机绝对路径。分享入口会拒绝对同一客户端/工作区的活动连接重新preview，避免覆盖旧回执。不要绕过入口直接调用package内CLI来覆盖记录。

更新前停服务并备份数据，替换程序后校验整包，再更新受影响 launch 路径。撤销客户端优先使用 connectionId 的 disconnect 与对账；手工 UI 先移除自有条目，再 `--confirm-manual-removal`。保留用户外部改动；reconcile/trust不复活removed，重新preview是新一轮接入，可能复用同一ID，旧计划与回执须另行保全。数据目录删除须另有明确授权。

运行边界：不自动充值、更换模型、登录、不关闭用户任务。Claude 适配器保留，但原生兼容性 UNVERIFIED、尚未验收，不声明支持完成。第三方归属见 `package/THIRD_PARTY_NOTICES.md`。
