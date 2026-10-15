import os
import shutil
import subprocess
import sys
import unittest

# 将项目根目录加入模块搜索路径
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

# 子进程 Python 编码三件套：任何调用方（含 extra）都不得覆盖
_PYTHON_ENCODING_FLAGS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONLEGACYWINDOWSSTDIO": "0",
}


def clean_env(**extra):
    """以 os.environ 为底，强制钉死 Python 子进程编码 flag。

    先写入三个编码 flag，再 merge 调用方 extra，最后把这三个 key 再钉一次——
    保证调用方即使显式传 PYTHONIOENCODING="gbk" 也覆盖不了。
    """
    env = dict(os.environ)
    env.update(_PYTHON_ENCODING_FLAGS)
    env.update(extra)
    env.update(_PYTHON_ENCODING_FLAGS)  # merge 之后再钉一次，禁止被覆盖
    return env


def run_py(args, cwd=None, **kw):
    """跑 Python 子进程的唯一出口：永不返回 None 流，永不受 GBK 控制台影响。

    内部禁止 text=True / encoding=：统一以 bytes 捕获，再用
    decode("utf-8", "replace") 原地转成 str，保证 stdout/stderr 恒为 str。
    """
    env = clean_env(**(kw.pop("env", None) or {}))
    kw.pop("text", None)            # 禁止调用方绕过 bytes 归一化
    kw.pop("encoding", None)
    kw.pop("universal_newlines", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                         capture_output=True, **kw)
    res.stdout = (res.stdout or b"").decode("utf-8", "replace")
    res.stderr = (res.stderr or b"").decode("utf-8", "replace")
    return res


def _modules_ready(*names):
    import importlib.util

    return all(importlib.util.find_spec(n) is not None for n in names)


#: E 级能力登记表：豁免只准点名这里存在的能力，写不出名字就是假豁免。
_CAPABILITIES = {
    "ocr-engine": lambda: _modules_ready("rapidocr", "onnxruntime"),
    "installed-doctor": lambda: (shutil.which("douyin-ingest") is not None
                                 and _modules_ready("rapidocr", "onnxruntime")),
}


def capability_ready(name):
    """实测某项本机能力是否具备；未登记直接抛错，探测本身炸了算不具备。"""
    if name not in _CAPABILITIES:
        raise KeyError(f"未登记的能力: {name}（E 级豁免必须点名具体能力，"
                       f"可选: {', '.join(sorted(_CAPABILITIES))}）")
    try:
        return bool(_CAPABILITIES[name]())
    except Exception:
        return False


def requires_env(capability, reason):
    """E 级用例唯一入口：本机缺能力时 SKIP 并写明原因——SKIP 不等于产品通过。

    用 unittest.skipUnless 而非 pytest.mark.skipif：普通函数与 unittest.TestCase
    方法都吃它（实测），一处工厂覆盖两种写法。
    """
    return unittest.skipUnless(capability_ready(capability), f"SKIP(E): {reason}")
