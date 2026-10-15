#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阶段 5 工单 5.1：单写者调度器与事件日志（规格 15.4）。

- 并发工作器不得直接修改计划：工作器只返回不可变事件，唯一协调器持锁、
  校验 revision、原子写计划与事件日志。
- 事件日志是崩溃恢复的事实源：图片已落盘但计划未写入时，恢复不得重复请求图片。
- 上游失败只阻塞下游，无关任务继续；输入未变的已通过任务直接复用。
"""
import base64
import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import run_py  # B1 范式：起 Python 子进程的唯一出口

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import scripts.engine.visual_schema as schema_mod  # noqa: E402
import scripts.engine.visual_scheduler as scheduler_mod  # noqa: E402
from scripts.engine.image_executor import (  # noqa: E402
    GENERATION_REJECTED,
    RATE_LIMITED,
    GenerationError,
    GenerationRequest,
    GenerationResult,
)
from scripts.engine.review_contract import (  # noqa: E402
    CHECKLIST_ITEMS,
    result_path,
)
from scripts.engine.visual_events import (  # noqa: E402
    EVENT_KINDS,
    IMAGE_STORED,
    PUBLISHED,
    REVIEW_IMPORTED,
    TASK_BLOCKED,
    TRANSPORT_FAILED,
    VisualEvent,
    append_events,
    apply_events,
    event_log_path,
    read_events,
)
from scripts.engine.visual_publisher import PublishError, current_release  # noqa: E402
from scripts.engine.visual_scheduler import (  # noqa: E402
    SchedulerError,
    SchedulerSummary,
    VisualScheduler,
    plan_lock,
)
from scripts.engine.visual_schema import (  # noqa: E402
    AttemptRecord,
    VisualPlan,
    VisualTask,
    load_visual_plan,
)
from scripts.engine.visual_store import attempt_dir  # noqa: E402

# 真实可被 Pillow 解码的 4x4 PNG（发布器要核验尺寸，不能用假魔数）
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGNsaGhggAEmOAsvBwBMBAGIxu9U"
    "aAAAAABJRU5ErkJggg==")


def _distinct_png(seq: int) -> bytes:
    """按调用序号产出不同字节的合法 PNG。

    真实生成器每轮产出的图片必然不同；用同一份字节会让相邻轮次共享六项绑定，
    旧轮审核结果就会被误绑到新一轮——那是夹具假象，不是产品行为。
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (seq % 256, (seq * 7) % 256, (seq * 13) % 256)).save(
        buffer, format="PNG")
    return buffer.getvalue()


STYLE_HASH = "a" * 64
STYLE_NAME = "仙侠国风电影CG"
TASK_ID = "CH_林姑娘-正脸-v01"
PLAN_REL = ("视觉资产", "计划.toml")


def _task(task_id=TASK_ID, **kw):
    base = dict(task_id=task_id, asset_id="CH_林姑娘", kind="正脸", status="PLANNED",
                aspect_ratio="1:1", style_slug=STYLE_NAME, style_version="1.0.0",
                style_hash=STYLE_HASH, prompt="【任务目标】正脸身份近景",
                fact_source=".process/资产索引.json#CH_林姑娘")
    base.update(kw)
    return VisualTask(**base)


def _plan(*tasks):
    return VisualPlan(schema_version=1, style_name=STYLE_NAME, style_slug=STYLE_NAME,
                      style_version="1.0.0", style_hash=STYLE_HASH, tasks=tuple(tasks))


def _independent_tasks(count):
    return tuple(_task(task_id=f"CH_角色{i:02d}-正脸-v01", asset_id=f"CH_角色{i:02d}")
                 for i in range(1, count + 1))


def _request_factory(task, plan):
    """测试用请求编译器：不依赖资产索引与画风包，把轮次写进提示词。

    轮次必须进提示词：真实返工每轮都要带定向修正指令（否则「同一批指令重复」应判无效），
    这样六项绑定也能区分同一任务的相邻轮次，旧轮审核结果不会误绑到新一轮。
    """
    _ = plan
    return GenerationRequest(task_id=task.task_id,
                             prompt=f"{task.prompt}｜第 {task.round_count + 1} 轮",
                             negative_prompt=task.negative_prompt,
                             aspect_ratio=task.aspect_ratio,
                             style_name=task.style_slug, style_version=task.style_version)


def _fail_compile(task, plan):
    raise ValueError(f"MISSING_REFERENCE：{task.task_id} 参考图缺失")


class _FakeExecutor:
    """记录请求并返回真实 PNG；可注入并发屏障、故障码与故障任务。"""

    def __init__(self, image=PNG_BYTES, barrier=None, failures=None):
        self.image = image
        self.barrier = barrier
        self.failures = dict(failures or {})
        self.requests: list[GenerationRequest] = []
        self.inflight = 0
        self.max_inflight = 0
        self.barrier_broken = False
        self._lock = threading.Lock()

    def generate(self, request: GenerationRequest) -> GenerationResult:
        with self._lock:
            self.requests.append(request)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            code = self.failures.get(request.task_id, "")
            seq = len(self.requests)
        try:
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            self.barrier_broken = True
        finally:
            with self._lock:
                self.inflight -= 1
        if code:
            raise GenerationError(code, f"{request.task_id} 注入故障")
        return GenerationResult(provider="fake", model="fake-model",
                                image_bytes=_distinct_png(seq),
                                mime_type="image/png", response_id=f"r{seq}", status="OK")


def _scheduler(lib, executor=None, factory=_request_factory):
    return VisualScheduler(lib, executor=executor or _FakeExecutor(),
                           request_factory=factory)


def _plan_path(lib):
    return Path(lib).joinpath(*PLAN_REL)


def _load(lib):
    return load_visual_plan(_plan_path(lib))


def _approve(lib, task_id, decision="ACCEPTED", issues=()):
    """按已落盘的审核请求写出审核结果（模拟当前主模型读图后回写）。"""
    request_file = Path(lib) / "视觉资产" / "审核请求" / task_id / "review-request.json"
    assert request_file.is_file(), f"审核请求未导出：{request_file}"
    request = json.loads(request_file.read_text(encoding="utf-8"))
    payload = {
        "schema_version": 1, "task_id": task_id, "decision": decision,
        "binding": request["binding"],
        "checks": [{"item": item, "verdict": "pass", "evidence": "单元测试主模型"}
                   for item in CHECKLIST_ITEMS],
        "issues": [dict(row) for row in issues],
        "reviewer": "单元测试主模型", "reviewed_at": "2026-09-18T00:00:00Z",
    }
    out = result_path(lib, task_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n")


# --- 事件契约 -------------------------------------------------------------------

def test_event_kinds_cover_the_seven_contract_names():
    assert set(EVENT_KINDS) == {"GENERATION_STARTED", "TRANSPORT_FAILED", "IMAGE_STORED",
                                "REVIEW_REQUESTED", "REVIEW_IMPORTED", "PUBLISHED",
                                "TASK_BLOCKED"}


def test_event_ids_are_deterministic_and_payload_digest_bound():
    one = VisualEvent.create(IMAGE_STORED, TASK_ID, revision=1,
                             payload={"round": 1, "image_sha256": "b" * 64})
    two = VisualEvent.create(IMAGE_STORED, TASK_ID, revision=1,
                             payload={"round": 1, "image_sha256": "b" * 64})
    other = VisualEvent.create(IMAGE_STORED, TASK_ID, revision=1,
                               payload={"round": 2, "image_sha256": "b" * 64})
    assert one.event_id == two.event_id, "同一事实必须得到同一 event_id（重放幂等的前提）"
    assert one.event_id != other.event_id


def test_event_log_append_is_idempotent_by_event_id(tmp_path):
    lib = tmp_path / "资产库"
    event = VisualEvent.create(IMAGE_STORED, TASK_ID, payload={"round": 1})
    from scripts.engine.visual_events import append_events

    assert append_events(lib, (event, event)) == 1, "同一 event_id 只落盘一次"
    assert append_events(lib, (event,)) == 0
    assert len(read_events(lib)) == 1
    assert event_log_path(lib).name == "视觉事件.jsonl"
    assert event_log_path(lib).parent.name == ".process"


def test_event_log_ignores_half_written_trailing_line(tmp_path):
    from scripts.engine.visual_events import append_events

    lib = tmp_path / "资产库"
    append_events(lib, (VisualEvent.create(IMAGE_STORED, TASK_ID, payload={"round": 1}),))
    path = event_log_path(lib)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"event_id": "半行", "kind": "IMAGE_STOR')  # 进程中断留下的半行
    events = read_events(lib)
    assert len(events) == 1, "异常退出的半行事件必须忽略"


def test_apply_events_is_idempotent_for_image_stored():
    plan = _plan(_task())
    event = VisualEvent.create(
        IMAGE_STORED, TASK_ID, payload={"round": 1, "image": "视觉资产/任务/占位/image.png",
                                        "image_sha256": "c" * 64, "provider": "fake"})
    once = apply_events(plan, (event,))
    twice = apply_events(once, (event,))
    task = twice.by_id(TASK_ID)
    assert task.status == "WAITING_REVIEW"
    assert len(task.attempts) == 1, "同一轮次不得重复追加 attempts"
    assert task.attempts[0].round == 1
    assert once.tasks == twice.tasks


def test_published_event_routes_through_whitelist_and_replays_idempotently():
    events = (
        VisualEvent.create(IMAGE_STORED, TASK_ID,
                           payload={"round": 1, "image": "视觉资产/任务/占位/image.png",
                                    "image_sha256": "c" * 64}),
        VisualEvent.create(REVIEW_IMPORTED, TASK_ID, payload={"decision": "ACCEPTED"}),
        VisualEvent.create(PUBLISHED, TASK_ID, payload={"version": "attempt-01"}),
    )
    plan = apply_events(_plan(_task()), events)
    task = plan.by_id(TASK_ID)
    assert task.status == "READY_AS_REFERENCE", "发布必须经状态机白名单走到「可作为参考」"
    assert task.current_version == "attempt-01"
    assert apply_events(plan, events).tasks == plan.tasks, "重放同一批事件必须是空操作"


def test_plan_revision_round_trips_through_toml(tmp_path):
    path = _plan_path(tmp_path)
    plan = _plan(_task())
    assert plan.revision == 0, "缺省 revision 为 0（旧计划零改动可读）"
    schema_mod.write_visual_plan(replace(plan, revision=3), path)
    assert load_visual_plan(path).revision == 3


# --- 计划锁 ---------------------------------------------------------------------

def test_plan_lock_is_reentrant_in_thread_but_exclusive_across_threads(tmp_path):
    """同一线程可重入（调度器持锁时发布器再取一次不得自锁），其他线程必须互斥。"""
    lib = tmp_path / "资产库"
    with plan_lock(lib):
        assert (lib / ".process" / "视觉调度.lock").is_file()
        with plan_lock(lib, timeout=1.0):
            pass
        busy: list[SchedulerError] = []

        def other():
            try:
                with plan_lock(lib, timeout=0.3):
                    pass
            except SchedulerError as exc:
                busy.append(exc)

        thread = threading.Thread(target=other, daemon=True)
        thread.start()
        thread.join(10)
        assert busy and busy[0].code == "SCHEDULER_LOCK_BUSY", "其他线程必须被挡住"
    # 锁文件按设计常驻（内核锁才是互斥本体）；释放后必须能再次获取
    with plan_lock(lib, timeout=1.0):
        pass


def test_leftover_lock_file_without_holder_does_not_block(tmp_path):
    """上一个进程崩溃留下的锁文件不得阻塞：内核锁随进程退出自动释放。"""
    lib = tmp_path / "资产库"
    leftover = lib / ".process" / "视觉调度.lock"
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text('{"pid": 999999, "acquired_at": "2000-01-01T00:00:00Z"}',
                        encoding="utf-8")
    old = time.time() - 7200
    os.utime(leftover, (old, old))
    with plan_lock(lib, timeout=2.0):
        pass


# --- 并发与单写者 ---------------------------------------------------------------

def test_lock_is_released_when_holder_process_dies(tmp_path):
    """持锁进程崩溃（`os._exit` 跳过 finally）后内核自动释放锁，后续进程立即可获取。"""
    lib = tmp_path / "资产库"
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})\n"
        "import os\n"
        "from scripts.engine.visual_scheduler import plan_lock\n"
        f"with plan_lock({str(lib)!r}):\n"
        "    os._exit(0)\n"
    )
    proc = run_py(["-c", script], cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    assert (lib / ".process" / "视觉调度.lock").is_file(), "崩溃进程留下了锁文件"
    with plan_lock(lib, timeout=2.0):
        pass


def test_concurrent_lock_acquisition_never_yields_two_holders(tmp_path):
    """多线程同时抢锁时，临界区里永远只能有一个持有者；且不得死锁。"""
    lib = tmp_path / "资产库"
    state = {"now": 0, "max": 0}
    guard = threading.Lock()
    errors = []

    def worker():
        try:
            for _ in range(3):
                with plan_lock(lib, timeout=15.0, poll_seconds=0.001):
                    with guard:
                        state["now"] += 1
                        state["max"] = max(state["max"], state["now"])
                    time.sleep(0.01)
                    with guard:
                        state["now"] -= 1
        except Exception as exc:  # noqa: BLE001 - 线程内异常需带回主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, f"锁获取失败：{errors}"
    assert state["max"] == 1, "同时只能有一个持锁者"


def test_twenty_tasks_complete_with_four_workers(tmp_path):
    lib = tmp_path / "资产库"
    # 4 个工作者必须真正同时在跑：屏障放行条件就是 4 个并发请求
    executor = _FakeExecutor(barrier=threading.Barrier(4))
    summary = _scheduler(lib, executor).run_ready(_plan(*_independent_tasks(20)),
                                                  max_parallel=4)
    assert isinstance(summary, SchedulerSummary)
    assert executor.barrier_broken is False, "未出现 4 路并发，max_parallel 未生效"
    assert executor.max_inflight == 4
    assert len(executor.requests) == 20, "每个任务只允许一次生成请求"
    stored = _load(lib)
    assert len(stored.tasks) == 20
    for task in stored.tasks:
        assert task.status == "WAITING_REVIEW", f"{task.task_id} 未停在等待审核"
        assert len(task.attempts) == 1, f"{task.task_id} 轮次被重复追加"
        assert task.attempts[0].round == 1
    assert summary.generated == 20
    assert stored.revision == summary.plan.revision
    assert stored.revision >= 5, "每批（4 路并发共 5 批）至少各提交一次计划"


def test_resume_does_not_regenerate_after_stale_review_replay(tmp_path):
    """恢复时不得让「旧轮次判定」冲掉已生成的新轮次（否则重复消耗画面轮次）。"""
    lib = tmp_path / "资产库"
    rel1 = f"{attempt_dir(TASK_ID, 1)}/image.png"
    rel2 = f"{attempt_dir(TASK_ID, 2)}/image.png"
    for rel, payload in ((rel1, b"\x11"), (rel2, b"\x22")):
        target = lib / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG_BYTES + payload)
    task = _task(status="WAITING_REVIEW")
    task = replace(task, attempts=(
        AttemptRecord(round=1, image=rel1, image_sha256="d" * 64),
        AttemptRecord(round=2, image=rel2, image_sha256="e" * 64),
    ))
    schema_mod.write_visual_plan(_plan(task), _plan_path(lib))
    # 日志里留着第 1 轮的「需要返工」判定（进程在写计划之后、吸收该判定之前退出）
    append_events(lib, (
        VisualEvent.create(REVIEW_IMPORTED, TASK_ID,
                           payload={"round": 1, "decision": "NEEDS_REVISION"}),
    ))
    executor = _FakeExecutor()
    summary = _scheduler(lib, executor).resume()
    stored = _load(lib).by_id(TASK_ID)
    assert stored.status == "WAITING_REVIEW", "旧判定不得把新轮次打回「需要修改」"
    assert len(stored.attempts) == 2
    assert executor.requests == [], "恢复不得因此重新生成"
    assert summary.recovered == 0, "陈旧判定不得改变任何任务状态"


def test_parallelism_is_bounded_by_max_parallel(tmp_path):
    lib = tmp_path / "资产库"
    executor = _FakeExecutor(barrier=threading.Barrier(2))
    _scheduler(lib, executor).run_ready(_plan(*_independent_tasks(6)), max_parallel=2)
    assert executor.barrier_broken is False
    assert executor.max_inflight == 2, "并发不得超过 max_parallel"


def test_only_coordinator_thread_writes_plan(tmp_path, monkeypatch):
    lib = tmp_path / "资产库"
    writers = []
    real_write = schema_mod.write_visual_plan

    def spy(plan, path):
        writers.append(threading.current_thread().name)
        return real_write(plan, path)

    monkeypatch.setattr(schema_mod, "write_visual_plan", spy)
    executor = _FakeExecutor(barrier=threading.Barrier(4))
    _scheduler(lib, executor).run_ready(_plan(*_independent_tasks(20)), max_parallel=4)
    assert writers, "协调器必须写计划"
    assert set(writers) == {threading.current_thread().name}, (
        f"计划只能由协调器主线程写，实际写入线程：{sorted(set(writers))}")


# --- 崩溃恢复 -------------------------------------------------------------------

def test_crash_between_image_and_plan_write_recovers_without_regeneration(
        tmp_path, monkeypatch):
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    real_write = schema_mod.write_visual_plan

    def flaky(plan, path):
        task = plan.by_id(TASK_ID)
        if task is not None and task.attempts:
            raise RuntimeError("模拟「图片已保存、计划未写入」时进程中断")
        return real_write(plan, path)

    monkeypatch.setattr(schema_mod, "write_visual_plan", flaky)
    with pytest.raises(RuntimeError):
        _scheduler(lib, executor).run_ready(_plan(_task()), max_parallel=1)

    image = lib / "视觉资产" / "任务" / TASK_ID / "attempt-01" / "image.png"
    assert image.is_file(), "崩溃前图片已落盘"
    kinds = [event.kind for event in read_events(lib)]
    assert IMAGE_STORED in kinds, "事件必须先于计划落盘（写前日志）"
    assert _load(lib).by_id(TASK_ID).status == "PLANNED", "崩溃时计划尚未写入该轮次"

    monkeypatch.setattr(schema_mod, "write_visual_plan", real_write)
    resume_scheduler = _scheduler(lib, executor)
    summary = resume_scheduler.resume()
    assert len(executor.requests) == 1, "恢复不得重复请求已生成的图片"
    assert _load(lib).by_id(TASK_ID).status == "WAITING_REVIEW"
    assert summary.recovered >= 1


# --- 依赖与失败隔离 -------------------------------------------------------------

def test_upstream_failure_blocks_only_downstream(tmp_path):
    lib = tmp_path / "资产库"
    upstream = _task(task_id="CH_甲-正脸-v01", asset_id="CH_甲")
    downstream = _task(task_id="CH_乙-正脸-v01", asset_id="CH_乙",
                       depends_on=("CH_甲-正脸-v01",))
    unrelated = _task(task_id="CH_丙-正脸-v01", asset_id="CH_丙")
    executor = _FakeExecutor(failures={"CH_甲-正脸-v01": GENERATION_REJECTED})
    summary = _scheduler(lib, executor).run_ready(
        _plan(upstream, downstream, unrelated), max_parallel=3)

    stored = _load(lib)
    assert stored.by_id("CH_甲-正脸-v01").status == "BLOCKED"
    assert stored.by_id("CH_乙-正脸-v01").status == "PLANNED", "下游保持在未生成"
    assert stored.by_id("CH_丙-正脸-v01").status == "WAITING_REVIEW", "无关任务必须继续"
    assert "CH_乙-正脸-v01" not in [r.task_id for r in executor.requests]
    assert summary.blocked == 1
    assert summary.waiting_upstream == 1, "下游停在「等上游」必须被计数上报"
    assert any(event.kind == TASK_BLOCKED and event.task_id == "CH_甲-正脸-v01"
               for event in read_events(lib))


def test_compile_failure_blocks_task_with_reason_code(tmp_path):
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    _scheduler(lib, executor, factory=_fail_compile).run_ready(_plan(_task()), max_parallel=1)
    assert executor.requests == [], "编译失败不得触网"
    assert _load(lib).by_id(TASK_ID).status == "BLOCKED"


def test_transport_failure_does_not_consume_round(tmp_path):
    lib = tmp_path / "资产库"
    failing = _FakeExecutor(failures={TASK_ID: RATE_LIMITED})
    summary = _scheduler(lib, failing).run_ready(_plan(_task()), max_parallel=1)
    task = _load(lib).by_id(TASK_ID)
    assert task.status == "PLANNED", "传输失败后任务仍可重试"
    assert task.attempts == (), "传输失败不得消耗画面轮次"
    assert summary.transport_failed == 1
    assert [e.kind for e in read_events(lib)].count(TRANSPORT_FAILED) == 1

    healthy = _FakeExecutor()
    _scheduler(lib, healthy).run_ready(_load(lib), max_parallel=1)
    retried = _load(lib).by_id(TASK_ID)
    assert retried.status == "WAITING_REVIEW"
    assert len(retried.attempts) == 1 and retried.attempts[0].round == 1


# --- 复用与审核往返 -------------------------------------------------------------

def test_published_task_with_unchanged_inputs_is_reused(tmp_path):
    from conftest import publish_task_with_review

    lib = tmp_path / "资产库"
    schema_mod.write_visual_plan(_plan(_task()), _plan_path(lib))
    publish_task_with_review(lib, TASK_ID)
    executor = _FakeExecutor()
    summary = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    assert executor.requests == [], "输入未变的已通过任务不得重新生成"
    assert summary.reused == 1
    assert _load(lib).by_id(TASK_ID).status == "READY_AS_REFERENCE"
    assert current_release(lib, TASK_ID) is not None


def test_dependency_waits_for_upstream_publication(tmp_path):
    lib = tmp_path / "资产库"
    upstream = _task(task_id="CH_甲-正脸-v01", asset_id="CH_甲")
    downstream = _task(task_id="CH_乙-正脸-v01", asset_id="CH_乙",
                       depends_on=("CH_甲-正脸-v01",))
    executor = _FakeExecutor()
    _scheduler(lib, executor).run_ready(_plan(upstream, downstream), max_parallel=2)
    after_first = _load(lib)
    assert after_first.by_id("CH_乙-正脸-v01").status == "PLANNED", "上游未正式通过前不得生成下游"
    assert after_first.by_id("CH_乙-正脸-v01").attempts == ()

    _approve(lib, "CH_甲-正脸-v01")
    _scheduler(lib, executor).run_ready(_load(lib), max_parallel=2)
    after_second = _load(lib)
    assert after_second.by_id("CH_甲-正脸-v01").status == "READY_AS_REFERENCE"
    assert current_release(lib, "CH_甲-正脸-v01") is not None
    assert after_second.by_id("CH_乙-正脸-v01").status == "WAITING_REVIEW"
    assert len(after_second.by_id("CH_乙-正脸-v01").attempts) == 1


def test_publish_failure_goes_to_human_decision_and_is_reported(tmp_path, monkeypatch):
    """已通过却发不出去：必须落到「需要人工决定」并被计数上报，不能被静默丢弃。"""
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    _scheduler(lib, executor).run_ready(_plan(_task()), max_parallel=1)
    _approve(lib, TASK_ID)

    def boom(*args, **kwargs):
        raise PublishError("PUBLISH_MIME_UNSUPPORTED", "注入发布失败")

    monkeypatch.setattr(scheduler_mod, "publish_task", boom)
    summary = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    assert _load(lib).by_id(TASK_ID).status == "NEEDS_HUMAN_DECISION"
    assert any("PUBLISH_MIME_UNSUPPORTED" in issue for issue in summary.review_issues)


def test_unreadable_review_result_is_reported_not_silently_waited(tmp_path):
    """结果与当前输入不符：停在等待审核，但必须把稳定原因码上报。"""
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    _scheduler(lib, executor).run_ready(_plan(_task()), max_parallel=1)
    _approve(lib, TASK_ID)
    payload = json.loads(result_path(lib, TASK_ID).read_text(encoding="utf-8"))
    payload["binding"] = {**payload["binding"], "image_sha256": "0" * 64}
    result_path(lib, TASK_ID).write_text(json.dumps(payload, ensure_ascii=False),
                                         encoding="utf-8", newline="\n")
    summary = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    assert _load(lib).by_id(TASK_ID).status == "WAITING_REVIEW"
    assert summary.review_issues, "不合格结果必须被上报，不能静默等待"
    assert summary.review_issues[0].startswith(f"{TASK_ID}: ")
    assert current_release(lib, TASK_ID) is None


def test_unexpected_compile_error_propagates_loudly(tmp_path):
    """编译期的非预期异常必须炸出来，不得被压成 BLOCKED 后永久静默。"""
    def boom(task, plan):
        raise RuntimeError("第三方库异常")

    lib = tmp_path / "资产库"
    with pytest.raises(RuntimeError):
        _scheduler(lib, _FakeExecutor(), factory=boom).run_ready(_plan(_task()),
                                                                 max_parallel=1)


def test_review_round_trip_publishes_after_import(tmp_path):
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    first = _scheduler(lib, executor).run_ready(_plan(_task()), max_parallel=1)
    assert first.published == 0, "没有主模型审核结果时不得发布"
    assert _load(lib).by_id(TASK_ID).status == "WAITING_REVIEW"

    _approve(lib, TASK_ID)
    second = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    task = _load(lib).by_id(TASK_ID)
    assert task.status == "READY_AS_REFERENCE"
    assert task.current_version, "发布后必须记录当前正式版本"
    assert current_release(lib, TASK_ID) is not None
    assert second.published == 1
    assert len(executor.requests) == 1, "审核回写不得触发重新生成"


def test_pause_check_stops_further_dispatch_mid_run(tmp_path):
    """运行中暂停：不再派发新批次，已落盘结果与计划修订保留。"""
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] > 1  # 第一批派发后暂停

    scheduler = VisualScheduler(lib, executor=executor, request_factory=_request_factory,
                                pause_check=check)
    summary = scheduler.run_ready(_plan(*_independent_tasks(8)), max_parallel=2)
    assert summary.paused is True
    assert len(executor.requests) == 2, "暂停前只跑完第一批"
    stored = _load(lib)
    assert sum(1 for t in stored.tasks if t.status == "WAITING_REVIEW") == 2
    assert stored.revision == summary.plan.revision

    cleared = VisualScheduler(lib, executor=executor, request_factory=_request_factory,
                              pause_check=lambda: False)
    resumed = cleared.run_ready(_load(lib), max_parallel=2)
    assert resumed.paused is False
    assert len([t for t in _load(lib).tasks if t.status == "WAITING_REVIEW"]) == 8


def test_same_issues_twice_escalates_to_human_decision(tmp_path):
    """相同问题连续两轮无改善：不再消耗第三轮，直接转「需要人工决定」（规格十）。"""
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    issue = {"code": "IDENTITY_DRIFT", "severity": "BLOCKING", "evidence": "五官漂移",
             "repair_instruction": "以正脸为准重注入"}
    _scheduler(lib, executor).run_ready(_plan(_task()), max_parallel=1)
    _approve(lib, TASK_ID, decision="NEEDS_REVISION", issues=(issue,))
    first = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    after_first = _load(lib).by_id(TASK_ID)
    assert after_first.status == "WAITING_REVIEW", "第一轮返工判定应触发第 2 轮生成"
    assert len(after_first.attempts) == 2 and first.escalated == 0

    _approve(lib, TASK_ID, decision="NEEDS_REVISION", issues=(issue,))
    second = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    final = _load(lib).by_id(TASK_ID)
    assert final.status == "NEEDS_HUMAN_DECISION"
    assert len(final.attempts) == 2, "不得再消耗第三轮"
    assert second.escalated == 1
    escalated = [e for e in read_events(lib)
                 if e.kind == "REVIEW_IMPORTED"
                 and e.payload.get("reason") == "REVIEW_NO_IMPROVEMENT"]
    assert escalated, "升级原因必须落盘可追溯"


def test_improved_issues_do_not_escalate(tmp_path):
    """问题码变化（有改善）时继续正常返工，不得误升级。"""
    lib = tmp_path / "资产库"
    executor = _FakeExecutor()
    first_issue = {"code": "IDENTITY_DRIFT", "severity": "BLOCKING", "evidence": "五官漂移"}
    second_issue = {"code": "HAND_FINGER_DEFECT", "severity": "BLOCKING", "evidence": "手指粘连"}
    _scheduler(lib, executor).run_ready(_plan(_task()), max_parallel=1)
    _approve(lib, TASK_ID, decision="NEEDS_REVISION", issues=(first_issue,))
    _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    _approve(lib, TASK_ID, decision="NEEDS_REVISION", issues=(second_issue,))
    summary = _scheduler(lib, executor).run_ready(_load(lib), max_parallel=1)
    stored = _load(lib).by_id(TASK_ID)
    assert summary.escalated == 0
    assert stored.status == "WAITING_REVIEW"
    assert len(stored.attempts) == 3, "问题变了就该继续返工（第三轮）"
