# 安装与自证（本候选）

- 本包**未**经过任何真实安装动作；安装矩阵为 NOT_RUN。解压后可直接读。
- 前置：Python 3.10 或以上；工作目录固定为**解包后的包根目录**（含 SKILL.md 的那一层）。
- 以下命令均按字面执行：解释器用你自己环境里的 `python`（Windows 可用 `py`），不需要任何外部预设变量。

## 1. 建环境与依赖

```
python -m venv .venv
python -m pip install -r requirements.txt
python -m pytest tests
python -m pytest tests/test_upstream_checkpoint.py tests/test_ops.py
```

## 2. 验收命令与预期

| 命令 | 预期退出码 | 预期计数/输出 | 副作用与资源边界 | 需要网络 | 额外验收依赖 |
|---|---|---|---|---|---|
| `python -m venv .venv` | 0 | 生成 .venv 目录 | 仅在包根内新增 .venv | 否 | - |
| `python -m pip install -r requirements.txt` | 0 | 装完包内声明的依赖 | 写入 .venv 与 pip 缓存目录 | 是 | - |
| `python -m pytest tests` | 0 | 59 passed, 1 skipped，必须有最终计数行；本仓 addopts 已含 -q，命令再写 -q 会变 -qq 而吞掉汇总 | 测试自建合成库 | 否 | pytest |
| `python -m pytest tests/test_upstream_checkpoint.py tests/test_ops.py` | 0 | 双文件 passed | 临时断点文件 | 否 | - |

## 3. 口径说明

- 本轮前的旧包一条收件人可执行命令都没有；本说明取代它（旧 ZIP 保持原样，不回填旧 SHA）
- 离线 fixture 轮与普通环境轮分别执行、分别记结果；普通环境不偷设字节码外置、不注入未申报的开关。
- 任何疑似凭据的串只做本地一致性检查：不远程验证、不轮换、不与真实凭据库比对（NOT_RUN）。
