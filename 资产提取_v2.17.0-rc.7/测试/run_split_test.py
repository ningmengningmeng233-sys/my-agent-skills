# -*- coding: utf-8 -*-
"""拆段层回归测试（离线，不调 LLM）——固化 v2.2 修复，防回归。

覆盖：
1. cn2ar 千/万级与混合格式（第一千二百三十章 → 1230）
2. CHAPTER_RE 无编号词前瞻约束（「番外内容,继续写正文」不误判）
3. 端到端拆段：千章 + 番外场景 → 单段、段范围正确
4. 幂等内容比对：同内容重跑跳过（exit 0）
5. 同名内容不同 → 冲突报错（exit 1），--force 覆盖
6. GBK 编码源文件回退链

用法: python run_split_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPLIT = os.path.join(SKILL, "scripts", "split_segments.py")

PASS = 0


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        sys.exit(1)


def run_split(src, lib, *extra):
    r = subprocess.run(
        [sys.executable, SPLIT, src, "--novel", "测试", "--author", "回归", "--lib", lib, *extra],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    return r.returncode, r.stdout


def main():
    tmp = os.path.join(tempfile.gettempdir(), "split-reg-测试")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    src = os.path.join(tmp, "千章小说.txt")
    lib = os.path.join(tmp, "lib")

    # ---- 1. cn2ar 千/万级（import 脚本模块） ----
    sys.path.insert(0, os.path.join(SKILL, "scripts"))
    from split_segments import cn2ar, CHAPTER_RE, range_label
    for s, exp in (("一千二百三十", 1230), ("一万零五", 10005), ("三万二千", 32000),
                   ("十万", 100000), ("一千零821", 1821), ("一百零三", 103),
                   ("二〇二四", 2024)):
        check(f"cn2ar({s}) = {exp}", cn2ar(s) == exp, f"got {cn2ar(s)}")
    check("倒挂标签交换(第123章~第5章→第5章~第123章)", range_label("第123章", "第5章") == "第5章~第123章")
    check("正常标签不变", range_label("第1章", "第5章") == "第1章~第5章")
    check("CHAPTER_RE 匹配「第一千二百三十章」", bool(CHAPTER_RE.match("第一千二百三十章")))
    check("CHAPTER_RE 不匹配「番外内容,继续写正文」", not CHAPTER_RE.match("番外内容,继续写正文"))
    check("CHAPTER_RE 匹配独立「番外」", bool(CHAPTER_RE.match("番外")))
    check("CHAPTER_RE 不误判行首数字列表", not CHAPTER_RE.match("1. 他说：要点茶。"))
    check("CHAPTER_RE 保留带编号章节标记", bool(CHAPTER_RE.match("1. 第三章 风起")))

    # ---- 2. 端到端拆段（千章 + 番外误判场景） ----
    lines = ["第一章 开篇", "正文一。", "", "番外内容,继续写正文", "",
             "第一千二百三十章 巅峰", "正文二。", "", "第一千二百三十一章 落幕", "正文三。"]
    with open(src, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    rc, out = run_split(src, lib)
    check("拆段 exit 0", rc == 0, out[-300:])
    seg_dir = os.path.join(lib, "原文段", "测试-回归")
    segs = sorted(os.listdir(seg_dir))
    check("千章识别:单段且范围正确", segs == ["测试-回归-第1章~第1231章-001.md"], str(segs))

    # ---- 3. 幂等:同内容重跑跳过 ----
    rc, out = run_split(src, lib)
    check("同内容重跑 exit 0 且跳过", rc == 0 and "内容一致" in out, out[-200:])

    # ---- 4. 冲突:中间内容修改 → exit 1 ----
    with open(src, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).replace("正文二。", "正文二被修改。"))
    rc, out = run_split(src, lib)
    check("内容变更 → 冲突 exit 1", rc == 1 and "内容不同" in out, out[-200:])
    rc, out = run_split(src, lib, "--force")
    check("--force 覆盖 exit 0", rc == 0, out[-200:])

    # ---- 5. GBK 编码回退链 ----
    gbk_src = os.path.join(tmp, "GBK源.txt")
    with open(gbk_src, "w", encoding="gbk") as f:
        f.write("第一章 相遇\n窗外雨声。\n\n第二章 别离\n她走了。\n")
    rc, out = run_split(gbk_src, lib)
    check("GBK 源文件拆段 exit 0", rc == 0, out[-300:])
    gbk_dir = os.path.join(lib, "原文段", "测试-回归")
    check("GBK 段文件生成", any("第1章~第2章" in s for s in os.listdir(gbk_dir)))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n✅ 拆段回归测试全部通过（{PASS} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
