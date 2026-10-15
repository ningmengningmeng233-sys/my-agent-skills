import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(SKILL_DIR))
sys.path.insert(0, str(SKILL_DIR / "scripts"))
sys.path.insert(0, str(SKILL_DIR / "scripts" / "engine"))

# ---- 离线钉死（问题清单 B8）---------------------------------------------------
# 单测套件的运行前提：绝不发生真实外呼，且与本机钥匙串/中转池状态无关。
# 本段必须发生在任何被测模块导入之前（模块导入会触发 load_env 注入包外配置）：
#  1) 剥掉进程环境里所有 *_API_KEY 与隐式配置入口——需要密钥的用例自己显式注入；
#  2) ASSET_OFFLINE 置真：传输层在任何 socket 之前抛 OfflineRequested，
#     配置解析不再隐式回退技能目录 .env / ~/.asset-extract/.env（只认 ASSET_ENV_PATH）；
#  3) 代理钉到丢弃端口 9（漏网的外呼尝试会立即失败而不是计费），
#     NO_PROXY 保留 loopback，假端点照常工作。
for _name in [k for k in os.environ
              if k.upper().endswith("_API_KEY")
              or k.upper() in ("ASSET_ENV_PATH", "GOOGLE_GEMINI_BASE_URL",
                               "CODEX_GEMINI_BASE_URL")]:
    os.environ.pop(_name, None)
os.environ["ASSET_OFFLINE"] = "1"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
os.environ["http_proxy"] = "http://127.0.0.1:9"
os.environ["https_proxy"] = "http://127.0.0.1:9"
os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1"
os.environ["no_proxy"] = "127.0.0.1,localhost,::1"

# ---- B1 编码范式：子进程 Python 的编码由这里钉死，测试文件一律走 run_py 出口 ----
# 与 B站/抖音/去AI味 等仓同一套范式（治理仓 G10 认这个形状）。
# 本仓差异：`clean_env(base)` 收的是**整份环境**而非增量——上面那段离线钉死的产物是
# 「os.environ 剥掉若干 key」，用增量合并会把剥掉的 key 又加回来，等于放行真密钥。
_PYTHON_ENCODING_FLAGS = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONLEGACYWINDOWSSTDIO": "0",
}


def clean_env(base=None):
    """以调用方给的整份环境为底（缺省 os.environ），最后钉上编码三件套。"""
    env = dict(os.environ if base is None else base)
    env.update(_PYTHON_ENCODING_FLAGS)   # 放在最后：调用方覆盖不了
    return env


def run_py(args, cwd=None, env=None, **kw):
    """跑 Python 子进程的唯一出口：bytes 捕获后统一 utf-8 解码，不受 GBK 控制台影响。

    `env` 是整份环境（本仓用例自己剥 key）；text/encoding/capture_output 由本函数
    统一接管，调用方传了会被剥掉，避免两套解码口径。
    """
    kw.pop("text", None)
    kw.pop("encoding", None)
    kw.pop("errors", None)
    kw.pop("universal_newlines", None)
    kw.pop("capture_output", None)
    res = subprocess.run([sys.executable, *args], cwd=cwd, env=clean_env(env),
                         capture_output=True, **kw)
    res.stdout = (res.stdout or b"").decode("utf-8", "replace")
    res.stderr = (res.stderr or b"").decode("utf-8", "replace")
    return res


# 最小合法 PNG（含正确魔数，可被魔数嗅探识别）
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
# 真实可解码的 4x4 PNG（发布器要核验尺寸，必须能被 Pillow 打开）
TINY_PNG = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")


def write_utf8(p: Path, content: str = "内容") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def accept_task_with_artifact(lib: Path, task_id: str, status: str = "READY_AS_REFERENCE",
                              image: bytes | None = FAKE_PNG) -> None:
    """把任务置为已通过，并写入**真实可核验**的产物（文件 + 匹配哈希）。

    视觉交付门与编译链都会核验「产物真实存在且哈希一致」：只改状态不写产物
    会被判为无法核验（REFERENCE_MISSING），因此夹具必须落真实文件。

    `image=None` 时复用该任务**已有的** attempt 文件（已真实导入过产物的场景），
    只按现有字节登记哈希，避免覆盖调用方刚导入的图片。
    """
    import hashlib

    from scripts.engine.visual_schema import (
        AttemptRecord,
        VisualTask,
        load_visual_plan,
        write_visual_plan,
    )
    from scripts.engine.visual_store import attempt_dir

    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    task = plan.by_id(task_id)

    if image is None:
        existing = task.latest_attempt
        if existing is None or not (lib / existing.image).is_file():
            raise AssertionError(f"任务 {task_id} 尚无已导入产物，无法复用")
        rel = existing.image
        rounds = len(task.attempts)
    else:
        rel = f"{attempt_dir(task_id, 1)}/image.png"
        rounds = 1

    target = lib / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if image is not None:
        target.write_bytes(image)
    data = target.read_bytes()
    attempt = AttemptRecord(round=rounds, image=rel,
                            image_sha256=hashlib.sha256(data).hexdigest(),
                            model="manual", provider="manual")
    prior = tuple(task.attempts[:-1]) if image is None else ()
    write_visual_plan(plan.with_task(VisualTask(
        **{**task.__dict__, "status": status, "attempts": (*prior, attempt)})), plan_path)


def accept_all_with_artifacts(lib: Path, status: str = "READY_AS_REFERENCE") -> None:
    """把计划内全部任务置为已通过，并为每个任务写入真实产物（**不走发布器**）。

    仅用于「渲染 / 状态标签」这类不经过交付门的用例；交付门用例请用
    `publish_all_with_reviews`（阶段 2 起「已通过」必须来自发布器）。
    """
    from scripts.engine.visual_schema import load_visual_plan

    plan_path = lib / "视觉资产" / "计划.toml"
    for task_id in [t.task_id for t in load_visual_plan(plan_path).tasks]:
        accept_task_with_artifact(lib, task_id, status=status)


def publish_task_with_review(lib: Path, task_id: str, decision: str = "ACCEPTED",
                             reviewer: str = "单元测试主模型", image: bytes | None = None) -> str:
    """走完整发布链：落产物 → 出审核请求 → 写审核结果 → 发布正式版本。

    交付门（阶段 2 工单 2.4）会交叉验证正式版本清单，因此「已通过」的夹具必须经过发布器。
    """
    import hashlib
    import json

    from scripts.engine.review_contract import (
        build_review_request,
        load_review_result,
        result_path,
    )
    from scripts.engine.review_contract import CHECKLIST_ITEMS
    from scripts.engine.visual_publisher import publish_task
    from scripts.engine.visual_schema import (
        AttemptRecord,
        VisualTask,
        load_visual_plan,
        write_visual_plan,
    )
    from scripts.engine.visual_store import attempt_dir

    plan_path = lib / "视觉资产" / "计划.toml"
    plan = load_visual_plan(plan_path)
    task = plan.by_id(task_id)

    if image is None and task.latest_attempt and (lib / task.latest_attempt.image).is_file():
        rel = task.latest_attempt.image
        round_no = len(task.attempts)
    else:
        data = image if image is not None else TINY_PNG
        round_no = len(task.attempts) + 1
        rel = f"{attempt_dir(task_id, round_no)}/image.png"
        target = lib / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    payload = (lib / rel).read_bytes()
    task = VisualTask(**{**task.__dict__,
                         "prompt": task.prompt or f"【任务目标】{task.kind}（单元测试）",
                         "attempts": (AttemptRecord(round=round_no, image=rel,
                                                    image_sha256=hashlib.sha256(payload).hexdigest(),
                                                    model="unit-test", provider="unit-test"),)})
    plan = plan.with_task(task)
    request = build_review_request(lib, plan, task)
    review = {
        "schema_version": 1, "task_id": task_id, "decision": decision,
        "binding": request.binding.as_dict(),
        "checks": [{"item": item, "verdict": "pass", "evidence": "夹具"}
                   for item in CHECKLIST_ITEMS],
        "issues": [], "reviewer": reviewer, "reviewed_at": "2026-09-18T00:00:00Z",
    }
    out = result_path(lib, task_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8", newline="\n")
    plan, record = publish_task(lib, plan, task, request, load_review_result(out))
    write_visual_plan(plan, plan_path)
    return record.version


def publish_all_with_reviews(lib: Path, decision: str = "ACCEPTED") -> int:
    """把计划内全部任务按依赖顺序走发布器（上游先发布，下游才能发布）。"""
    from scripts.engine.visual_schema import load_visual_plan

    plan_path = lib / "视觉资产" / "计划.toml"
    published = 0
    for _ in range(len(load_visual_plan(plan_path).tasks) + 1):
        plan = load_visual_plan(plan_path)
        pending = [t for t in plan.tasks if not t.is_accepted]
        if not pending:
            break
        progress = False
        for task in sorted(pending, key=lambda t: (len(t.depends_on), t.task_id)):
            by_id = {t.task_id: t for t in plan.tasks}
            if any(not by_id[d].is_accepted for d in task.depends_on if d in by_id):
                continue
            publish_task_with_review(lib, task.task_id, decision=decision)
            published += 1
            progress = True
            break
        if not progress:
            break
    return published


def make_delivered_lib(root: Path, style: str = "H7") -> Path:
    """构造完整可交付资产库（含有效交付清单）。

    交付门用例的唯一事实源：指纹由 _current_fingerprints 现算，清单与产物天然自洽。
    必需检查集合直接读签发契约常量：契约扩项时本夹具自动跟随，不再各抄一份名单。
    """
    from scripts.engine.pipeline_state import REQUIRED_DELIVERY_CHECKS, _current_fingerprints

    lib = root / "资产库"
    write_utf8(lib / "原文段" / "小说-作者" / "段01.md", "# 第一章\n张三进茶楼")
    write_utf8(lib / "小说-作者-登记表.md", "| 主名 |\n| 张三 |")
    write_utf8(lib / "段产物" / "小说-作者" / "段01-资产记录.md", "| 类型 | 名称 |\n| 人物 | 张三 |")
    write_utf8(lib / "反推_最终.md", "| CH_张三 | 张三 |\n")
    index = {
        "schema_version": 1, "source_fingerprint": "sha256:x",
        "assets": [{
            "asset_id": "CH_张三", "stable_key": "CHR:00001:1", "type": "人物",
            "name": "张三", "priority": "P1", "fact_level": "剧本事实",
            "evidence": [{"segment": "段01.md", "chapter": "第1章", "line": "00001", "quote": "张三进茶楼"}],
            "dependencies": [], "relations": [], "status": "CONFIRMED",
        }],
    }
    write_utf8(lib / ".process" / "资产索引.json", json.dumps(index, ensure_ascii=False))
    write_utf8(lib / "描述" / "人物描述.md", "## 001. 张三\n\n正文\n")
    write_utf8(lib / "提示词" / "基础" / "人物提示词.md", "## 001. 张三\n\n正文\n")
    seg = lib / "原文段" / "小说-作者" / "段01.md"
    write_utf8(lib / ".start-state.json",
               json.dumps({"style": style, "input_source": str(seg)}, ensure_ascii=False))
    cur = _current_fingerprints(lib)
    manifest = {
        "schema_version": 1, "source_fingerprint": cur["source_fingerprint"],
        "style_fingerprint": cur["style_fingerprint"], "style": style,
        "asset_index_hash": cur["asset_index_hash"], "artifact_hashes": cur["artifact_hashes"],
        "counts": {}, "checks": [{"name": n, "rc": 0} for n in REQUIRED_DELIVERY_CHECKS],
        "generated_at": "t", "status": "DELIVERED",
    }
    write_utf8(lib / "交付清单.json", json.dumps(manifest, ensure_ascii=False))
    return lib


@pytest.fixture
def delivered_lib(tmp_path: Path) -> Path:
    """已签发交付清单的资产库（数据交付线 = DELIVERED）。"""
    return make_delivered_lib(tmp_path)


# ---------------- 通用假端点（加速改法工单 T1 引入） ----------------

def _sse_body(text="正文", usage=None):
    """按 OpenAI 流式格式拼 SSE 响应体。"""
    chunks = [f'data: {json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)}\n\n']
    if usage is not None:
        chunks.append(f'data: {json.dumps({"choices": [{"delta": {}}], "usage": usage}, ensure_ascii=False)}\n\n')
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode("utf-8")


class _FakeHandler(BaseHTTPRequestHandler):
    scripted = []      # 依次消费：{"status": int, "text": str, "usage": dict, "sleep": float, "body": str}
    received = []      # 收到的请求体（dict）
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, *a):   # 关掉默认访问日志
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        with _FakeHandler.lock:
            _FakeHandler.received.append(json.loads(raw.decode("utf-8") or "{}"))
            _FakeHandler.in_flight += 1
            _FakeHandler.peak = max(_FakeHandler.peak, _FakeHandler.in_flight)
            item = _FakeHandler.scripted.pop(0) if _FakeHandler.scripted else {}
        time.sleep(float(item.get("sleep") or 0))
        status = int(item.get("status") or 200)
        if item.get("body") is not None:
            payload = str(item["body"]).encode("utf-8")
        else:
            payload = _sse_body(item.get("text", "正文"), item.get("usage"))
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream" if status == 200 else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        with _FakeHandler.lock:
            _FakeHandler.in_flight -= 1


@pytest.fixture
def fake_endpoint():
    """启动本地假端点；用完即关。用法：fake_endpoint.url / .script([...]) / .peak / .received"""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _FakeHandler.scripted = []
    _FakeHandler.received = []
    _FakeHandler.in_flight = 0
    _FakeHandler.peak = 0

    class _Fake:
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"

        @staticmethod
        def script(items):
            _FakeHandler.scripted = list(items)

        @staticmethod
        def received():
            return list(_FakeHandler.received)

        @staticmethod
        def peak():
            return _FakeHandler.peak

    yield _Fake()
    server.shutdown()
    server.server_close()
