# Third-Party Notices

Universal Memory Hub (UMH) v1 复用与依赖的第三方作品及其许可证。
本文件由 C01 工单生成；迁移任何上游文件时必须同步更新（保留版权声明、SHA、
许可证与本地修改记录，见 `docs/upstream-audit/license-audit.md`）。

## 上游仓库（候选复用源）

### dsh-memory（首选 engine；本仓库 md_cg 运行单元的来源）

- 来源：https://github.com/FuRongJun-1999/dsh-memory.git
- 固定版本：commit `bfbdcb923da1ef2880dd3f9f979cf419d9211eb0`
- 许可证：MIT License — Copyright (c) 2026 FuRongJun-1999
- 许可证副本：`licenses/dsh-memory-LICENSE`
  （SHA256 `9a665e6361493b883988ef5b5b306e75138619b808cdd68d449752ed2be1c42c`）

```
MIT License

Copyright (c) 2026 FuRongJun-1999

（全文见 licenses/dsh-memory-LICENSE）
```

### agent-memory-bridge（v1 不依赖；历史导入阶段再评估）

- 来源：https://github.com/54Lynnn/agent-memory-bridge.git
- 固定版本：commit `989a9ba47e0a73e7df441821f79ad5672b6644a5`
- 许可证：MIT License — Copyright (c) 2026 Agent Memory Bridge Contributors
- 许可证副本：`licenses/agent-memory-bridge-LICENSE`
  （SHA256 `14f1685e06bf548837fe2e59df1477313754e83457003c3c5646b08e78c56f3c`）

### memorax-code（选择性复用锁/原子写/配置工具）

- 来源：https://github.com/memorax-ai/memorax-code.git
- 固定版本：commit `e09d2603292f21c71897c56d83d9dac93546de00`
- 许可证：MIT License — Copyright (c) 2026 MemoraX AI
- 许可证副本：`licenses/memorax-code-LICENSE`
  （SHA256 `6517c5f3904cee570626849cb080b5f0cfc5d3b69187cea5dbea51524a54624b`）

### dsh-memory 内的非 MIT 文件（随引擎单元分发时必须保留本节署名）

| 文件 | 许可证 | 署名/约束 |
|---|---|---|
| `md_cg/lexicon/cedict_en_zh.json` | CC BY-SA 4.0 | 派生自 CC-CEDICT（https://www.mdbg.net.cn/english-and-chinese-dictionary/cc-cedict ）的英→中反查表（28,294 条，构建脚本 `md_cg/lexicon/build_cedict_en_zh.py`）。分发该派生数据须保留 CC-CEDICT 署名并以 CC BY-SA 4.0 分发；不得并入 MIT 内联词表 |
| `md_cg/whitebox_kb/seed_knowledge/智能论3.4.md` | CC0 1.0 | 公共领域贡献，无署名义务；保留原文件头注 |

dsh-memory 的 benchmark 数据集 `data/benchmarks/locomo-zh-500/` 为
CC BY-NC 4.0（署名+非商业）：**不在**引擎单元内（单元配方排除 test*/bench*），
严禁随 UMH 发布包分发。

## npm 依赖（v1 公共层；C02 起安装，版本见 DEPENDENCIES-PIN.json）

| 包 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| @modelcontextprotocol/sdk | 1.31.0 | MIT | MCP Server/Client（v1 单包代际） |
| zod | 3.25.76 | MIT | SDK peer 依赖（工具 schema） |
| typescript | 5.9.3 | Apache-2.0 | 构建编译器（devDependency） |
| smol-toml | 1.9.0 | BSD-3-Clause | TOML 配置解析 |
| yaml | 2.9.1 | ISC | YAML 配置解析 |

各 npm 包的许可证全文随安装时的包内 LICENSE 分发（node_modules 未随 UMH
发布包复制时，以 registry 对应版本的 license 元数据为准，见
DEPENDENCIES-PIN.json）。

## Python 引擎

md_cg 运行单元零第三方 pip 依赖（仅 Python 标准库；逐文件 import 审计见
`docs/upstream-audit/license-audit.md`）。`data/policy.json` 为 dsh-memory
仓库的数据文件，随上述 MIT 许可证分发。

## 已 vendored 引擎单元（C04；vendor/mdcg）

- 来源：dsh-memory @ `bfbdcb923da1ef2880dd3f9f979cf419d9211eb0`（本文件上方
  「上游仓库」节的同一固定版本），由 `scripts/vendor-mdcg.py` 从只读 clone
  按 C01 清单（`docs/upstream-audit/engine-contract.json` 的
  distributionFiles + resourceFiles，共 591 个文件）逐字节复制并逐文件核验
  SHA256；单元聚合哈希与文件数登记于 `UPSTREAM-PIN.json` 的
  `vendoredEngine` 节（`--check` 可随时复核）。
- vendored 副本保留上游根 LICENSE（MIT，见 `licenses/dsh-memory-LICENSE`）
  与全部原文件内容，本地修改为零。
- 单元内的非 MIT 文件随单元原样携带，署名义务见上方「dsh-memory 内的非 MIT
  文件」表：`md_cg/lexicon/cedict_en_zh.json`（CC BY-SA 4.0）与
  `md_cg/whitebox_kb/seed_knowledge/智能论3.4.md`（CC0 1.0）。
- 排除项不变：CC BY-NC benchmark 数据（`data/benchmarks/`）与
  test*/bench*/__pycache__ 不在清单内，vendor 脚本对清单与 vendor 树双向
  强制拒绝（误入即 exit 1）。
- 运行期产物（`__pycache__`、节点存储、审核队列、令牌文件）一律写在
  `MDCG_ROOT`/`MDCG_AUX_ROOT` 等实例根，不写入 vendor 目录
  （worker env 含 `PYTHONDONTWRITEBYTECODE=1`）。

## 说明

- 三个上游根 LICENSE 均为 MIT，但 MIT 不自动覆盖「未带许可头的单文件」之外
  的所有特殊情形；逐文件审计与本地修改记录见
  `docs/upstream-audit/license-audit.md`，本文件不替代该审计。
- 本项目自身的许可证选择不在 C01 范围。
