import subprocess
import sys
import os

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


def run_py_narrow_console(args, env, cwd=None, **kw):
    """刻意验「窄编码控制台」的子进程出口：不钉编码 flag，其余纪律与 run_py 相同。

    这类探针的全部意义就是绕开 `clean_env` 的钉死，去复现 gbk/cp936 终端下 CLI 会不会
    被 `UnicodeEncodeError` 打崩。所以 env 由调用方自己给（本函数不碰编码变量），
    流也**保持 bytes** 交回，由调用方按自己模拟的那个代码页解码。
    收在这里而不是散在测试里裸调 subprocess：编码范式门（G10）只认这一族出口。
    """
    kw.pop("text", None)            # 禁止调用方绕过 bytes 归一化
    kw.pop("encoding", None)
    kw.pop("universal_newlines", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=env,
                         capture_output=True, **kw)
    res.stdout = res.stdout or b""
    res.stderr = res.stderr or b""
    return res
