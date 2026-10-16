"""测试隔离：common.py 在导入期就要求 SRC_DIR 指向一个已存在的源目录（作者本机是 Obsidian vault）。

单测只覆盖纯函数，不读写真实资料库；这里在任何被测模块导入之前，
把 SRC_DIR 指向一次性临时目录，并把用户私有配置目录指向空目录，
避免 CI 因缺少作者本机配置在收集阶段报错，也避免开发机上误读/误写真实 vault。
"""
import atexit
import os
import shutil
import tempfile

_SANDBOX = tempfile.mkdtemp(prefix="transcript-index-test-")
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)

_src = os.path.join(_SANDBOX, "vault")
_cfg = os.path.join(_SANDBOX, "config")
os.makedirs(_src, exist_ok=True)
os.makedirs(_cfg, exist_ok=True)

os.environ["SRC_DIR"] = _src
os.environ["TRANSCRIPT_INDEX_CONFIG_DIR"] = _cfg
