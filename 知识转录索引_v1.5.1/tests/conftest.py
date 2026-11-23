"""测试隔离：单测不碰开发机真实 SRC_DIR / 用户私有配置。"""
import atexit, os, shutil, tempfile

_SANDBOX = tempfile.mkdtemp(prefix="transcript-index-test-")
atexit.register(shutil.rmtree, _SANDBOX, ignore_errors=True)
_src = os.path.join(_SANDBOX, "vault")
_cfg = os.path.join(_SANDBOX, "config")
os.makedirs(_src, exist_ok=True)
os.makedirs(_cfg, exist_ok=True)
os.environ["SRC_DIR"] = _src
os.environ["TRANSCRIPT_INDEX_CONFIG_DIR"] = _cfg
