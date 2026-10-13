# -*- coding: utf-8 -*-
"""知识库交互与可视化控制台服务 (start_web.py)

功能：
1. 启动轻量级 Web 控制台（默认仅监听 127.0.0.1:5888）
2. 提供搜索接口与状态监控接口
3. 前端支持卡片流展示、胶囊标签（概念/方法/数据/观点）、行号高亮、一键复制
4. 支持在 Web 界面一键触发增量索引同步与系统体检

使用方式：
    python start_web.py [--lib <知识库路径>] [--port 5888]
"""

import os
import sys
import json
import argparse
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import urllib.parse

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
WEB_DIR = SKILL_DIR / "web"
sys.path.insert(0, str(SCRIPTS_DIR))

import config as v5config

# 默认值统一来自 config.py（支持 INDEX_LIB_DIR / INDEX_WEB_HOST / INDEX_WEB_PORT）
DEFAULT_LIB = Path(v5config.get_config().lib_dir)
DEFAULT_HOST = v5config.DEFAULT_WEB_HOST
DEFAULT_PORT = v5config.DEFAULT_WEB_PORT

# KB-03：本地控制台只服务本机。CORS 默认仅放行环回来源，禁止返回 `*`；
# Host 非环回的请求直接 403，防止任意跨域网页读取本地知识库原文。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _allowed_cors_origins(port: int) -> set[str]:
    """该端口上唯一合法的两个环回来源。"""
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}


def get_library_stats(lib_path: Path):
    """统计知识库基本概况"""
    stats = {
        "raw_count": 0,
        "index_count": 0,
        "nav_count": 0,
        "vector_count": 0,
        "vector_status": "未构建",
    }
    if not lib_path.exists():
        return stats

    raw_dir = lib_path / "原文"
    idx_dir = lib_path / "段索引"
    nav_dir = lib_path / "书导航"
    vec_file = lib_path / ".semantic_index" / "manifest.json"

    if raw_dir.exists():
        stats["raw_count"] = len(list(raw_dir.rglob("*.txt")) + list(raw_dir.rglob("*.md")))
    if idx_dir.exists():
        stats["index_count"] = len(list(idx_dir.rglob("*.md")))
    if nav_dir.exists():
        stats["nav_count"] = len(list(nav_dir.rglob("*.md")))
    if vec_file.exists():
        try:
            with open(vec_file, "r", encoding="utf-8") as f:
                manifest = json.load(f)
                stats["vector_status"] = f"已就绪 ({manifest.get('model', 'Ollama')})"
                total_v = sum(s.get("count", 0) for s in manifest.get("segments", []))
                stats["vector_count"] = total_v
        except Exception:
            stats["vector_status"] = "索引受损"

    return stats


class KnowledgeWebHandler(SimpleHTTPRequestHandler):
    lib_path: Path = DEFAULT_LIB
    # SearchSnapshot：embeddings/chunks/manifest/embedder/lexical_index 同一 build_id
    # 原子快照。局部加载校验后一次性发布，禁止边加载边发布。
    _snapshot_cache = None
    _snapshot_build_id = None
    _cache_lock = threading.Lock()
    # 后台预热状态机：starting/warming/ready/degraded/blocked
    _warm_state: dict = {"state": "starting", "stage": "init", "progress": 0.0,
                         "build_id": None, "error_code": "", "user_action": ""}
    _warm_thread = None
    # 并发搜索上限：超限返回 429，避免多次点击产生并发重计算
    _active_searches = 0
    _max_concurrent_searches = 2
    _search_sem = threading.Semaphore(2)
    # 模型探测 TTL（秒）：健康接口不每次重新请求 embedding
    _probe_cache: dict = {}
    _probe_ttl = 20
    server_version = "knowledge-index-v5/" + v5config.APP_VERSION

    @classmethod
    def _set_warm(cls, **fields):
        cls._warm_state.update(fields)

    @classmethod
    def _load_snapshot(cls, semantic_dir: str) -> dict:
        """加载同一 build_id 的完整快照：向量+词法文件+manifest+embedder。

        词法走持久化文件直接加载，不再现场遍历全部文档；embedder 失败时
        进入 degraded（纯词法仍可搜），索引损坏/维度不一致则抛错 blocked。
        """
        from semantic_search import (
            LexicalIndex,
            load_embedding_model,
            load_semantic_index,
        )
        manifest_path = os.path.join(semantic_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            disk = json.load(f)
        build_id = disk.get("build_id")
        manifest, embeddings, chunks = load_semantic_index(semantic_dir)
        lex_entry = manifest.get("lexical") or {}
        lex_name = lex_entry.get("npz")
        if not lex_name:
            raise ValueError("索引损坏：manifest 缺少 lexical 快照引用（blocked）")
        lex_path = os.path.join(semantic_dir, os.path.basename(lex_name))
        if not os.path.exists(lex_path):
            raise ValueError(f"索引损坏：词法快照缺失 {lex_name}（blocked）")
        t_lex0 = time.time()
        lexical_index = LexicalIndex.load_file(lex_path, expected_build_id=build_id)
        if lexical_index.document_count != len(chunks):
            raise ValueError("索引损坏：词法文档数与向量块数不一致（blocked）")
        lex_ms = round((time.time() - t_lex0) * 1000, 1)
        try:
            embedder = load_embedding_model(manifest.get("model"))
            expected_dim = manifest.get("dimension")
            actual_dim = getattr(embedder, "dim", None)
            if actual_dim is None and hasattr(embedder, "get_sentence_embedding_dimension"):
                actual_dim = embedder.get_sentence_embedding_dimension()
            if expected_dim and actual_dim and int(expected_dim) != int(actual_dim):
                raise ValueError(
                    f"查询模型维度 {actual_dim} 与索引维度 {expected_dim} 不一致，请重建索引"
                )
            degraded = False
            degraded_reason = ""
        except Exception as exc:
            embedder = None
            degraded = True
            degraded_reason = str(exc)
        snap = {
            "build_id": build_id,
            "manifest": manifest,
            "embeddings": embeddings,
            "chunks": chunks,
            "embedder": embedder,
            "lexical_index": lexical_index,
            "lex_load_ms": lex_ms,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
        }
        return snap

    @classmethod
    def ensure_warm(cls, semantic_dir: str) -> dict:
        """同步预热入口：快照缺失或 build_id 变化时加载，否则直接返回。"""
        try:
            with open(os.path.join(semantic_dir, "manifest.json"), encoding="utf-8") as f:
                disk_build = json.load(f).get("build_id")
        except Exception:
            disk_build = None
        snap = cls._snapshot_cache
        if snap is not None and snap.get("build_id") == disk_build:
            return snap
        with cls._cache_lock:
            snap = cls._snapshot_cache
            if snap is not None and snap.get("build_id") == disk_build:
                return snap
            cls._set_warm(state="warming", stage="load_snapshot", progress=0.2, build_id=disk_build)
            try:
                snap = cls._load_snapshot(semantic_dir)
            except Exception as exc:
                cls._set_warm(state="blocked", stage="error", progress=1.0,
                              error_code="SNAPSHOT_BLOCKED", user_action=str(exc)[:300])
                raise
            cls._snapshot_cache = snap
            cls._snapshot_build_id = snap.get("build_id")
            if snap.get("degraded"):
                cls._set_warm(state="degraded", stage="lexical_only", progress=1.0,
                              build_id=snap.get("build_id"),
                              error_code="EMBEDDER_OFFLINE",
                              user_action=f"向量模型离线，已降级为纯词法搜索：{snap.get('degraded_reason','')[:200]}")
            else:
                cls._set_warm(state="ready", stage="ready", progress=1.0,
                              build_id=snap.get("build_id"), error_code="", user_action="")
            print(f"[cache] 语义快照已载入: {len(snap['chunks'])} 块 build={snap.get('build_id')} "
                  f"lex={snap.get('lex_load_ms')}ms degraded={snap.get('degraded')}", flush=True)
            return snap

    @classmethod
    def start_background_warm(cls, semantic_dir: str):
        """启动后立即后台预热：首页可打开，ready 前搜索按钮禁用并显示进度。"""
        if cls._warm_thread is not None and cls._warm_thread.is_alive():
            return
        cls._set_warm(state="warming", stage="queued", progress=0.05)

        def _run():
            try:
                cls.ensure_warm(semantic_dir)
            except Exception:
                pass
        cls._warm_thread = threading.Thread(target=_run, daemon=True)
        cls._warm_thread.start()

    def _request_host(self) -> str:
        """Host 头的主机部分（去端口、小写）；缺失或畸形返回空串。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        else:
            host = host.split(":", 1)[0]
        return host.strip()

    def _host_allowed(self) -> bool:
        """KB-03：仅允许环回 Host（localhost/127.0.0.1）。"""
        return self._request_host() in _LOOPBACK_HOSTS

    def _cors_origin(self) -> str | None:
        """KB-03：Origin 命中本端口环回白名单才回显，否则一律不返回 CORS 头。"""
        origin = (self.headers.get("Origin") or "").strip()
        try:
            port = self.server.server_address[1]
        except Exception:
            port = DEFAULT_PORT
        if origin and origin in _allowed_cors_origins(port):
            return origin
        return None

    def _send_forbidden(self) -> None:
        self._send_json({"error": "仅允许来自本机的请求"}, 403)

    def do_OPTIONS(self):
        if not self._host_allowed():
            self._send_forbidden()
            return
        self.send_response(200)
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        if not self._host_allowed():
            self._send_forbidden()
            return
        url = urllib.parse.urlparse(self.path)
        path = url.path
        params = urllib.parse.parse_qs(url.query)

        # 1. 静态主页
        if path in ["/", "/index.html"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_file = WEB_DIR / "index.html"
            if html_file.exists():
                with open(html_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.wfile.write(b"<h1>Web UI index.html not found</h1>")
            return

        # 2b. 状态计数 API：只返回计数/版本/模型/build_id/健康，不再全量返回文件列表。
        # 文件列表走 /api/files 分页接口，避免 346 文件 52KB 拖慢管理页。
        if path == "/api/stats":
            stats = get_library_stats(self.lib_path)
            try:
                manifest_path = self.lib_path / ".semantic_index" / "manifest.json"
                build_id = model = None
                vector_count = 0
                if manifest_path.exists():
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    vector_count = int(manifest.get("chunk_count", 0) or 0)
                    build_id = manifest.get("build_id")
                    model = manifest.get("model")
            except (OSError, ValueError):
                vector_count = 0
                build_id = model = None
            res = {
                "raw_count": stats["raw_count"],
                "index_count": stats["index_count"],
                "nav_count": stats["nav_count"],
                "vector_count": vector_count,
                "vector_status": stats["vector_status"],
                "build_id": build_id,
                "model": model,
                "lib": str(self.lib_path),
            }
            self._send_json(res)
            return

        # 2c. 文件分页接口：/api/files?offset=0&limit=100&query=xxx（limit 最大 500）
        if path == "/api/files":
            try:
                offset = max(0, int(params.get("offset", [0])[0]))
            except ValueError:
                offset = 0
            try:
                limit = max(1, min(500, int(params.get("limit", [100])[0])))
            except ValueError:
                limit = 100
            q = params.get("query", [""])[0].strip().lower()
            files = []
            try:
                raw_dir = self.lib_path / "原文"
                if raw_dir.exists():
                    for fp in sorted(raw_dir.rglob("*")):
                        if fp.is_file() and fp.suffix.lower() in {".txt", ".md"}:
                            if q and q not in fp.name.lower():
                                continue
                            try:
                                files.append({
                                    "name": fp.name,
                                    "dir": fp.parent.name,
                                    "size": fp.stat().st_size,
                                    "path": str(fp.relative_to(self.lib_path)).replace("\\", "/"),
                                })
                            except OSError:
                                continue
            except OSError:
                files = []
            self._send_json({"total": len(files), "offset": offset, "limit": limit,
                             "files": files[offset:offset + limit]})
            return

        # 2d. 健康接口：live 只证明存活（50ms 内）；ready 检查快照与模型（500ms 内，
        # 模型探测走 TTL 缓存，不每次请求 embedding）；search 返回缓存与搜索状态。
        if path == "/api/health/live":
            self._send_json({"live": True})
            return
        if path == "/api/health/ready":
            snap = type(self)._snapshot_cache
            warm = dict(type(self)._warm_state)
            probe_dim = None
            probe_err = ""
            try:
                import time as _t
                now = _t.time()
                cached = type(self)._probe_cache
                if cached and now - cached.get("at", 0) < type(self)._probe_ttl:
                    probe_dim = cached.get("dim")
                    probe_err = cached.get("err", "")
                else:
                    manifest_path = self.lib_path / ".semantic_index" / "manifest.json"
                    if manifest_path.exists():
                        with open(manifest_path, "r", encoding="utf-8") as f:
                            m = json.load(f)
                        import config as _cfg
                        pr = _cfg.probe_ollama(_cfg.get_config().ollama_base_url,
                                               m.get("model", ""), timeout=3, embed_timeout=8)
                        probe_dim = pr.get("dimension")
                        probe_err = pr.get("error", "")
                        type(self)._probe_cache = {"at": now, "dim": probe_dim, "err": probe_err}
            except Exception as exc:
                probe_err = str(exc)
            try:
                with open(self.lib_path / ".semantic_index" / "manifest.json", encoding="utf-8") as f:
                    disk_build = json.load(f).get("build_id")
            except Exception:
                disk_build = None
            self._send_json({
                "ready": warm.get("state") == "ready",
                "state": warm.get("state"),
                "stage": warm.get("stage"),
                "progress": warm.get("progress"),
                "build_id": disk_build,
                "cache_build_id": (snap or {}).get("build_id") if isinstance(snap, dict) else None,
                "model": None,
                "dimension": None,
                "error_code": warm.get("error_code", ""),
                "user_action": warm.get("user_action", ""),
                "probe_dimension": probe_dim,
                "probe_error": probe_err,
                "active_searches": type(self)._active_searches,
            })
            return
        if path == "/api/health/search":
            snap = type(self)._snapshot_cache
            self._send_json({
                "cache_build_id": (snap or {}).get("build_id") if isinstance(snap, dict) else None,
                "active_searches": type(self)._active_searches,
                "max_concurrent": type(self)._max_concurrent_searches,
            })
            return

        # 2. 状态 API
        if path == "/api/status":
            stats = get_library_stats(self.lib_path)
            from check_env import run_diagnostics
            diag = run_diagnostics(lib_dir=str(self.lib_path))
            res = {"stats": stats, "diagnostics": diag}
            self._send_json(res)
            return

        # 3. 搜索 API：CLI/Web 共用 search_hybrid；快照原子发布；并发上限 2，超限 429。
        if path == "/api/search":
            query = params.get("q", [""])[0].strip()
            try:
                top_k = max(1, min(50, int(params.get("k", [5])[0])))
            except ValueError:
                self._send_json({"error": "k 必须是整数"}, 400)
                return
            if not query or len(query) > 500:
                self._send_json({"error": "查询长度必须为 1~500 字"}, 400)
                return
            if not type(self)._search_sem.acquire(blocking=False):
                self._send_json({"error": "搜索并发已满，请 3 秒后重试", "results": []},
                                429, {"Retry-After": "3"})
                return
            type(self)._active_searches += 1
            try:
                from semantic_search import (
                    definition_core_terms,
                    is_definition_query,
                    search_hybrid,
                )

                semantic_dir = str(self.lib_path / ".semantic_index")
                try:
                    snap = type(self).ensure_warm(semantic_dir)
                except Exception as exc:
                    self._send_json({"error": f"索引未就绪（blocked）：{exc}", "results": []}, 503)
                    return
                t0 = time.time()
                threshold = params.get("threshold", [0.25])[0]
                try:
                    threshold = float(threshold)
                except (TypeError, ValueError):
                    threshold = 0.25
                if snap.get("degraded") or snap.get("embedder") is None:
                    # degraded：纯词法降级（向量恒零 + 词法/关键词照常），明确标记
                    try:
                        results = search_hybrid(
                            query, _LexicalOnlyEmbedder(), _DegradedEmbeddings(len(snap["chunks"])),
                            snap["chunks"],
                            snap["lexical_index"], top_k=top_k, threshold=threshold,
                            build_id=snap["build_id"],
                        )
                    except Exception as exc:
                        self._send_json({"error": f"检索失败: {exc}", "results": []}, 503)
                        return
                    degraded = True
                else:
                    try:
                        results = search_hybrid(
                            query, snap["embedder"], snap["embeddings"], snap["chunks"],
                            snap["lexical_index"], top_k=top_k, threshold=threshold,
                            build_id=snap["build_id"],
                        )
                    except Exception as exc:
                        # Ollama 下线等 embedding 故障：明确 503，不持续转圈
                        self._send_json({"error": f"检索失败: {exc}", "results": []}, 503)
                        return
                    degraded = False
                must_terms = definition_core_terms(query) if is_definition_query(query) else []
                elapsed_ms = round((time.time() - t0) * 1000, 1)
                fallback_used = any(r.get("result_source") == "fallback" for r in results)
                if degraded:
                    for r in results:
                        r["degraded"] = True
                print(f"[search] q={query[:30]} top_k={top_k} {len(results)} hits {elapsed_ms}ms filtered={bool(must_terms)} degraded={degraded}", flush=True)
                self._send_json({"query": query, "results": results, "must_terms": must_terms,
                                 "filter_applied": bool(must_terms), "fallback_used": fallback_used,
                                 "filtered": bool(must_terms), "build_id": snap["build_id"],
                                 "degraded": degraded, "elapsed_ms": elapsed_ms})
            except Exception as e:
                self._send_json({"error": str(e), "results": []}, 500)
            finally:
                type(self)._active_searches -= 1
                type(self)._search_sem.release()
            return

        # 4. 受限原文查看器：只允许读取知识库根目录内的文本，并返回局部行窗口。
        # 安全：拒绝绝对路径/.. /编码变体/符号链接绕过；解析后必须仍位于库根内。
        if path == "/api/source":
            rel = params.get("path", [""])[0]
            try:
                import urllib.parse as _up
                rel = _up.unquote(rel)
            except Exception:
                pass
            try:
                target_line = max(1, int(params.get("line", [1])[0]))
            except ValueError:
                self._send_json({"error": "line 必须是整数"}, 400)
                return
            if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
                self._send_json({"error": "非法路径"}, 403)
                return
            root = self.lib_path.resolve()
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self._send_json({"error": "路径越出知识库"}, 403)
                return
            # 符号链接绕过防护：最终目标不允许是库外符号链接
            try:
                if target.is_symlink() and target.resolve().relative_to(root) is None:
                    self._send_json({"error": "路径越出知识库"}, 403)
                    return
            except ValueError:
                self._send_json({"error": "路径越出知识库"}, 403)
                return
            except OSError:
                pass
            if target.suffix.lower() not in {".txt", ".md"} or not target.is_file():
                self._send_json({"error": "只允许查看知识库内的 txt/md 文件"}, 404)
                return
            try:
                lines = target.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                start = max(1, target_line - 50)
                end = min(len(lines), target_line + 50)
                self._send_json({
                    "path": rel.replace("\\", "/"),
                    "line": target_line,
                    "start": start,
                    "end": end,
                    "lines": [{"number": n, "text": lines[n - 1]} for n in range(start, end + 1)],
                })
            except OSError as exc:
                self._send_json({"error": str(exc)}, 500)
            return

        # 默认处理其他静态文件
        super().do_GET()

    def _send_json(self, data: dict, status: int = 200, extra_headers: dict | None = None):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        # KB-03：仅回显命中的环回 Origin，永不返回 `*`。
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(payload)


def _LexicalOnlyEmbedder():
    """degraded 纯词法降级：向量点积恒零，词法+关键词照常排序。"""
    import numpy as _np

    class _Zero:
        dim = 0

        def encode(self, texts, **kwargs):
            return _np.zeros((len(texts), 1), dtype=_np.float32)
    return _Zero()


class _DegradedEmbeddings:
    """degraded 下的零向量替身：与 chunks 等长，点积恒零。"""

    def __init__(self, n: int):
        self._n = n
        self.shape = (n, 1)

    def __len__(self):
        return self._n

    def __mul__(self, other):
        import numpy as _np
        return _np.zeros(self._n, dtype=_np.float32)

    __rmul__ = __mul__

    def __array__(self, dtype=None):
        import numpy as _np
        return _np.zeros((self._n, 1), dtype=dtype or _np.float32)


def main():
    parser = argparse.ArgumentParser(description="知识库索引系统 Web 控制台服务")
    parser.add_argument("--lib", default=str(DEFAULT_LIB), help="知识库根目录")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Web 服务端口 (默认 5888)")
    args = parser.parse_args()

    lib_path = Path(args.lib).resolve()
    KnowledgeWebHandler.lib_path = lib_path
    KnowledgeWebHandler._set_warm(state="starting", stage="boot", progress=0.02,
                                  build_id=None, error_code="", user_action="")

    os.chdir(str(WEB_DIR))
    if args.host != DEFAULT_HOST:
        print("⚠️ 警告：当前服务将暴露到非本机地址，请确认网络访问边界和知识库敏感性。")
    server = ThreadingHTTPServer((args.host, args.port), KnowledgeWebHandler)
    server.daemon_threads = True
    # 启动后立即后台预热：首页可打开，ready 前前端显示进度
    KnowledgeWebHandler.start_background_warm(str(lib_path / ".semantic_index"))

    print("=" * 60)
    print(f" 知识库索引系统 v{v5config.APP_VERSION} - 现代化 Web 控制台")
    print("=" * 60)
    print(f"[*] 知识库路径: {lib_path}")
    print(f"[+] 控制台已启动: http://localhost:{args.port}")
    print("💡 按 Ctrl + C 停止服务")
    print("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 正在关闭控制台服务...")
        server.server_close()


if __name__ == "__main__":
    main()
