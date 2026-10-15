# -*- coding: utf-8 -*-
"""环境预检与健康检查工具 (check_env.py)

功能：
1. 检查 Python 环境与关键依赖（requests, dotenv, pdfplumber, docx 等）
2. 探测本地 Ollama 服务连通性与模型可用性（qwen3-embedding, bge-m3 等）
3. 检查知识库目录与 .env 配置状态
4. 提供模型自动拉取 (auto-pull) 建议或静默触发

使用方式：
    python check_env.py [--lib <知识库路径>] [--pull]
"""

import os
import sys
import json
import subprocess
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    requests = None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import config as v5config

# 默认值统一来自 config.py，支持 OLLAMA_HOST / INDEX_EMBEDDING_MODEL 环境变量
DEFAULT_OLLAMA_BASE_URL = v5config.DEFAULT_OLLAMA_BASE_URL
DEFAULT_TARGET_MODELS = ["qwen3-embedding:8b", "qwen3-embedding:0.6b", "bge-m3", "nomic-embed-text"]


def check_python_dependencies():
    """检查必备与可选 Python 依赖"""
    deps = {
        "requests": "HTTP 请求支持（API / Ollama 通信）",
        "dotenv": "环境变量 (.env) 加载支持",
        "docx": "Word 文档 (.docx) 文本解析支持",
        "pdfplumber": "PDF 文档 (.pdf) 精准文本解析支持",
        "numpy": "本地向量计算与余弦相似度支持",
        "epub": "EPUB 容器与 XHTML 顺序解析支持（Python 标准库）",
    }
    results = {}
    for mod, desc in deps.items():
        try:
            if mod == "dotenv":
                import dotenv  # noqa: F401
            elif mod == "docx":
                import docx  # noqa: F401
            elif mod == "pdfplumber":
                import pdfplumber  # noqa: F401
            elif mod == "numpy":
                import numpy  # noqa: F401
            elif mod == "epub":
                import zipfile  # noqa: F401
            else:
                __import__(mod)
            results[mod] = {"installed": True, "desc": desc}
        except ImportError:
            results[mod] = {"installed": False, "desc": desc}
    return results


def check_ollama(base_url=None, target_models=None):
    """检查本地 Ollama 服务与模型健康状态。

    地址/模型默认来自 config.py（支持 OLLAMA_HOST、OLLAMA_MODELS、
    INDEX_EMBEDDING_MODEL 环境变量），并在输出中显示最终生效配置。
    附带一次真实 embedding 探测，确认可用维度。
    """
    cfg = v5config.get_config()
    if base_url is None:
        base_url = cfg.ollama_base_url
    if target_models is None:
        extra = [m.strip() for m in os.environ.get("OLLAMA_MODELS", "").split(",") if m.strip()]
        target_models = [cfg.embedding_model] + [m for m in extra if m != cfg.embedding_model] + \
            [m for m in DEFAULT_TARGET_MODELS if m != cfg.embedding_model]

    if requests is None:
        return {
            "online": False,
            "error": "缺少 requests 库，无法连接 Ollama",
            "models": [],
            "matched_model": None,
        }

    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            available = [m.get("name", "") for m in data.get("models", [])]
            matched = None
            for tm in target_models:
                # 精确匹配优先：含 tag 必须完整命中，不含 tag 才允许同名任意 tag
                hit = v5config.match_model(tm, available)
                if hit:
                    matched = hit
                    break

            probe = v5config.probe_ollama(base_url, matched or cfg.embedding_model)
            # 配置模型精确命中才算 matched，避免 :8b 配置在只有 :0.6b 时误报
            exact = v5config.match_model(cfg.embedding_model, available)
            return {
                "online": True,
                "base_url": base_url,
                "models": available,
                "matched_model": matched,
                "required_model": cfg.embedding_model,
                "required_model_found": exact is not None,
                "required_model_hit": exact,
                "effective_config": {
                    "ollama_base_url": cfg.ollama_base_url,
                    "embedding_model": cfg.embedding_model,
                    "embedding_dim": cfg.embedding_dim,
                    "chunk_strategy": cfg.chunk_strategy,
                    "index_version": cfg.index_version,
                },
                "probe_dimension": probe.get("dimension"),
                "probe_error": probe.get("error", ""),
            }
        else:
            return {
                "online": False,
                "error": f"Ollama 响应非 200 状态码: {resp.status_code}",
                "models": [],
                "matched_model": None,
            }
    except Exception as e:
        return {
            "online": False,
            "error": f"无法连接到 Ollama ({base_url}): {e}",
            "models": [],
            "matched_model": None,
        }


def pull_ollama_model(model_name=None):
    """尝试自动拉取推荐的向量模型"""
    model_name = model_name or v5config.get_config().embedding_model
    print(f"[*] 正在尝试拉取 Ollama 模型: {model_name} ...")
    try:
        cmd = ["ollama", "pull", model_name]
        ret = subprocess.run(cmd, capture_output=True, text=True)
        if ret.returncode == 0:
            print(f"[+] 成功拉取模型: {model_name}")
            return True
        else:
            print(f"[-] 模型拉取失败: {ret.stderr}")
            return False
    except Exception as e:
        print(f"[-] 无法执行 ollama 命令: {e}")
        return False


def run_diagnostics(lib_dir=None, auto_pull=False):
    """运行全套环境与系统体检"""
    report = {
        "python_version": sys.version.split()[0],
        "dependencies": check_python_dependencies(),
        "ollama": check_ollama(),
        "library_status": {},
    }

    # 检查知识库目录
    if lib_dir:
        lib_path = Path(lib_dir)
        report["library_status"] = {
            "path": str(lib_path.resolve()),
            "exists": lib_path.exists(),
            "has_raw": (lib_path / "原文").exists(),
            "has_segment_index": (lib_path / "段索引").exists(),
            "has_book_nav": (lib_path / "书导航").exists(),
            "has_semantic_index": (lib_path / ".semantic_index").exists(),
        }

    # 自动拉取处理
    if auto_pull and report["ollama"]["online"] and not report["ollama"]["matched_model"]:
        pulled = pull_ollama_model()
        if pulled:
            report["ollama"] = check_ollama()

    # 目录读写权限 + Web 端口占用
    try:
        from pathlib import Path as _P
        target = _P(lib_dir) if lib_dir else _P(v5config.get_config().lib_dir)
        report["writable"] = os.access(str(target), os.W_OK | os.X_OK) if target.exists() else False
    except Exception:
        report["writable"] = False
    try:
        import socket as _socket
        _s = _socket.socket()
        _s.settimeout(1)
        report["web_port_in_use"] = _s.connect_ex(("127.0.0.1", v5config.get_config().web_port)) == 0
        _s.close()
    except Exception:
        report["web_port_in_use"] = None

    return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description="知识库索引系统环境自愈与预检工具")
    parser.add_argument("--lib", default=None, help="知识库根目录")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式输出")
    parser.add_argument("--pull", action="store_true", help="若无可用嵌入模型则自动尝试 pull")
    args = parser.parse_args()

    diag = run_diagnostics(lib_dir=args.lib, auto_pull=args.pull)

    if args.json:
        oll = diag.get("ollama", {})
        blocked = (not oll.get("online")) or (not oll.get("required_model_found")) or (not oll.get("probe_dimension"))
        diag["blocked"] = bool(blocked)
        print(json.dumps(diag, ensure_ascii=False, indent=2))
        return 1 if blocked else 0

    print("=" * 60)
    print(" 知识库索引系统 v5.0 - 环境自愈与预检报告")
    print("=" * 60)
    print(f"Python 版本: {diag['python_version']}")
    print("-" * 60)
    print("[1] 关键依赖检查:")
    all_deps_ok = True
    for mod, info in diag["dependencies"].items():
        status = "✅ 已就绪" if info["installed"] else "❌ 未安装"
        if not info["installed"]:
            all_deps_ok = False
        print(f"  - {mod:<12}: {status} ({info['desc']})")

    print("-" * 60)
    print("[2] 本地 Ollama 向量服务检查:")
    ollama = diag["ollama"]
    env_blocked = False
    if ollama["online"]:
        print(f"  - 服务状态   : ✅ 正常运行 ({ollama.get('base_url')})")
        required = ollama.get("required_model") or "(未知)"
        if ollama.get("required_model_found"):
            print(f"  - 配置模型   : ✅ 已就绪 ({ollama.get('required_model_hit')})")
        else:
            env_blocked = True
            print(f"  - 配置模型   : ❌ 未命中 (要求 {required}，已装: {', '.join(ollama.get('models', [])) or '无'})")
        if ollama["matched_model"] and ollama["matched_model"] != ollama.get("required_model_hit"):
            print(f"  - 兼容命中   : ⚠️ {ollama['matched_model']}（与配置不一致，仅提示，不作为验收通过）")
        if ollama.get("probe_dimension"):
            print(f"  - 探测维度   : ✅ {ollama['probe_dimension']}")
        else:
            env_blocked = True
            print(f"  - 探测维度   : ❌ 为空 ({ollama.get('probe_error', '未知错误')})，不得视为环境正常")
        print(f"  - 已装模型数 : {len(ollama['models'])} 个")
    else:
        env_blocked = True
        print("  - 服务状态   : ⚠️ 未连接或未启动")
        print(f"  - 详情说明   : {ollama.get('error')}")
        print("  - 影响       : 问答将默认回退到符号/标题匹配模式，语义向量加速暂不可用")

    if diag["library_status"]:
        print("-" * 60)
        print("[3] 知识库目录规范检查:")
        lib = diag["library_status"]
        print(f"  - 库目录路径 : {lib['path']}")
        print(f"  - 基础三目录 : 原文={'✅' if lib['has_raw'] else '❌'}, "
              f"段索引={'✅' if lib['has_segment_index'] else '❌'}, "
              f"书导航={'✅' if lib['has_book_nav'] else '❌'}")
        print(f"  - 语义索引库 : {'✅ 已就绪' if lib['has_semantic_index'] else '未初始化(将在建索引时创建)'}")

    print("=" * 60)
    if env_blocked:
        print("❌ 环境未通过：配置模型未精确命中或探测维度为空，请先修复再重建。")
        return 1
    if not all_deps_ok:
        print("💡 建议运行安装缺失依赖: pip install pdfplumber python-docx requests python-dotenv numpy")
        return 1
    print("✨ 核心基础环境完全就绪！")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    main()
