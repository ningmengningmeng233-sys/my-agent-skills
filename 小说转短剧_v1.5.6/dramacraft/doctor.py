import sys
import os
import shutil
import subprocess
import json
from typing import Tuple, Optional


def check_python_version() -> Tuple[bool, str]:
    """检查 Python 运行环境版本（要求 >= 3.8）"""
    v = sys.version_info
    ver_str = f"{v.major}.{v.minor}.{v.micro}"
    if v.major >= 3 and v.minor >= 8:
        return True, f"Python {ver_str} 运行环境正常"
    return False, f"Python 版本过低 ({ver_str})，DramaCraft 要求 Python 3.8+"


def check_node_available() -> Tuple[bool, str]:
    """检查 Node.js 运行时（12 道剧本质量门依赖 Node.js）"""
    node_bin = shutil.which("node")
    if not node_bin:
        return False, "未检测到 Node.js 运行时。DramaCraft 的 12 道剧本质量门依赖 Node.js 执行。请前往 https://nodejs.org 下载安装 Node.js（LTS 版本即可）。"
    try:
        res = subprocess.run([node_bin, "-v"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            return True, f"Node.js {res.stdout.strip()} 运行时正常"
        return False, f"Node.js 存在但在执行 node -v 时报错: {res.stderr.strip()}"
    except Exception as e:
        return False, f"检测 Node.js 时发生异常: {str(e)}"


def check_validator_script() -> Tuple[bool, str]:
    """检查剧本质量门脚本 validate.mjs 是否就绪"""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    script_path = os.path.join(base_dir, "skills_runtime", "dramacraft_patch", "validate.mjs")
    if os.path.exists(script_path):
        return True, f"质量门脚本存在 ({script_path})"
    return False, f"未找到质量门脚本，期望路径: {script_path}"


def check_llm_nodes(nodes_path: Optional[str] = None, probe_api: bool = False) -> Tuple[bool, str]:
    """检查 LLM 模型节点池配置及三级发现链"""
    candidates = []
    if nodes_path:
        candidates.append(nodes_path)
    env_path = os.environ.get("DRAMACRAFT_NODES")
    if env_path:
        candidates.append(env_path)
    skill_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates.append(os.path.join(skill_root, "llm_nodes.json"))
    candidates.append(os.path.join(
        os.path.expanduser("~"), ".agents", "skills",
        "bilibili-auto-transcript", "llm_nodes.json"))

    found_path = None
    for cand in candidates:
        if os.path.exists(cand):
            found_path = cand
            break

    if not found_path:
        return False, (
            "未找到可用的 llm_nodes.json 模型节点配置！\n"
            "   排查建议：\n"
            "   1. 可直接将 llm_nodes.example.json 复制一份并重命名为 llm_nodes.json，填入您的 API Key；\n"
            "   2. 若已使用过 B 站转录技能，会自动共享其已配置的模型池；\n"
            "   3. 或设置环境变量 DRAMACRAFT_NODES 指向您的配置文件。"
        )

    try:
        with open(found_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception as e:
        return False, f"模型节点配置文件格式损坏 ({found_path}): {str(e)}"

    nodes = data.get("nodes", [])
    if not nodes:
        return False, f"配置文件中 nodes 列表为空 ({found_path})"

    # 检查占位符
    valid_count = 0
    has_placeholder = False
    for n in nodes:
        k = n.get("api_key", "")
        if "REPLACE_ME" in k or not k:
            has_placeholder = True
        else:
            valid_count += 1

    summary = f"已找到节点配置 ({found_path})，共识别出 {len(nodes)} 个节点（{valid_count} 个有效密钥"
    if has_placeholder:
        summary += "，检测到包含未填写的 REPLACE_ME 占位符"
    summary += "）"

    if valid_count == 0:
        return False, f"{summary}。所有节点的 API Key 均为空或未填写，请填入真实密钥后再跑批。"

    if probe_api:
        # 网络请求探测
        import requests
        first_node = next((n for n in nodes if n.get("api_key") and "REPLACE_ME" not in n.get("api_key")), None)
        if first_node:
            base_url = first_node.get("base_url", "").rstrip("/")
            url = f"{base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {first_node.get('api_key')}",
                "Content-Type": "application/json"
            }
            body = {
                "model": first_node.get("model"),
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5
            }
            try:
                resp = requests.post(url, headers=headers, json=body, timeout=10)
                if resp.status_code == 200:
                    summary += f"\n   ✓ API 连通性探测成功: 首节点 [{first_node.get('name')}] 响应正常"
                else:
                    return False, f"{summary}\n   ❌ API 探测失败: 状态码 {resp.status_code}，响应: {resp.text[:200]}"
            except Exception as e:
                return False, f"{summary}\n   ❌ API 网络连通失败: {str(e)}"

    return True, summary


def run_doctor(nodes_path: Optional[str] = None, probe_api: bool = False) -> Tuple[bool, str]:
    """执行 DramaCraft 全套分发环境健康体检"""
    lines = []
    lines.append("================================================================")
    lines.append("🩺 DramaCraft 环境与依赖健康体检 (Doctor)")
    lines.append("================================================================")

    all_passed = True

    # 1. Python
    py_ok, py_msg = check_python_version()
    lines.append(f"[{'✓' if py_ok else '❌'}] [1/4] Python 运行环境: {py_msg}")
    if not py_ok:
        all_passed = False

    # 2. Node.js
    node_ok, node_msg = check_node_available()
    lines.append(f"[{'✓' if node_ok else '❌'}] [2/4] Node.js 运行时: {node_msg}")
    if not node_ok:
        all_passed = False

    # 3. 质量门
    val_ok, val_msg = check_validator_script()
    lines.append(f"[{'✓' if val_ok else '❌'}] [3/4] 剧本质量门脚本: {val_msg}")
    if not val_ok:
        all_passed = False

    # 4. LLM 节点
    llm_ok, llm_msg = check_llm_nodes(nodes_path=nodes_path, probe_api=probe_api)
    lines.append(f"[{'✓' if llm_ok else '❌'}] [4/4] LLM 模型节点池: {llm_msg}")
    if not llm_ok:
        all_passed = False

    lines.append("================================================================")
    if all_passed:
        lines.append("🎉 体检通过！DramaCraft 核心运行环境一切就绪，可以放心跑批。")
    else:
        lines.append("⚠️ 体检未通过！请根据上方带有 [❌] 的项目提示进行排查和安装。")
    lines.append("================================================================")

    return all_passed, "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DramaCraft 环境与依赖健康体检")
    parser.add_argument("--nodes", help="指定模型节点配置文件路径")
    parser.add_argument("--probe-api", action="store_true", help="发送真实极简网络请求探测模型 API 连通性")
    args = parser.parse_args()

    passed, report = run_doctor(nodes_path=args.nodes, probe_api=args.probe_api)
    print(report)
    sys.exit(0 if passed else 1)
