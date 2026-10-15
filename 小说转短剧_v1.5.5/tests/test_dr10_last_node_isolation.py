# -*- coding: utf-8 -*-
"""DR-10：网关 last_node 并发隔离回归。

故障：LLMGateway.last_node 是实例共享可变属性，多线程并发调用时后完成的
线程覆盖先完成线程的执笔节点记录，self_healing 据此记账会张冠李戴。
修复：last_node 改用 threading.local 按线程隔离；call_llm 返回的回复对象
携带本次命中的节点名，调用方优先使用返回值自带的节点标识。
"""
import threading

from dramacraft.gateway import LLMGateway

_NODES = {
    "nodes": [
        {"name": "NodeA", "proto": "openai", "base_url": "https://a.test", "model": "m", "api_key": "k"},
        {"name": "NodeB", "proto": "openai", "base_url": "https://b.test", "model": "m", "api_key": "k"},
    ]
}


def _install_router(gw, monkeypatch):
    """按 prompt 路由到不同节点：B 走 NodeA（快），A 在 NodeA 上失败后走 NodeB。"""

    def fake_call_node(node, prompt, system_prompt=""):
        if prompt == "A" and node.name == "NodeA":
            raise RuntimeError("A skips NodeA")
        return "reply-from-%s" % node.name

    monkeypatch.setattr(gw, "_call_node", fake_call_node)


def test_call_llm_reply_carries_hit_node(monkeypatch):
    gw = LLMGateway(config=_NODES)
    _install_router(gw, monkeypatch)
    reply_b = gw.call_llm(prompt="B")
    assert reply_b == "reply-from-NodeA"
    assert getattr(reply_b, "node", None) == "NodeA"
    assert gw.last_node == "NodeA"
    reply_a = gw.call_llm(prompt="A")
    assert getattr(reply_a, "node", None) == "NodeB"
    assert gw.last_node == "NodeB"


def test_last_node_isolated_across_threads(monkeypatch):
    gw = LLMGateway(config=_NODES)
    _install_router(gw, monkeypatch)

    b_called = threading.Event()
    a_called = threading.Event()
    results = {}
    errors = {}

    def worker_b():
        try:
            reply = gw.call_llm(prompt="B")
            results["B_reply_node"] = getattr(reply, "node", None)
            b_called.set()
            assert a_called.wait(timeout=30), "worker A 未按时完成"
            results["B_last_node"] = gw.last_node
        except Exception as exc:  # pragma: no cover - 由主线程断言转交
            errors["B"] = exc

    def worker_a():
        try:
            assert b_called.wait(timeout=30), "worker B 未按时完成"
            reply = gw.call_llm(prompt="A")
            results["A_reply_node"] = getattr(reply, "node", None)
            results["A_last_node"] = gw.last_node
            a_called.set()
        except Exception as exc:  # pragma: no cover - 由主线程断言转交
            errors["A"] = exc

    # B 先调用（命中 NodeA），A 后调用（命中 NodeB）；B 在 A 写完之后再读。
    # 未隔离时 B 读到的是 A 覆盖后的 NodeB（串台）；隔离后 B 仍读到自己的 NodeA。
    th_b = threading.Thread(target=worker_b, daemon=True)
    th_a = threading.Thread(target=worker_a, daemon=True)
    th_b.start()
    th_a.start()
    th_b.join(timeout=60)
    th_a.join(timeout=60)
    assert not th_b.is_alive() and not th_a.is_alive(), "工作线程未收敛"
    assert not errors, "工作线程异常: %r" % (errors,)

    assert results["B_reply_node"] == "NodeA", "B 的返回值应携带 NodeA"
    assert results["A_reply_node"] == "NodeB", "A 的返回值应携带 NodeB"
    assert results["A_last_node"] == "NodeB", "A 线程应读到自己的 NodeB"
    assert results["B_last_node"] == "NodeA", "B 线程被 A 覆盖，读到 %r" % results.get("B_last_node")
