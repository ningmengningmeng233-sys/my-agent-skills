#!/usr/bin/env python3
"""
validate_rewrite.py - 改写前后机械保真检查入口（v1.2 Task 10 起为薄前置层）

本文件不再自带规则：所有 L0/L1 判定与判定码→严重度表都由同目录的
``novel_validation.py``（四层验证合同的机械层实现）提供，这里只保留
v1.1 时代的**判词三态 + 退出码**（v1.2 Task 12 起多一个如实的 NO-OP），
让既有调用方（``file_commit.py``、发布门、回归矩阵）的退出码语义不变。

退出码契约（不变）：
  0 - 检查完全通过，未发现结构、令牌或绑定漂移（PASS_MECHANICAL），
      或两份文件字节完全一致、没有任何改写发生（NO-OP，v1.2 Task 12 起如实报）
  1 - 发现明确的保真违规或结构失真（FAIL）
  2 - 检测到无法机械定案的风险线索，需语义复核（REVIEW）
  3 - 输入参数、文件读取或编码错误（ERROR）

判词集合从三态变成「三态 + NO-OP」时，四个调用方一起对齐（不是只改这里）：
``file_commit.commit_rewrite`` 把 NO-OP 当作「内容一致，不动文件」；
``accept_release.check_negation_matrix`` 与 ``run_negation_matrix.py`` 把 NO-OP 记为
合法正例通过，危险反例仍要求「既不是 PASS_MECHANICAL 也不是 NO-OP」；
``novel_output.publish`` 的 NO-OP 交付语义与此同源（设计方案 84 行）。

v1.2 起被移除的脆弱规则（详见 novel_validation 模块文档与
tests/test_novel_validation.py 的双向用例）：
  * 「新增连续 12 字即违规」→ 按文类与授权成形的两条风险线索
    （``unsourced_new_content`` / ``verbatim_carryover_undocumented``），
    单纯调高阈值被设计方案 72 行明确否定；
  * 锁住所有中文引号 → 引文按「显式引用 vs 小说对白」分类，区分不了进 REVIEW；
  * 标题只比级别 → 比 (级别, 文字) 序列，另加数量与顺序；
  * 段落数必须相同 → 段落消失才报，且由删除授权解释，缺授权进 REVIEW。
仍然保留的真实约束：显式引用、代码、数字及其绑定对象、专名、日期时间、
版本、URL/链接、表格、保护项与删除授权、否定/条件/限定强度。
"""

import sys
from pathlib import Path

EXIT_PASS, EXIT_FAIL, EXIT_REVIEW, EXIT_ERROR, EXIT_USAGE = 0, 1, 2, 3, 3

#: 「没有把该改的改坏」的判词集合：机械通过，或压根没发生改写（NO-OP）。
#: 判词由本模块产生，消费方（``accept_release`` 的否定矩阵、``run_negation_matrix.py``）
#: 从这里取同一份名单，不再各自抄一遍状态字面量——抄两份的下场是先修问题第 11 项。
PASSING_VERDICTS: tuple[str, ...] = ("PASS_MECHANICAL", "NO-OP")

_ENGINE = None
_ENGINE_PATH = Path(__file__).resolve().with_name("novel_validation.py")


def engine():
    """取规则引擎本体。

    与 ``file_commit._load_validator`` 同源的做法：优先复用 ``sys.modules`` 里
    已加载的模块，否则「读源码 → compile → exec」，避免在运行包目录里长出
    ``__pycache__`` 而让安装目录的文件集合与发布清单失配。
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    loaded = sys.modules.get("novel_validation")
    if loaded is not None and hasattr(loaded, "ValidationPolicy"):
        _ENGINE = loaded
        return _ENGINE
    import importlib.util
    import types

    if not _ENGINE_PATH.is_file():
        raise RuntimeError(f"缺少规则引擎 {_ENGINE_PATH}，拒绝在无引擎的情况下判通过")
    spec = importlib.util.spec_from_file_location("novel_validation", str(_ENGINE_PATH))
    module = importlib.util.module_from_spec(spec)
    # 必须先登记再执行：@dataclass 在 `from __future__ import annotations` 下要按
    # cls.__module__ 回查 sys.modules 解析字符串注解，没登记就 AttributeError。
    sys.modules[spec.name] = module
    flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True          # 不在运行包目录里留派生缓存
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)    # 半成品模块不许留在进程里
        raise
    finally:
        sys.dont_write_bytecode = flag
    if not hasattr(module, "collect_issues"):
        raise RuntimeError(f"规则引擎缺少 collect_issues()，拒绝继续: {_ENGINE_PATH}")
    _ENGINE = module
    return module


def policy(scene="ordinary", strength="balanced"):
    return engine().ValidationPolicy(scene, strength)


#: 入口默认策略：普通场景 + 平衡力度（小说场景由 Task 12 的 CLI 显式传入）。
DEFAULT_POLICY = policy("ordinary", "balanced")


def _universal_newlines(text: str) -> str:
    """与 ``Path.read_text`` 同一口径的通用换行归一。

    调用方（``file_commit._run_mechanical_validator``）用 ``write_text`` 落临时文件，
    Windows 上会把 ``\\n`` 翻译成 ``\\r\\n``；旧实现经 ``read_text`` 读回来时自动归一，
    规则比对的是内容而不是换行。改成读字节判 NO-OP 之后这一步必须显式保留，
    否则「只有换行不同」会被规则读成内容差异（数字换绑一类假阳性）。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate(before_path: Path, after_path: Path, chosen_policy=None):
    """三态契约保持不变：(status, issues, exit_code)。

    v1.2 Task 12（Task 10 移交的账）新增一个如实的判词：**两份文件字节完全相同**时报
    ``NO-OP`` 而不再是 ``PASS_MECHANICAL``，退出码仍是 0。引擎 ``novel_validation`` 对同一
    输入早就判 NO-OP（``_build_result(noop=True)`` → ``mechanical_verdict``），前端把它压成
    「机械检查通过」等于替用户说「这句被改过了」——设计方案 84 行与交付合同要求前后端
    对同一份字节给同一个结论。除这一条之外，判定路径与规则集完全没动。
    """
    try:
        before_bytes = Path(before_path).read_bytes()
        after_bytes = Path(after_path).read_bytes()
    except Exception as exc:
        return "ERROR", [f"文件读取错误: {exc}"], EXIT_ERROR
    if before_bytes == after_bytes:
        # 一个字节都没变：这是交付层的 NO-OP，不是「改写后通过保真检查」。
        return "NO-OP", [], EXIT_PASS
    try:
        before_text = _universal_newlines(before_bytes.decode("utf-8"))
        after_text = _universal_newlines(after_bytes.decode("utf-8"))
    except Exception as exc:
        return "ERROR", [f"文件读取错误: {exc}"], EXIT_ERROR

    nv = engine()
    try:
        payload = nv.ValidationInput(before_text, after_text,
                                     chosen_policy or DEFAULT_POLICY)
        issues = nv.collect_issues(payload)
    except Exception as exc:
        return "ERROR", [f"机械验证器无法完成判定: {exc}"], EXIT_ERROR

    # ERROR 级=事故（合同入参非法、引擎不可用），既不是通过也不是复核：
    # 直接把「没判成」如实报出去，绝不让它落到 PASS_MECHANICAL 那一档。
    errors = [item.message for item in issues if item.severity == "ERROR"]
    fail = [item.message for item in issues if item.severity == "FAIL"]
    review = [item.message for item in issues if item.severity == "REVIEW"]
    if errors:
        return "ERROR", errors, EXIT_ERROR
    if fail:
        return "FAIL", fail, EXIT_FAIL
    if review:
        return "REVIEW", review, EXIT_REVIEW
    return "PASS_MECHANICAL", [], EXIT_PASS


def sentence_polarity(sentence: str) -> str:
    """极性口径由引擎提供；这里保留同名出口，历史测试与文档仍指向本模块。"""
    return engine().sentence_polarity(sentence)


def main():
    if len(sys.argv) != 3:
        print("用法: python validate-rewrite.py BEFORE.md AFTER.md", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    before_file = Path(sys.argv[1])
    after_file = Path(sys.argv[2])
    if not before_file.exists() or not after_file.exists():
        print(f"错误: 目标文件不存在 ({before_file} 或 {after_file})", file=sys.stderr)
        sys.exit(EXIT_USAGE)

    status, issues, code = validate(before_file, after_file)
    if status == "NO-OP":
        print("NO-OP: 改写前后字节完全一致，没有任何改写发生（不是一份新产出）。")
        sys.exit(EXIT_PASS)
    if status == "PASS_MECHANICAL":
        print("PASS_MECHANICAL: 改写前后受覆盖的结构、保护片段与机械约束检查通过"
              "（注：机械项通过不代表因果与立场语义完全等价；最终发布仍要过 L2/L3）。")
        sys.exit(EXIT_PASS)
    if status == "FAIL":
        print("FAIL: 机械保真检查拦截到明确违规或漂移项：", file=sys.stderr)
        for err in issues:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(EXIT_FAIL)
    if status == "REVIEW":
        print("REVIEW: 检测到潜在事实关系变动或新增断言，无法机械判定，需进一步复核：",
              file=sys.stderr)
        for item in issues:
            print(f"  - {item}", file=sys.stderr)
        sys.exit(EXIT_REVIEW)
    sys.exit(code)


if __name__ == "__main__":
    main()
