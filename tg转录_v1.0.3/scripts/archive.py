"""Telegram 归档薄 CLI 路由（Task 2 的导出/状态部分）。

本模块只做「子命令名 -> 对应模块处理函数」的路由与退出码约定，不含业务实现：
选择合同在 job.py、能力报告在 preflight.py、分段与完成状态在 state.py，媒体、
阅读、导入、检索、分享、增量、方案各路由到 media.py / reading.py /
importing.py / catalog.py / sharing.py / updating.py / presets.py。
12 个子命令已全部注册（validate、check、export、status、resume、download、
render、import、search、share、update、preset），没有未实现动作，不留空壳。

退出码（单聊天与多聊天走同一条聚合口径，不再有任一路径绕过）：

* ``0`` 所选输出全部验收；
* ``1`` partial/failed，或运行期 IO 异常（状态文件读不出、落盘失败等）：成功段与
  诊断以任务状态目录实况为准；IO 异常不是「配置非法」，不归退出码 2；
* ``2`` 选择或配置非法（缺字段、身份/摘要冲突、lock_root 或参数模板写法非法），
  在启动任何子进程之前拒绝；
* ``3`` 能力或依赖缺口导致不启动任何子进程：导出参数未核对、会话锁被其它归档任务占用、
  锁未能释放（不伪装已释放）。

作为脚本运行时同目录模块直接可导入；在测试里由
work/telegram-archive-tests/bootstrap.py 把本目录加入 sys.path。
"""

import argparse
import json
import re
import sys
from pathlib import Path

from job import chat_identity, job_fingerprint, job_summary, validate_job
from preflight import inspect_environment
import catalog
import importing
import media
import presets
import reading
import sharing
import state
import updating

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 1
EXIT_REJECTED = 2
EXIT_CAPABILITY = 3

PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

# 参数模板里允许出现的占位符（本模块的替换接口，不代表 tdl 的旗标名）。
# 本机 tdl 0.20.4 的导出旗标面以 work/upstream/tdl-help-0.20.4.txt 的 `tdl chat export`
# 段为唯一权威：-c/--chat、-T/--type [time,id,last]、-i/--input、-o/--output、--all、
# --raw、--with-content、-f/--filter、--reply、--topic，全局 -n/--ns、--storage、--proxy。
PLACEHOLDER_NAMES = frozenset(("chat_id", "destination", "display_name", "locator", "lower",
                               "namespace", "output", "peer_type", "segment_id", "tdl",
                               "upper"))


# ---------------------------------------------------------------- 输入读取

def _read_job(args):
    if not args.job:
        raise ValueError("%s 需要 --job 指向任务配置 JSON 文件" % args.command)
    payload = json.loads(Path(args.job).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("任务配置必须是 JSON 映射(dict)：由用户选择与运行参数组成")
    return payload


def _template_builder(args):
    """把调用方给的参数模板变成 command_builder；模板本身由使用者提供，本模块不猜 tdl 参数名。

    未知占位符在解析模板时就拒绝（配置非法＝退出码 2），不留到子进程边界才发现。
    """
    if not args.command_template:
        return None
    raw = json.loads(Path(args.command_template).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise ValueError("导出参数模板必须是非空的字符串 JSON 数组（参数列表，绝不拼 shell 字符串）")
    template = list(raw)
    unknown = sorted({name for item in template for name in PLACEHOLDER.findall(item)}
                     - PLACEHOLDER_NAMES)
    if unknown:
        raise ValueError("导出参数模板含未知占位符：%s（可用占位符：%s）"
                         % ("、".join("{%s}" % name for name in unknown),
                            "、".join(sorted("{%s}" % name for name in PLACEHOLDER_NAMES))))

    def build(config, segment, temporary):
        values = {"lower": segment["lower"], "upper": segment["upper"],
                  "chat_id": segment["chat_id"], "segment_id": segment["segment_id"],
                  "peer_type": config["chats"][0]["peer_type"],
                  "display_name": config["chats"][0]["display_name"],
                  "locator": config["chats"][0]["locator"], "output": str(temporary),
                  "tdl": config.get("tdl"), "destination": config.get("destination"),
                  "namespace": config.get("namespace")}

        def replace(match):
            name = match.group(1)
            if name not in values:
                raise ValueError("导出参数模板含未知占位符：{%s}" % name)
            if values[name] is None:
                raise ValueError("导出参数模板占位符 {%s} 在本次配置里没有值" % name)
            return str(values[name])

        return [PLACEHOLDER.sub(replace, item) for item in template]

    return build


def _report_exit(report):
    return {"complete": EXIT_COMPLETE, "unavailable": EXIT_CAPABILITY}.get(
        report["status"], EXIT_INCOMPLETE)


def _declared_runtime_crash(command, error):
    """import/download/render 的运行期兜底：说清原因与落点，且**只打一行纯文本**、绝不与 JSON 混发。

    组合层（``media.run_download`` / ``reading.run_render`` / ``importing.run_import``）本应把
    **每一条**失败路径都收成状态并写出里程碑；真穿到这里就是组合层没兜住的缺陷，按运行期失败（1）
    申报。此处如实说明：没有可解析的报告 JSON，也**不宣称已写出里程碑**（里程碑是否落盘无法确认，
    须整体重跑后再看任务状态目录的 outputs.json）。配置非法（ValueError）与锁被占用（LockBusy）
    原样抛给 ``main`` 按 2 / 3 归类，不走这条兜底。
    """
    print("%s 未能完成：运行期异常 %s：%s；这是组合层未兜住的运行期缺陷，本次没有可解析的"
          "报告 JSON，也不申报「已写出里程碑」（是否落盘无法确认，需整体重跑后再核对任务状态目录"
          "的 outputs.json），按运行期失败退出码 1"
          % (command, type(error).__name__, error))


def _guarded(command, call):
    """只给 import/download/render 用：运行期异常（含 OSError）一律归 1 并只打一行纯文本说明。

    兜底分支返回 ``None``（不是退出码 int）——调用方拿到的一定是 report dict 或 None，
    判 None 后直接返回退出码，绝不会把 int 当 dict 下标（``report["status"]`` 触发
    ``TypeError: 'int' object is not subscriptable``），stdout 也就不会「在报告 JSON 的位置
    打出一个裸 1」。配置非法（ValueError）与锁被占（state.LockBusy）原样抛出，交给 ``main``
    按 2 / 3 归类。
    """
    try:
        return call()
    except (ValueError, state.LockBusy):
        raise                       # 配置非法 2 / 锁被占 3：口径不动，仍由 main 归类
    except Exception as error:  # noqa: BLE001 - 运行期兜底：只打纯文本、返回 None，绝不伪装成功报告
        _declared_runtime_crash(command, error)
        return None


# ---------------------------------------------------------------- 子命令处理函数

def _cmd_import(args):
    """import：只追加注册与转发（Task 5）——业务全在 importing.py，本函数不碰锁与退出码口径。

    离线动作：不要求 tdl / storage / namespace，不登录、不联网、不递归磁盘；``sources``
    只能是用户明确选定的路径。报告的 ``headline`` 用中文写明「全部/部分/失败」，
    退出码仍按既有口径：complete 0、partial/failed 1、配置非法 2、能力缺口 3。
    """
    config = validate_job(_read_job(args))
    if config["action"] != args.command:
        raise ValueError("配置里的 action=%s 与本次子命令 %s 不一致（不代为改写动作）"
                         % (config["action"], args.command))
    report = _guarded("import", lambda: importing.run_import(config))
    if report is None:                        # 运行期兜底已打纯文本说明，不再伪装成功报告
        return EXIT_INCOMPLETE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _report_exit(report)


def _cmd_validate(args):
    """validate：只解析配置，不连接 Telegram、不读写归档。"""
    config = validate_job(_read_job(args))
    identities = [chat_identity(entry) for entry in config["chats"]]
    print(json.dumps({"config": config,
                      "identity": identities[0] if len(identities) == 1 else None,
                      "identities": identities, "fingerprint": job_fingerprint(config),
                      "summary": job_summary(config)}, ensure_ascii=False, indent=2))
    return EXIT_COMPLETE


def _cmd_check(args):
    """check：本地环境与能力报告；会话探测默认关闭，本 CLI 不联网也不执行 tdl。

    lock_root 指向账号存储（登录会话）目录属于配置非法：报告照常出，退出码归 2，
    与 export/resume/download 的拒绝口径一致（不让「check 看着没事、一跑就被拒」误导人）。
    """
    job = _read_job(args)
    action = job.get("action") if isinstance(job, dict) else None
    mutex = state.session_mutex_report(job)
    observations = {
        "tdl_id_range_endpoints": {
            "status": "insufficient_source", "reason": state.ENDPOINT_NOTE,
            "affected_action": "export"},
        # 互斥面不足必须申报，不许沉默（未配 lock_root 时跨 destination 不互斥）。
        mutex["capability"]: {
            "status": mutex["status"], "reason": mutex["reason"],
            "affected_action": action if action in state.MUTEX_AFFECTED_ACTIONS
            else "export"}}
    report = inspect_environment(job, job.get("action"), bool(args.probe_session),
                                observations=observations)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    selection = next((item for item in report["capabilities"]
                      if item["capability"] == "job_selection"), None)
    if selection and selection["status"] != "available":
        return EXIT_REJECTED
    if mutex["lock_scope"] == state.LOCK_SCOPE_REJECTED:
        return EXIT_REJECTED
    return EXIT_COMPLETE


def _run_online(args):
    """export / resume 共用：整任务一把会话锁，逐聊天逐段落盘验收，齐段才发布 final。

    配置或任务身份不符时 state.run_export 直接抛 ValueError（组合层已授权），
    由 main 归为退出码 2；会话被占用/能力缺口由 run_export 收成 unavailable 状态，
    归为 3，两条路径单/多聊天一致。多聊天时状态根由配置里的 destination 与 peer 身份
    决定，下面算出的 root 只用于单聊天。
    """
    config = validate_job(_read_job(args))
    if config["action"] != args.command:
        raise ValueError("配置里的 action=%s 与本次子命令 %s 不一致（不代为改写动作）"
                         % (config["action"], args.command))
    root = state.task_state_root(config["destination"], chat_identity(config["chats"][0]))
    report = state.run_export(root, config, command_builder=_template_builder(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _report_exit(report)


def _cmd_status(args):
    """status：只读汇总三类完成状态，绝不启动或恢复任何子进程。"""
    if not args.state_root and not args.job:
        raise ValueError("status 需要 --root 状态目录，或 --job 配置以推定状态目录")
    job = _read_job(args) if args.job else None
    if args.state_root:
        root = Path(args.state_root)
    else:
        config = validate_job(job)
        if len(config["chats"]) != 1:
            raise ValueError("status 用 --job 定位状态目录时配置只能包含一个聊天")
        root = state.task_state_root(config["destination"], chat_identity(config["chats"][0]))
    report = state.build_status(root, job)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _report_exit(report)


def _cmd_download(args):
    """download：只消费本任务所选媒体索引，三步分开记录（候选索引/下载/验收）。

    未选类别与无法判定（unknown）的消息绝不进下载索引；后端只用 `tdl dl -f <索引> -d
    <目录>`，绝不带 -i/-e（按文件名筛会串类别）、--group（自动扩选相册）、--skip-same
    （同名同尺寸跳过会丢附件）。退出码 0 单独不算附件完整，验收不过就报 partial/failed。

    失败归类：文字源被删/被占这类**凭据不足**报 unavailable（3），跑在中途的其它 OSError
    报 failed（1），两条都由组合层写出 media 里程碑与补漏清单；本函数只兜住漏网的运行期
    异常（同样归 1 并申报原因），配置非法仍归 2。
    """
    config = validate_job(_read_job(args))
    if config["action"] != args.command:
        raise ValueError("配置里的 action=%s 与本次子命令 %s 不一致（不代为改写动作）"
                         % (config["action"], args.command))
    if not args.export_file:
        raise ValueError("download 需要 --export-file 指向本任务的导出 JSON（不自动找文件、"
                         "不递归磁盘）")
    report = _guarded("download", lambda: media.run_download(
        config, args.export_file, media_dir=args.media_dir))
    if report is None:                        # 运行期兜底已打纯文本说明，不再伪装成功报告
        return EXIT_INCOMPLETE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _report_exit(report)


def _cmd_render(args):
    """render：只读「本任务完成清单验收过」的文本源出阅读版；纯媒体选择按合同跳过。

    阅读版与清洗结果都是派生产物，原始 JSON 摘要在构建前后一致（reading.py 记录并核对）；
    未验收/身份不符的 JSON 一律不采信，报 unavailable（退出码 3），绝不出半成品成品；
    发布中途失败报 failed（1）并保留上一次可用视图。**render 一次只吃一个聊天**，多个
    chats 逐个调用（限制同时写进报告 limitations 与 references/reading-and-cleaning.md）。
    """
    config = validate_job(_read_job(args))
    if config["action"] != args.command:
        raise ValueError("配置里的 action=%s 与本次子命令 %s 不一致（不代为改写动作）"
                         % (config["action"], args.command))
    report = _guarded("render", lambda: reading.run_render(
        config, state_root=args.state_root, target=args.reading_dir,
        media_dir=args.media_dir))
    if report is None:                        # 运行期兜底已打纯文本说明，不再伪装成功报告
        return EXIT_INCOMPLETE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] in ("complete", "skipped"):
        return EXIT_COMPLETE
    return _report_exit(report)


# ---------------------------------------------------------------- search（Task 6）

def _absolute_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s 必须是非空绝对路径字符串" % label)
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("%s 必须是绝对路径（不猜相对位置）：%s" % (label, value))
    return str(path)


def _search_database(config, args):
    """索引库位置：显式参数 > 配置 database，两者都没有就按配置非法拒绝（不注入默认路径）。"""
    return _absolute_text(args.database or config.get("database"), "--database/database")


def _load_archives(paths):
    """只吃用户明确选定的派生归档 JSON（不递归磁盘、不把别的文件当归档）。"""
    docs = []
    for raw in paths:
        path = _absolute_text(raw, "sources 里的路径")
        source = Path(path)
        if not source.is_file():
            raise ValueError("索引来源文件不存在：%s（只接受用户选定的归档 JSON，不自动找文件）"
                             % path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("kind") != importing.ARCHIVE_KIND:
            raise ValueError("%s 不是已归一化的归档（kind 应为 %s）：本命令不猜文件内容"
                             % (source.name, importing.ARCHIVE_KIND))
        docs.append(payload)
    return docs


def _attach_reading_pack(docs, reading_pack):
    """把用户选定的阅读版挂到对应聊天上（阅读版入口一律取自真实清单，绝不编路径）。"""
    target = Path(_absolute_text(reading_pack, "--reading-pack"))
    manifest = target / reading.MANIFEST_NAME
    if not manifest.is_file():
        raise ValueError("阅读版清单不存在：%s（view=reading 需要用户选定已建好的阅读版目录）"
                         % manifest)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    chats = payload.get("chats") or {}
    attached = []
    for doc in docs:
        entry = chats.get(doc.get("identity"))
        if not entry:
            continue
        doc["reading"] = {"status": "complete", "target": str(target),
                          "files": [item["path"] for item in entry.get("files") or []],
                          "cleaning": entry.get("cleaning"),
                          "template_compression": entry.get("template_compression")}
        attached.append(doc["identity"])
    return attached


def _search_lock_root(config, database):
    """归档锁的**状态根**：配置 destination 之下的派生层目录（评审 N-2 的同源口径）。

    import 侧以台账父目录（``<destination>/derived``）为状态根取锁，索引/重建侧必须锁
    同一个目录才会撞在同一把锁上；这里复用配置里的 destination 与 ``importing.DERIVED_SUBDIR``
    两个既有事实，不新造第三套路径规则。没配 destination 时退化成索引库所在目录——那把锁
    只与同样以该目录为状态根的一方互斥，报告里的 ``lock.root`` 照实写，不假装跨进程互斥。
    """
    destination = config.get("destination")
    return (Path(str(destination)) / importing.DERIVED_SUBDIR) if destination \
        else Path(str(database)).parent


def _run_search(config, database, query, reading_pack, rebuild=False):
    """按用户选定范围核对索引 → 只读已提交视图检索；归档锁只在要重建时由索引层取。"""
    lock_root = _search_lock_root(config, database)
    marker = catalog.archive_lock_marker(lock_root)
    docs = _load_archives(config["sources"])
    if reading_pack:
        _attach_reading_pack(docs, reading_pack)
    index_report = catalog.index_archive(database, docs, query["view"],
                                         rebuild=rebuild, lock_root=str(lock_root))
    search_report = catalog.search_archive(database, query)
    return {"kind": "telegram-archive-search-report", "version": catalog.CATALOG_VERSION,
            "status": "complete" if index_report["status"] in ("complete", "partial")
            else index_report["status"],
            "view": query["view"], "database": str(database),
            "index": index_report, "search": search_report,
            "runtime": {"tdl_required": False, "session_required": False,
                        "offline_action": True,
                        "note": "search 属离线动作：没有 tdl / storage / namespace 也照跑，"
                                "不登录、不联网、不上传内容"},
            "lock": {"kind": "archive", "taken": bool(index_report["reindexed"]),
                     "marker": marker, "root": str(lock_root),
                     "order": list(state.LOCK_ORDER),
                     "note": "加锁序固定「先归档锁后会话锁」；archive-* 是归档锁名，"
                             "session-* 是会话锁名，两者互不顶替"},
            "claims": dict(search_report["claims"],
                           **{"local_snapshot_only": True,
                              "online_full_export_verified": False,
                              "network_used": False, "login_used": False}),
            "headline": "视图 %s：%s 条命中（关键词按字面匹配，未启用全文分词）"
                        % (query["view"], search_report["total"])}


def _cmd_search(args):
    """search：本地检索（离线动作）——无 tdl / 无登录也能用；查询只读已提交视图。

    索引范围由用户明确选定（``sources`` 里的派生归档，必要时加 ``--reading-pack``），
    本命令不递归磁盘、不自动全建；关键词一律当字面文本（``%``、``_``、引号、反斜杠、
    中文都是普通字符），正文里再像 SQL 的串也不会被执行。原始归档只读，索引是可删
    重建的派生层。退出码沿用既有口径：0 命中或未命中但检索正常、2 配置/查询非法、
    3 归档锁被占或索引不可用（可从原档重建）、1 运行期失败。
    """
    config = validate_job(_read_job(args))
    if config["action"] != args.command:
        raise ValueError("配置里的 action=%s 与本次子命令 %s 不一致（不代为改写动作）"
                         % (config["action"], args.command))
    query = dict(config.get("query") or {})
    if args.view:
        query["view"] = args.view
    if query.get("view") not in catalog.VIEWS:
        # 先拒非法查询，再碰任何文件/数据库：不留下半截索引
        raise ValueError("search 必须在 query.view 里显式写明视图（可选 %s），不默认、不猜"
                         % "、".join(catalog.VIEWS))
    database = _search_database(config, args)
    try:
        report = _guarded("search", lambda: _run_search(config, Path(database), query,
                                                        args.reading_pack,
                                                        rebuild=bool(args.rebuild)))
    except catalog.CatalogError as error:
        print("search 未能完成：本地索引不可用（%s）。原始归档没有被改动、仍可直接读取；"
              "schema 版本落后或被人篡改会在下次检索时自动从原档重建、不必手工删库，"
              "只有结构损坏（缺表、没有版本记录）才需要删掉索引库后按用户选定的来源重建，"
              "按能力缺口退出码 3" % error)
        return EXIT_CAPABILITY
    except state.LockBusy as error:
        print("search 未能开始：同一归档的归档锁被占用（%s）；检索与索引和在线更新按归档锁"
              "互斥，请等对方结束后再试，按能力缺口退出码 3" % error)
        return EXIT_CAPABILITY
    if report is None:                        # 运行期兜底已打纯文本说明，不再伪装成功报告
        return EXIT_INCOMPLETE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _report_exit(report)


# ---------------------------------------------------------------- share（Task 7）

def _cmd_share(args):
    """share：只追加注册与转发（Task 7）——业务全在 sharing.py，本函数不碰锁与退出码口径。

    离线动作：没有 tdl / storage / namespace 也照跑，不登录、不联网、不上传内容。分享
    目标（输出位置）、脱敏字段、是否带附件与逐项附件选择都在任务配置的 ``share`` 块里由
    用户显式给出，本 CLI 一律不注入默认值（少一项就按配置非法拒绝）。化名映射表只写在
    私有归档位置（``destination``），由 sharing.py 复用 ``state.acquire_archive_lock``
    互斥；分享目录里不会出现映射表、会话文件、代理凭据、完整 raw 或私有绝对路径。
    退出码沿用既有口径：complete 0、partial 1、配置或选项非法 2、归档锁被占 3。
    """
    config = validate_job(_read_job(args))
    if config["action"] != args.command:
        raise ValueError("配置里的 action=%s 与本次子命令 %s 不一致（不代为改写动作）"
                         % (config["action"], args.command))
    report = _guarded("share", lambda: sharing.run_share(config))
    if report is None:                        # 运行期兜底已打纯文本说明，不再伪装成功报告
        return EXIT_INCOMPLETE
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return _report_exit(report)


# ---------------------------------------------------------------- update（Task 8）

def _cmd_update(args):
    """update：增量更新——只做注册与转发，业务全在 updating.py，本函数不碰锁与退出码口径。

    覆盖清单、上界探测、抓取结果、索引库四个文件都要显式给出（updating.build_parser
    的必需参数），缺任何一个由 updating.main 按配置非法拒绝（退出码 2），本 CLI
    不注入默认值。归档锁被占、索引不可用由 updating.main 按能力缺口报 3。
    """
    argv = ["update"]
    for flag, value in (("--manifest", args.manifest), ("--probe", args.probe),
                        ("--fetched", args.fetched), ("--database", args.database),
                        ("--lock-root", args.lock_root), ("--out", args.out),
                        ("--recheck-since", args.recheck_since),
                        ("--recheck-until", args.recheck_until),
                        ("--resume-round", args.resume_round)):
        if value:
            argv += [flag, str(value)]
    if args.lookback_days is not None:
        argv += ["--lookback-days", str(args.lookback_days)]
    return updating.main(argv)


# ---------------------------------------------------------------- preset（Task 9）

def _cmd_preset(args):
    """preset：只在用户要求时保存/列出/读取/调用常用方案，绝不启动任务、不建定时作业。

    方案目录与本轮上下文全部显式给出：本 CLI 不注入默认目录、不读真实时钟（`use` 缺
    --preset-now 直接按配置非法拒绝），打印的是去掉内存 job 的可展示视图，
    退出码沿用既有口径（0 成功、2 需用户重选/配置非法、3 能力缺口）。
    """
    action = args.preset_action or "list"
    # 空值旗标整对丢弃：只剩裸旗标（如 ["list", "--preset-dir", "--name"]）会被
    # argparse 判参数不合法，Task 9 报告片段里的 `if item` 过滤有此缺口，这里按对组装。
    argv = [action]
    if args.preset_dir:
        argv += ["--preset-dir", args.preset_dir]
    if args.preset_name:
        argv += ["--name", args.preset_name]
    if action == "save":
        if args.job:
            argv += ["--job", args.job]
        if args.preset_overwrite:
            argv.append("--overwrite")
    elif action == "use":
        if args.preset_now:
            argv += ["--now", args.preset_now]
        for flag, value in (("--overrides", args.preset_overrides),
                            ("--accounts", args.preset_accounts),
                            ("--candidates", args.preset_candidates)):
            if value:
                argv += [flag, value]
    return presets.main(argv)


# 后续任务在本字典追加一行："<子命令>": (<处理函数>, "<一行中文说明>")。
# 说明文本只写中文，不写英文命令名，帮助页因此不会提前暴露未实现的动作。
COMMANDS = {
    "validate": (_cmd_validate, "只解析配置，不连接 Telegram"),
    "check": (_cmd_check, "本地环境与能力报告"),
    "export": (_run_online, "分段导出并逐段验收"),
    "status": (_cmd_status, "只读汇总三类完成状态"),
    "resume": (_run_online, "复用已验收段继续导出"),
    "download": (_cmd_download, "只消费本任务所选媒体索引并验收附件"),
    "render": (_cmd_render, "只读已验收的文本源出阅读版"),
    "import": (_cmd_import, "把用户选定的本地导出归一化成归档"),
    "search": (_cmd_search, "在本地索引里按关键词/聊天/日期/消息 ID 检索"),
    "share": (_cmd_share, "把选定的归档出成脱敏后的独立分享副本"),
    "update": (_cmd_update, "按已验收覆盖清单做增量更新"),
    "preset": (_cmd_preset, "保存与调用常用方案（按用户要求动手，不启动任务、不建定时作业）"),
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="archive.py",
        description="Telegram 归档本地薄封装：按子命令路由到对应模块。",
        epilog="可用子命令：" + "、".join(sorted(COMMANDS)),
    )
    parser.add_argument("command", nargs="?", help="要执行的子命令")
    parser.add_argument("--job", help="任务配置 JSON 文件路径")
    parser.add_argument("--root", dest="state_root", help="状态目录（status 使用）")
    parser.add_argument("--command-template", dest="command_template",
                        help="导出参数模板 JSON 数组文件（占位符按段代入）")
    parser.add_argument("--probe-session", dest="probe_session", action="store_true",
                        help="请求会话探测（本 CLI 不联网，只如实标注未验证）")
    # download 专用入参（追加项，不影响上面已有子命令的解析）：
    parser.add_argument("--export-file", dest="export_file",
                        help="本任务的导出 JSON 文件（download 按原始元数据分类）")
    parser.add_argument("--media-dir", dest="media_dir",
                        help="附件保存目录（download：默认 <destination>/media/<身份>；"
                             "render：必须与本任务状态目录同源，不同源按配置非法拒绝）")
    # render 专用入参（追加项）：阅读版输出目录，默认落在 destination/阅读版
    parser.add_argument("--reading-dir", dest="reading_dir",
                        help="阅读版输出目录（render 使用，默认 <destination>/阅读版）")
    # search 专用入参（Task 6 追加项）：索引库、视图与阅读版入口，全部显式给出、不注入默认
    parser.add_argument("--database", dest="database",
                        help="本地索引库路径（search 使用，必须是非空绝对路径）")
    parser.add_argument("--view", dest="view",
                        help="检索视图（search 使用：raw 原始正文 / reading 阅读版）")
    parser.add_argument("--reading-pack", dest="reading_pack",
                        help="用户选定的阅读版目录（search 走 reading 视图时提供阅读入口）")
    parser.add_argument("--rebuild", dest="rebuild", action="store_true",
                        help="强制重建本地索引（search 使用；默认只读已提交视图）")
    # update 专用入参（Task 8 追加项）：四个必需文件与回查选项，全部显式给出、不注入默认
    parser.add_argument("--manifest", dest="manifest",
                        help="已验收覆盖清单 JSON（update 使用，必需）")
    parser.add_argument("--probe", dest="probe",
                        help="本轮各聊天上界探测值 JSON（update 使用，必需）")
    parser.add_argument("--fetched", dest="fetched",
                        help="本轮抓取结果 JSON（update 使用，必需）")
    parser.add_argument("--out", dest="out",
                        help="把本轮 update 报告写到该 JSON 文件")
    parser.add_argument("--lock-root", dest="lock_root",
                        help="归档锁根（update 使用；默认取索引库所在目录）")
    parser.add_argument("--lookback-days", dest="lookback_days", type=int,
                        help="回查天数（update 使用，必须有界）")
    parser.add_argument("--recheck-since", dest="recheck_since",
                        help="显式替换回查起点（update 使用）")
    parser.add_argument("--recheck-until", dest="recheck_until",
                        help="显式替换回查终点（update 使用）")
    parser.add_argument("--resume-round", dest="resume_round",
                        help="上一轮计划 JSON（update 使用：沿用其冻结上界）")
    # preset 专用入参（Task 9 追加项）：子动作、方案目录与本轮上下文，全部显式给出
    parser.add_argument("--preset-action", dest="preset_action",
                        choices=["save", "list", "show", "use"], help="方案动作")
    parser.add_argument("--preset-dir", dest="preset_dir",
                        help="方案目录（不得位于技能发布包内）")
    parser.add_argument("--preset-name", dest="preset_name", help="方案名")
    parser.add_argument("--preset-now", dest="preset_now",
                        help="use：本次调用时刻（带时区 ISO；不读真实时钟）")
    parser.add_argument("--preset-overrides", dest="preset_overrides",
                        help="use：本轮用户重述字段的 JSON 文件（逐字段替换，不做并集）")
    parser.add_argument("--preset-accounts", dest="preset_accounts",
                        help="use：本次可用账号清单 JSON 文件（本地引用）")
    parser.add_argument("--preset-candidates", dest="preset_candidates",
                        help="use：本次聊天清单 JSON 文件（用于唯一解析目标）")
    parser.add_argument("--preset-overwrite", dest="preset_overwrite", action="store_true",
                        help="覆盖同名方案（仅在用户明确要求时）")
    return parser


def main(argv=None):
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as signal:
        return EXIT_REJECTED if signal.code not in (0, None) else EXIT_COMPLETE
    if not args.command:
        parser.print_help()
        return EXIT_REJECTED
    if args.command not in COMMANDS:
        print("未知子命令：%s；本次已实现的子命令：%s"
              % (args.command, "、".join(sorted(COMMANDS))))
        return EXIT_REJECTED
    handler = COMMANDS[args.command][0]
    try:
        return handler(args)
    except state.LockBusy as error:
        # 组合层已把「会话被占用」收成 unavailable 状态；这里兜住任何直接抛出的情形，
        # 让锁文件路径可读可见，绝不向外抛裸 traceback。
        print("%s 未能开始：%s" % (args.command, error))
        return EXIT_CAPABILITY
    except ValueError as error:
        # 选择或配置非法：在启动任何子进程之前拒绝（退出码 2）。
        print("%s 被拒绝：%s" % (args.command, error))
        return EXIT_REJECTED
    except OSError as error:
        # 运行期 IO 异常（状态文件读不出、落盘失败等）：不是「配置非法」，按运行期
        # 失败（退出码 1）申报；成功段与诊断以任务状态目录实况为准，不在这里重断言。
        print("%s 读取或写入失败：%s：%s；按运行期失败退出码 1（不是配置非法）"
              % (args.command, type(error).__name__, error))
        return EXIT_INCOMPLETE
    except Exception as error:  # noqa: BLE001 - 运行期兜底：报人话，不抛 traceback
        # 文案不再写「未归类」这种读不懂的话：说清已保留什么、去哪儿看、为什么是 1。
        print("%s 运行期异常：%s：%s；本次已保留成功段与诊断（任务状态目录的 outputs.json "
              "里程碑、tmp/ 与 segments/），按运行期失败退出码 1"
              % (args.command, type(error).__name__, error))
        return EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
