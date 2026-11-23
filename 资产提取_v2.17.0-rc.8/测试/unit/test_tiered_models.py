import json
from pathlib import Path


def test_roles_split_nodes(tmp_path, fake_endpoint):
    cfg = tmp_path / "llm_nodes.json"
    cfg.write_text(json.dumps({"nodes": [
        {"name": "fast", "proto": "openai", "base_url": fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
         "model": "m-fast", "api_key": "k", "roles": ["mechanical"]},
        {"name": "strong", "proto": "openai", "base_url": fake_endpoint.url.rsplit("/v1/", 1)[0] + "/v1",
         "model": "m-strong", "api_key": "k", "roles": ["judgement"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    fake_endpoint.script([{"text": "机械活"}, {"text": "判断活"}])
    from engine import llm_pool
    pool = llm_pool.from_env(lib=tmp_path, search=[tmp_path])
    pool.call("s", "u", step="合规修", role="mechanical")
    pool.call("s", "u", step="聚合裁决", role="judgement")
    models = [r.get("model") for r in fake_endpoint.received()]
    assert models == ["m-fast", "m-strong"]


def test_ledger_groups_by_role(tmp_path):
    from engine import perf_ledger
    perf_ledger.append(tmp_path, {"step": "x", "role": "mechanical", "model": "m", "node": "A",
                                  "elapsed_ms": 1, "prompt_tokens": 1, "completion_tokens": 1,
                                  "retries": 0, "error": ""})
    summary = perf_ledger.summarize(tmp_path)
    assert summary["x"]["by_role"]["mechanical"]["calls"] == 1


def test_defaults_keep_old_behavior(tmp_path, fake_endpoint, monkeypatch):
    """没有 roles 配置时，全部调用仍走原节点（行为不变）。"""
    monkeypatch.setenv("SUMMARY_API_URL", fake_endpoint.url)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("SUMMARY_API_MODEL", "m-old")
    fake_endpoint.script([{"text": "a"}, {"text": "b"}])
    from engine import llm_pool
    pool = llm_pool.from_env(lib=None)
    pool.call("s", "u", role="mechanical")
    pool.call("s", "u", role="judgement")
    models = [r.get("model") for r in fake_endpoint.received()]
    assert models == ["m-old", "m-old"]


def test_no_production_callsite_uses_mechanical_role():
    """防误用闸：生产代码里不得出现 mechanical 档调用点。

    **本测试的覆盖边界（正版千问对抗评审指出，已采纳）**：它只能证明"字面量
    `role="mechanical"` 会被拦住"，**不能**证明全部调用点在语义上都是判断活，
    也**不能**证明没有间接传入 `mechanical` 的调用点（例如经变量或常量传入）。

    下面这份分类是**人工逐个审读的结论**，不是测试证明的：
      段级提取×2   从原文识别资产 —— 判断
      聚合裁决     决定哪些资产成立、如何处理冲突 —— 判断
      全局登记     从原文提出候选主名 —— 判断
      对账并入     模板明文要求「按语义判定同实异名」 —— 判断
      生成/衍生×2  由资产数据写视觉提示词 —— 生成（不是机械抄写）

    真正的机械活（顿号 / 三视图 / 负面锁修复）由 `engine/autofix.py` 用**纯 Python**
    完成、0 次模型调用。因此把判断活降档到快模型只会降质量——复核报告里
    「给机械活调用点标 mechanical」那条建议**不成立**。
    将来若确实新增 mechanical 调用点，须先在此处补上"输入已完全确定、输出只是照抄/格式化"的理由。
    """
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "scripts").rglob("*.py"))
    # 防空转：扫描面为空时本测试会"假绿"，先把这件事挡掉
    assert len(files) >= 10, f"扫描面异常（只找到 {len(files)} 个 .py），测试将失去意义"
    hits = []
    for py in files:
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if 'role="mechanical"' in line or "role='mechanical'" in line:
                hits.append(f"{py.relative_to(root)}:{i}")
    assert hits == [], f"生产代码出现 mechanical 调用点，请先补降档理由：{hits}"


def test_perf_report_flags_idle_mechanical_node(tmp_path, monkeypatch, capsys):
    """配了 mechanical 节点却零调用时要挑明，免得以为分档已经生效。"""
    from engine import perf_ledger
    from maintenance import do_perf_report
    perf_ledger.append(tmp_path, {"step": "聚合裁决", "role": "judgement", "model": "m", "node": "A",
                                  "elapsed_ms": 1, "prompt_tokens": 1, "completion_tokens": 1,
                                  "retries": 0, "error": ""})
    monkeypatch.setenv("ASSET_ENV_PATH", str(tmp_path / "接管.env"))
    (tmp_path / "llm_nodes.json").write_text(json.dumps({"nodes": [
        {"name": "fast", "proto": "openai", "base_url": "http://127.0.0.1:1/v1",
         "model": "m-fast", "api_key": "k", "roles": ["mechanical"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    assert do_perf_report(tmp_path, as_json=False) == 0
    out = capsys.readouterr().out
    assert "mechanical" in out and "零调用" in out, out
