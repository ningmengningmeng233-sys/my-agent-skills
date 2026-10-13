# 全局记忆 UMH：交给其他人的版本

将整个目录或同名 ZIP 发送给接收者，解压到稳定目录，再将这个技能目录放进其 AI 客户端支持的 skills 目录或让 AI 阅读 SKILL.md。不要只复制 SKILL.md。

目录内已经包含运行程序、Node 依赖和 Python 引擎。接收者仍需自行安装 Node >=24.2 和 Python >=3.12（下限依据 package/DEPENDENCIES-PIN.json 的 runtime.python.minimum；无 3.10 实测，不下调）。无需原仓库、不需要编译、不需要 npm install、不需要作者的用户名或盘符；没有用户数据或密钥。

首次运行：
```text
node "<解压目录>/scripts/setup.mjs" --check
node "<解压目录>/scripts/setup.mjs" --init
node "<解压目录>/scripts/umh.mjs" start
```

默认数据根是接收者家目录中的 `.umh`；不是技能目录。自定义数据根时，每条命令都应使用相同的 `--umh-home <绝对路径>`。

start 运行期间需保持进程存活，另开终端执行 status、接入和审核。接收者的六端接入尚未验证，按 references/clients.md 从零生成本机计划。

能力基于 Windows 六类客户端现场验证；不同版本需重新核验。Claude 原生兼容性延期未验证。macOS/Linux 的本分发流程未实测。

`DISTRIBUTION.json` 记录程序来源、固定依赖、内容 SHA256；`node scripts/verify-bundle.mjs` 检查整包。解压后不要修改 package 或脚本，否则完整性检查会拒绝；正常用户数据和客户端 plan 放在目录之外。
