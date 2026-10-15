# -*- coding: utf-8 -*-
"""大纲预审门 O1-O6：改编的根必须先合格。

12 道剧本门全部以大纲为对账基准（G8/G9/时长/爽点认领），大纲本身烂了后面全是白搭——
v1.1.0 起大纲在进入生成/汇编前必须先过这六道确定性预审门。纯 Python 零依赖，
可独立运行：python -m dramacraft.outline_lint <outline.json>
"""
import hashlib
import json
from typing import Any, Dict, List


def compute_outline_hash(data: Dict[str, Any]) -> str:
    """计算大纲规范化 MD5 哈希（前 12 位）。
    确保键序字典序、整型化 targetSeconds，同内容异序哈希必定相同。
    """
    def _normalize(obj: Any) -> Any:
        if isinstance(obj, dict):
            new_dict = {}
            for k in sorted(obj.keys()):
                val = obj[k]
                if k == "targetSeconds" and isinstance(val, (int, float)):
                    new_dict[k] = int(val)
                else:
                    new_dict[k] = _normalize(val)
            return new_dict
        elif isinstance(obj, list):
            return [_normalize(x) for x in obj]
        return obj

    norm = _normalize(data)
    encoded = json.dumps(norm, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.md5(encoded).hexdigest()[:12]


def lint_outline(data: Dict[str, Any]) -> List[str]:
    """返回违规清单，空列表 = 六门全过"""
    bad: List[str] = []

    # O1 标题
    if not str(data.get("title", "")).strip():
        bad.append("[O1] title 缺失：剧本命名与下游资产全靠它")

    # O2 人物登记表
    chars = data.get("characters", [])
    if not isinstance(chars, list) or not chars:
        bad.append("[O2] characters 为空：台词说话人无法映射人名，G3/G8 无从对账")
    seen_ids = set()
    for i, c in enumerate(chars if isinstance(chars, list) else []):
        cid = str(c.get("id", "")).strip()
        if not cid:
            bad.append(f"[O2] 第 {i + 1} 个角色缺 id")
            continue
        if cid in seen_ids:
            bad.append(f"[O2] 角色 id 重复：{cid}")
        seen_ids.add(cid)
        for field in ("name", "desire", "obstacle", "voiceStyle"):
            if not str(c.get(field, "")).strip():
                bad.append(f"[O2] 角色 {cid} 缺 {field}（人设四字段不齐=纸片人预警）")

    # O3 集号与全剧统一时长
    eps = data.get("episodes", [])
    if not isinstance(eps, list) or not eps:
        bad.append("[O3] episodes 为空：没有集就没戏")
    ep_nums = []
    target_by_ep = {}
    for ep in eps if isinstance(eps, list) else []:
        n = ep.get("ep")
        if not isinstance(n, int) or n < 1:
            bad.append(f"[O3] 存在非法集号: {n}")
            continue
        ep_nums.append(n)
        t = ep.get("targetSeconds")
        if not isinstance(t, (int, float)) or t <= 0:
            bad.append(f"[O3] 第 {n} 集缺合法 targetSeconds")
        else:
            target_by_ep[n] = int(t)
    if len(ep_nums) != len(set(ep_nums)):
        dup = sorted({n for n in ep_nums if ep_nums.count(n) > 1})
        bad.append(f"[O3] 集号重复: {dup}")
    if len(set(target_by_ep.values())) > 1:
        detail = "｜".join(f"第{k}集:{v}s" for k, v in sorted(target_by_ep.items()))
        bad.append(f"[O3] 目标时长必须全剧统一，逐集实为 {detail}——乱填是反塞凑门的指纹")

    # O4 逐集钩子与悬念落在纸面
    for ep in eps if isinstance(eps, list) else []:
        for field in ("hook", "cliff"):
            if not str(ep.get(field, "")).strip():
                bad.append(f"[O4] 第 {ep.get('ep')} 集缺 {field}")

    # O5 爽点节拍归属合法
    valid_eps = set(ep_nums)
    beat_types_by_ep: Dict[int, set] = {}
    beats = data.get("beats", [])
    for i, b in enumerate(beats if isinstance(beats, list) else []):
        bt = str(b.get("type", "")).strip()
        be = b.get("episode")
        if not bt:
            bad.append(f"[O5] 第 {i + 1} 条 beat 缺 type")
            continue
        if be not in valid_eps:
            bad.append(f"[O5] beat「{bt}」挂在不存在的第 {be} 集（孤儿节拍，剧本永远不会认领它）")
            continue
        beat_types_by_ep.setdefault(be, set()).add(bt)

    # O6 认领双向对账：不漏认领、不虚报
    for ep in eps if isinstance(eps, list) else []:
        n = ep.get("ep")
        if not isinstance(n, int):
            continue
        claimed = set(str(x) for x in ep.get("beatsClaimed", []) if str(x).strip())
        due = beat_types_by_ep.get(n, set())
        for t in sorted(due - claimed):
            bad.append(f"[O6] 第 {n} 集漏认领大纲爽点「{t}」")
        for t in sorted(claimed - due):
            bad.append(f"[O6] 第 {n} 集虚报了大纲没有的爽点「{t}」")

    return bad


def main():
    import sys

    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print("用法: python -m dramacraft.outline_lint <outline.json>")
        sys.exit(0)
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
    violations = lint_outline(data)
    if violations:
        print("\n================== 大纲预审门：未通过 ==================")
        for v in violations:
            print(f"✗ {v}")
        print("======================================================")
        sys.exit(1)
    print("✓ 大纲预审门 O1-O6 全部通过，可以进剧本生产。")


if __name__ == "__main__":
    main()
