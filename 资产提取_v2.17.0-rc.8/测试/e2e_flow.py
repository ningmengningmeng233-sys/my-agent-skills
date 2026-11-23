# -*- coding: utf-8 -*-
"""资产提取v2 全流程触发测试：mock OpenAI 兼容 API + 完整跑通 8 步骤(含步骤 3.6 服饰设计衔接)。
验证 SKILL.md 文档中每个命令是否可正常触发（参数/前置/产物），并输出流程触发顺序。

v2.4：补步骤 3.6 服饰设计衔接（桥接 → 角色清单 → 服饰产物 → check_costumes 校验）；
SKILL 路径由 __file__ 推导（不再硬编码）。
用法: python e2e_flow.py                 （产物留在系统临时目录，可复跑）
     python e2e_flow.py --snapshot       （重建 测试/e2e/资产库 静态快照，供 CI 与 run_e2e_check 使用）
"""
import json
import os
import subprocess
import sys
import shutil
import tempfile

if __name__ == "__main__":  # CLI 直跑才改宿主 stdio；被导入时不得改（会吞测试框架收尾、并串编码）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_api import start_mock_server  # mock 产物单一来源（e2e_flow / run_release_migration 共用）

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if "--snapshot" in sys.argv:
    ROOT = os.path.join(SKILL, "测试", "e2e")
    if os.path.isdir(ROOT):
        shutil.rmtree(ROOT)
    os.makedirs(ROOT)
else:
    ROOT = tempfile.mkdtemp(prefix="asset_flow_")
LIB = os.path.join(ROOT, "资产库")
os.environ["ASSET_LIB_DIR"] = LIB
os.environ["OPENAI_API_KEY"] = "flow-test-key"
os.environ["SUMMARY_API_MODEL"] = "flow-test-model"
os.environ["ASSET_WORKERS"] = "8"

results = []


def step(name, rc, extra="", stdout_ok=None):
    """rc==0 且（可选）stdout 断言通过才算 OK——生成类脚本失败时 rc 仍为 0，
    必须核对 stdout 汇总行（「失败 N」）防假阳性。"""
    ok = rc == 0 and (stdout_ok if stdout_ok is not None else True)
    results.append((name, ok, rc if isinstance(rc, int) else 1))
    print(f"  {'OK ' if ok else 'FAIL'} {name}" + (f"  [{str(extra)[:900]}]" if extra else ""))


def _decode(raw):
    """子进程输出按 utf-8 试解，失败退回 gbk：本文件的合同就是**不得依赖**编码保护伞，
    所以两种编码都可能出现，一律解码失败再兜底，绝不静默吞异常。"""
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run(cmd_args, cwd=SKILL):
    r = subprocess.run([sys.executable] + cmd_args, capture_output=True, cwd=cwd,
                       env={**os.environ, "ASSET_LIB_DIR": LIB})
    out, err = _decode(r.stdout), _decode(r.stderr)
    _dump_log(cmd_args, r.returncode, out, err)
    return r.returncode, out, err


LOG_DIR = os.environ.get("ASSET_E2E_LOG_DIR")


def _dump_log(cmd_args, rc, out, err):
    """完整错误留存：默认关，设 ASSET_E2E_LOG_DIR 才按调用序列落全文（不改任何判定）。"""
    if not LOG_DIR:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    tag = os.path.basename(str(cmd_args[0])) or "step"
    seq = len(os.listdir(LOG_DIR)) + 1
    path = os.path.join(LOG_DIR, f"{seq:02d}-{tag}.log")
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(f"$ {' '.join(str(a) for a in cmd_args)}\nrc={rc}\n")
        fh.write("--- stdout ---\n" + out + "\n--- stderr ---\n" + err)


srv, _mock_url = start_mock_server()
os.environ["SUMMARY_API_URL"] = _mock_url
print(f"mock API: {os.environ['SUMMARY_API_URL']}")

# ============ 测试源文件（12 章，含人物/场景/道具特征词） ============
SRC = os.path.join(ROOT, "全流程测试.txt")
lines = []
for i in range(1, 13):
    lines.append(f"第{i}章 章节{i}")
    lines.append("林姑娘走进茶楼，掌柜在柜台后拨算盘，角落老者抚琴。")
    lines.append("琴声穿过天井，青瓷茶壶冒着热气，油纸伞靠在门边。")
    lines.append("船夫在江边码头整理旧书，屏风后传来脚步声。")
with open(SRC, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))

print("=" * 60)
print("全流程触发测试（mock API，含步骤 3.6 服饰设计）")
print("=" * 60)

# ============ 步骤 1：拆段 ============
print("\n[步骤 1] split_segments.py（摸底与拆段）")
rc, out, err = run(["scripts/split_segments.py", SRC, "--novel", "全流程测试", "--author", "流测",
                    "--lib", LIB, "--overlap", "1"])
print(out[-300:])
step("步骤1 拆段（--overlap 1）", rc, err[:100])

# ============ 步骤 1.5：全局登记表 ============
print("\n[步骤 1.5] scan_registry.py（候选遍 + 分层聚合）")
seg_dir = os.path.join(LIB, "原文段", "全流程测试-流测")
rc, out, err = run(["scripts/scan_registry.py", seg_dir, "--lib", LIB])
print(out[-400:])
step("步骤1.5 登记表生成", rc, err[:100])
reg_ok = os.path.exists(os.path.join(LIB, "全流程测试-流测-登记表.md"))
step("登记表产物存在", 0 if reg_ok else 1, "登记表.md")

# ============ 步骤 2：段级提取 ============
print("\n[步骤 2] extract_segments.py（段级提取，锚点注入）")
rc, out, err = run(["scripts/extract_segments.py", seg_dir, "--lib", LIB])
print(out[-400:])
step("步骤2 段级提取", rc, err[:100])

# ============ 步骤 3：聚合裁决 ============
print("\n[步骤 3] aggregate.py（预聚合 + 分块裁决 + 登记表对账）")
prod_dir = os.path.join(LIB, "段产物", "全流程测试-流测")
rc, out, err = run(["scripts/aggregate.py", prod_dir, seg_dir, "--lib", LIB])
print(out[-500:])
step("步骤3 聚合裁决", rc, err[:100])
final_ok = os.path.exists(os.path.join(LIB, "反推_最终.md"))
step("反推_最终.md 产物存在", 0 if final_ok else 1)
report_ok = os.path.exists(os.path.join(LIB, ".process", "登记表对账报告.md"))
step("登记表对账报告存在", 0 if report_ok else 1)

# ============ 步骤 3.5：对账闭环 ============
print("\n[步骤 3.5] scan_registry.py --include-claims（[新增]申报并入登记表）")
rc, out, err = run(["scripts/scan_registry.py", "--include-claims", os.path.join(LIB, ".process", "登记表对账报告.md"), "--lib", LIB])
print(out[-300:])
step("步骤3.5 对账闭环", rc, err[:100])

# ============ 步骤 3.6：服饰设计衔接（v2.4 新增） ============
print("\n[步骤 3.6] 服饰设计衔接（prepare_costume_input → 角色清单 → 校验）")
rc, out, err = run(["scripts/prepare_costume_input.py", os.path.join(LIB, "反推_最终.md"),
                    "--genre", "古风", "--lib", LIB])
print(out[-300:])
step("步骤3.6a 桥接（角色清单+剧种）", rc, err[:100])
clist = os.path.join(LIB, "服饰设计", "测试全流程小说", "角色清单.md")
clist_ok = os.path.exists(clist)
step("角色清单产物存在", 0 if clist_ok else 1, os.path.basename(clist) if clist_ok else "未找到")
if clist_ok:
    # mock 服饰设计产物（照服饰设计/输出/青云令 格式）+ check_costumes 校验
    cdir = os.path.dirname(clist)
    std = os.path.join(cdir, "服饰标准库.md")
    with open(std, "w", encoding="utf-8") as f:
        f.write("# 《测试全流程小说》服饰标准库（剧种：古装宫廷）\n## 配色板\n| 色名 | 色值 | 定位 |\n|---|---|---|\n| 玄青 | #1F2937 | 主色 |\n| 月白 | #F5F0E6 | 主色 |\n## 禁忌清单\n**剧种黑名单**：机甲、外骨骼、赛博霓虹、现代西装、领带、皮鞋、军装、迷彩\n**用户额外禁忌**：无\n")
    roles = [l.split("|")[1].strip() for l in open(clist, encoding="utf-8") if l.startswith("|") and "角色名" not in l and "---" not in l and l.count("|") >= 4]
    rfile = os.path.join(cdir, "角色服饰标准表.md")
    rows = []
    for name in roles:
        rows.append(f"## 角色 {name}\n- 基础信息: 6.5 头身\n- 全身穿搭: 长袍 主色 #1F2937, 腰带 次色 #F5F0E6\n- 生图提示词: 图一：脸部参考图——{name}正面，中性表情，均匀柔光，中性灰背景。图二：服装展示图——同一画面横排三格（正面、侧面、背面全身站姿），三格头部一律裁除不入画，完整展示长袍与腰带细节，中性灰背景，均匀柔光。\n")
    with open(rfile, "w", encoding="utf-8") as f:
        f.write("\n".join(rows))
    rc, out, err = run(["服饰设计/scripts/check_costumes.py", std, rfile])
    print(out[-300:])
    step("步骤3.6b 服饰校验（阻断清零）", rc, err[:100])

# ============ 步骤 0：参数持久化（模拟编排器开始界面确认，ADR-0079/0080） ============
_start_state = {
    "input_source": os.path.join(ROOT, "全流程测试.txt"),
    "genre": "古风",
    "style": "仙侠奇幻风",
    "emphasis_hash": "mock",
    "output_root": LIB,
    "last_confirmed_at": "2026-09-06T00:00:00",
}
with open(os.path.join(LIB, ".start-state.json"), "w", encoding="utf-8") as f:
    json.dump(_start_state, f, ensure_ascii=False, indent=2)
print("步骤 0：.start-state.json 已写入（画风=仙侠奇幻风）")

# ============ 步骤 4：描述补全 ============
print("\n[步骤 4] generate_prompts.py --enrich（描述补全，--yes 跳过画风确认）")
rc, out, err = run(["scripts/generate_prompts.py", "--enrich", os.path.join(LIB, "反推_最终.md"),
                    "--genre", "古风", "--lib", LIB, "--style", "仙侠奇幻风", "--yes"])
print(out[-300:])
step("步骤4 描述补全(失败数=0)", rc, err[:100], stdout_ok=("失败 0" in out))

# ============ 步骤 5：基础提示词 ============
print("\n[步骤 5] generate_prompts.py --base（基础提示词）")
rc, out, err = run(["scripts/generate_prompts.py", "--base", os.path.join(LIB, "反推_最终.md"),
                    "--genre", "古风", "--lib", LIB, "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"])
print(out[-300:])
step("步骤5 基础提示词(失败数=0)", rc, err[:100], stdout_ok=("失败 0" in out))

# ============ 步骤 6：衍生提示词 ============
print("\n[步骤 6] generate_prompts.py --init-schedule + --deriv（调度清单 + 衍生）")
rc, out, err = run(["scripts/generate_prompts.py", "--init-schedule", os.path.join(LIB, "反推_最终.md"),
                    "--genre", "古风", "--lib", LIB])
print(out[-200:])
step("步骤6a 调度清单生成", rc, err[:100])
rc, out, err = run(["scripts/generate_prompts.py", "--deriv", "--genre", "古风", "--lib", LIB,
                    "--style", "仙侠奇幻风", "--yes", "--view-mode", "full"])
print(out[-300:])
step("步骤6b 衍生提示词(失败数=0)", rc, err[:100], stdout_ok=("失败 0" in out))

# ============ 步骤 6.5：结算询问-裁头版补生成（模拟用户确认需要） ============
print("\n[步骤 6.5] generate_prompts.py --view-mode crop（结算询问确认后补生成裁头版）")
rc, out, err = run(["scripts/generate_prompts.py", "--base", os.path.join(LIB, "反推_最终.md"),
                    "--genre", "古风", "--lib", LIB, "--style", "仙侠奇幻风", "--yes", "--view-mode", "crop"])
print(out[-300:])
step("步骤6.5a 裁头版基础提示词(失败数=0)", rc, err[:100], stdout_ok=("失败 0" in out))
rc, out, err = run(["scripts/generate_prompts.py", "--deriv", "--genre", "古风", "--lib", LIB,
                    "--style", "仙侠奇幻风", "--yes", "--view-mode", "crop"])
print(out[-300:])
step("步骤6.5b 裁头版衍生提示词(失败数=0)", rc, err[:100], stdout_ok=("失败 0" in out))

# ============ 步骤 7：完整性检查 ============
print("\n[步骤 7] check_schedule.py（四项完整性检查 + 画风锁定）")
rc, out, err = run(["scripts/check_schedule.py", os.path.join(LIB, "反推_最终.md"),
                    "--lib", LIB, "--style", "仙侠奇幻风"])
print(out[-500:])
step("步骤7 完整性检查", rc, err[:100])

# ============ 步骤 7.5：验证收口与交付清单真实签发（ADR-0084/R2a） ============
print("\n[步骤 7.5] maintenance.py verify（全绿签发交付清单）")
rc, out, err = run(["scripts/maintenance.py", "verify", "--lib", LIB, "--style", "仙侠奇幻风"])
print(out[-400:])
mf_path = os.path.join(LIB, "交付清单.json")
manifest_ok = False
detail = f"verify rc={rc}"
if rc == 0 and os.path.isfile(mf_path):
    try:
        with open(mf_path, encoding="utf-8") as f:
            mf = json.load(f)
        names = {c.get("name") for c in mf.get("checks", []) if isinstance(c, dict)}
        # 工单 13：签发契约扩到 9 项，这里按**完整契约**断言而不是子集，
        # 否则清单少了新检查项也照样判「全过」，等于把扩展门白加。
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from scripts.engine.pipeline_state import REQUIRED_DELIVERY_CHECKS

        missing = set(REQUIRED_DELIVERY_CHECKS) - names
        manifest_ok = (mf.get("status") == "DELIVERED" and not missing
                       and all(isinstance(c, dict) and c.get("rc") == 0 for c in mf.get("checks", [])))
        detail = (f"status={mf.get('status')} checks={len(names)}/{len(REQUIRED_DELIVERY_CHECKS)}"
                  + (f" 缺: {sorted(missing)}" if missing else ""))
    except (OSError, ValueError) as e:
        detail = f"清单解析失败: {e}"
step("步骤7.5 verify签发交付清单(DELIVERED+签发契约检查全过)", 0 if manifest_ok else 1, detail)

# ============ 步骤 7.6：机器可读状态（真实 status --json，ADR-0084/R2b） ============
print("\n[步骤 7.6] maintenance.py status --json（必须 DELIVERED/DELIVERY_VALID）")
rc, out, err = run(["scripts/maintenance.py", "status", "--lib", LIB, "--json"])
payload = None
state_ok = False
detail = "JSON 解析失败"
try:
    payload = json.loads(out)
    state_ok = payload.get("pipeline_state") == "DELIVERED" and payload.get("reason_code") == "DELIVERY_VALID"
    detail = f"{payload.get('pipeline_state')}/{payload.get('reason_code')}"
except ValueError:
    pass
step("步骤7.6 status --json=DELIVERED/DELIVERY_VALID", rc if state_ok else 1, detail)

# ============ 汇总 ============
print("\n" + "=" * 60)
print("全流程触发结果汇总")
print("=" * 60)
for name, ok, rc in results:
    print(f"  {'✅' if ok else '❌'} {name}")
n_ok = sum(1 for _, ok, _ in results if ok)
print(f"\n{len(results)} 步中 {n_ok} 步成功")
print(f"产物目录: {LIB}")

# 产物清单
print("\n产物清单:")
for root_dir, dirs, files in os.walk(LIB):
    for f in sorted(files):
        p = os.path.join(root_dir, f)
        rel = os.path.relpath(p, LIB)
        print(f"  {rel}")
sys.exit(0 if n_ok == len(results) else 1)
