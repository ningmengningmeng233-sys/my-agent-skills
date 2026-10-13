# 从零接入与撤销

先确定实际绝对工作区、数据根和配置路径；多个配置/profile 不能默认选第一个。CLI/桌面变体必须区分，历史测试结果不替接收电脑作保证。

| clientId | 实际variant | 接入方式 |
|---|---|---|
| cherry | desktop | 手工UI，配置MCP并绑定目标助手 |
| qoder | ide / cli | IDE手工UI；CLI独立检测，不以IDE安装替代CLI |
| zcode | cli / desktop | 显式选择用户/项目配置路径；只操作已授权作用域 |
| codex | cli / desktop | 先核实CODEX_HOME及实际配置/叠加方式 |
| minimax | desktop / cli | 桌面手工；CLI须选实际dataDir |
| dsh | cli | 显式profile的cordis.patch.yml，按本机计划指引 |

```text
node "<技能目录>/scripts/umh.mjs" connect qoder --variant ide --workspace "<绝对项目路径>" --dry-run --out "<绝对备份目录>/qoder-plan.json" --umh-home "<数据根>"
node "<技能目录>/scripts/umh.mjs" connect apply "<绝对备份目录>/qoder-plan.json"
```

自动端：审阅 plan.changes，备份实际文件后 apply，检查返回状态。若需指定profile/项目配置，在preview加 `--config-path <绝对文件路径>`，不要对照作者的配置猜路径。

手工端：changes=[]只表示UMH未自动写客户端，不表示不存在配置变更。审阅手工范围后：
```text
node "<技能目录>/scripts/umh.mjs" connect trust --connection <本次connectionId> --umh-home "<数据根>" --confirm-reviewed
```
确认binding_published，再经客户端官方MCP入口按本次plan.launch填 command/args/env。不要复制示例bindingId。Qoder版本不同，入口可能是扩展管理→连接器→添加自定义MCP；以实际UI为准。

policy块加到已授权的本项目规则文件，不覆盖原内容，不批量写用户真实项目：
```html
<!-- BEGIN UMH MEMORY POLICY v1 -->
- 在回答前可调用 memory_status/memory_recall/memory_search/memory_get 读取项目共享记忆；读取失败不得阻塞回答。
- 涉及用户偏好、项目决策、可复用事实时调用 memory_remember 提交记忆；一次只提交一条、正文自含。
- 绝不把凭据、密钥或认证材料写入记忆正文。
<!-- END UMH MEMORY POLICY v1 -->
```

真实客户端调用memory_remember提交候选→管理者review list/decide审核→另一客户端的新会话recall。项目候选使用实际projectId；global/project分别记录，不能在错误作用域找不到时判断提交丢失。

撤销：自动端 `disconnect --connection <id> --umh-home <根>`；手工端先在官方UI删本次条目/解除助手绑定，再同命令加 `--confirm-manual-removal`。最后 `connect reconcile --connection <id> --umh-home <根>` 应保持removed。外部配置修改引发冲突则停止覆盖，不用旧整文件盲目恢复。
