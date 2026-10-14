import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import requests


@dataclass
class LLMNode:
    name: str
    proto: str
    base_url: str
    model: str
    api_key: str
    max_tokens: int = 4096
    timeout: int = 60


def _default_nodes_candidates() -> List[str]:
    """节点池三级发现链：显式环境变量 -> 技能根自带池 -> 用户家目录的 b 站转录池。
    分发到任何机器都成立，不绑定某台机器的绝对路径。"""
    candidates = []
    env_path = os.environ.get("DRAMACRAFT_NODES")
    if env_path:
        candidates.append(env_path)
    skill_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates.append(os.path.join(skill_root, "llm_nodes.json"))
    candidates.append(os.path.join(
        os.path.expanduser("~"), ".agents", "skills",
        "bilibili-auto-transcript", "llm_nodes.json"))
    return candidates


class LLMReply(str):
    """携带命中节点名的回复字符串。

    行为与 str 完全一致（可比较、可 json 解析），额外用 ``.node`` 记录本次
    调用实际命中的节点名，供并发调用方直接取用，不再依赖跨线程读取网关状态。
    """

    def __new__(cls, text: str, node: Optional[str] = None):
        obj = super().__new__(cls, text)
        obj.node = node
        return obj


class BudgetExhausted(RuntimeError):
    """预算熔断：LLM 调用次数触顶。可续跑的中止，不是数据错误——run_batch 捕获后 exit 3"""


class LLMGateway:
    def __init__(self, config_path: Optional[str] = None, config: Optional[Dict[str, Any]] = None,
                 max_calls: Optional[int] = None, ledger: Optional[Any] = None):
        self.nodes: List[LLMNode] = []
        self.max_calls = max_calls
        self.call_count = 0
        self.ledger = ledger
        self._tls = threading.local()
        self.current_ep: Optional[int] = None
        self.is_exhausted = False
        self._lock = threading.Lock()
        if config is not None:
            self._load_from_dict(config)
        elif config_path and os.path.exists(config_path):
            self._load_from_file(config_path)
        else:
            for candidate in _default_nodes_candidates():
                if os.path.exists(candidate):
                    self._load_from_file(candidate)
                    break

    @property
    def last_node(self) -> Optional[str]:
        """本线程最近一次调用命中的节点名（线程局部隔离，未调用过为 None）。"""
        return getattr(self._tls, "last_node", None)

    @last_node.setter
    def last_node(self, value: Optional[str]) -> None:
        self._tls.last_node = value

    def _load_from_dict(self, data: Dict[str, Any]):
        for item in data.get("nodes", []):
            self.nodes.append(
                LLMNode(
                    name=item.get("name", "Unnamed"),
                    proto=item.get("proto", "openai").lower(),
                    base_url=item.get("base_url", "").rstrip("/"),
                    model=item.get("model", ""),
                    api_key=item.get("api_key", ""),
                    max_tokens=item.get("max_tokens", 4096),
                    timeout=item.get("timeout", 60),
                )
            )

    def _load_from_file(self, file_path: str):
        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            self._load_from_dict(data)

    def _looks_truncated(self, content: str, finish_reason: str) -> bool:
        text = str(content or "").strip()
        if not text:
            return True
        if finish_reason and finish_reason not in ("stop", "end_turn"):
            return True
        return False

    def _charge(self):
        with self._lock:
            if self.max_calls is not None and self.call_count >= self.max_calls:
                self.is_exhausted = True
                raise BudgetExhausted(f"LLM 调用次数已达到设定的上限 ({self.max_calls} 次)，触发预算熔断保护。")
            self.call_count += 1

    def _call_node(self, node: LLMNode, prompt: str, system_prompt: str = "") -> str:
        """调用单个节点，支持同节点截断重试一次"""
        for attempt in range(1, 3):
            self._charge()
            if node.proto == "anthropic":
                url = f"{node.base_url}/v1/messages" if not node.base_url.endswith("/messages") else node.base_url
                headers = {
                    "Content-Type": "application/json",
                    "x-api-key": node.api_key,
                    "anthropic-version": "2023-06-01",
                }
                payload = {
                    "model": node.model,
                    "max_tokens": node.max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if system_prompt:
                    payload["system"] = system_prompt

                resp = requests.post(url, json=payload, headers=headers, timeout=node.timeout)
                resp.raise_for_status()
                data = resp.json()
                content_list = data.get("content", [])
                text = "".join([c.get("text", "") for c in content_list if c.get("type") == "text"])
                finish = data.get("stop_reason", "")
                if not self._looks_truncated(text, finish) or attempt == 2:
                    return text

            else:
                # 默认 OpenAI 兼容协议
                url = f"{node.base_url}/chat/completions" if not node.base_url.endswith("/completions") else node.base_url
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {node.api_key}",
                }
                messages = []
                if system_prompt:
                    messages.append({"role": "system", "content": system_prompt})
                messages.append({"role": "user", "content": prompt})

                payload = {
                    "model": node.model,
                    "messages": messages,
                    "max_tokens": node.max_tokens,
                }

                resp = requests.post(url, json=payload, headers=headers, timeout=node.timeout)
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices", [])
                if not choices or "message" not in choices[0]:
                    raise ValueError(f"API 返回格式异常: {data}")
                text = str(choices[0].get("message", {}).get("content") or "")
                finish = str(choices[0].get("finish_reason") or "")
                if not self._looks_truncated(text, finish) or attempt == 2:
                    return text

            time.sleep(0.5)

        raise RuntimeError(f"节点 {node.name} 返回疑似截断内容且重试失败")

    def call_llm(self, prompt: str, system_prompt: str = "", ep: Optional[int] = None) -> str:
        """支持全节点自动轮换故障转移的高可靠调用入口"""
        if not self.nodes:
            raise RuntimeError("没有可用的 LLM 节点配置")
        if self.is_exhausted:
            raise BudgetExhausted("预算保险丝已熔断 (is_exhausted=True)，中止后续模型调用。")

        target_ep = ep if ep is not None else self.current_ep
        errors = []
        for node in self.nodes:
            try:
                res = self._call_node(node, prompt, system_prompt)
                self.last_node = node.name
                if self.ledger:
                    self.ledger.emit("llm_call", node=node.name, ok=True, ep=target_ep)
                return LLMReply(res, node=node.name)
            except BudgetExhausted:
                raise
            except Exception as e:
                if self.ledger:
                    self.ledger.emit("llm_call", node=node.name, ok=False, error=str(e)[:200], ep=target_ep)
                errors.append(f"[{node.name}] {str(e)}")
                continue

        raise RuntimeError("所有 LLM 节点调用均失败:\n" + "\n".join(errors))
