# -*- coding: utf-8 -*-
"""压缩统计命令行：python report.py [--json] [--tail N] [--rtk]

--json   机器可读输出
--tail N 顺带打印最近 N 条 ledger
--rtk    末尾附上 rtk 自身 `rtk gain` 的口径（两边数字不同源，互为对照）
"""
import argparse
import io
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--tail', type=int, default=0)
    ap.add_argument('--rtk', action='store_true')
    a = ap.parse_args()
    st = common.load_stats()
    saved = st.get('before', 0) - st.get('after', 0)
    ratio = (100.0 * saved / st['before']) if st.get('before') else 0.0
    top = sorted((st.get('patterns') or {}).items(), key=lambda kv: -kv[1])[:10]
    if a.json:
        print(json.dumps({'stats': st, 'saved': saved, 'ratio_pct': round(ratio, 2),
                          'top_patterns': top, 'home': common.home()}, ensure_ascii=False, indent=1))
    else:
        print(common.main_stats_line(st))
        print(u'数据目录：%s ｜ 平均单次省 %s'
              % (common.home(), common.fmt_bytes(saved // max(st.get('count', 1), 1))))
        print(u'高频压缩形态（次数 >= %d 会自动落进 learned-rules.toml）：' % common.RULE_THRESHOLD)
        for key, n in top:
            print(u'  %-40s %d 次' % (key, n))
        if not top:
            print(u'  （暂无记录）')
    if a.tail:
        led = common.ledger_path()
        rows = []
        if os.path.exists(led):
            with io.open(led, encoding='utf-8', errors='replace') as f:
                rows = [l for l in f.read().splitlines() if l.strip()][-a.tail:]
        print(u'最近 %d 条：' % len(rows))
        for l in rows:
            d = json.loads(l)
            if 'before' in d:
                print(u'  %s ｜ %s ｜ %s -> %s ｜ %s ｜ 原文 %s'
                      % (d.get('ts'), (d.get('command') or '')[:48], common.fmt_bytes(d['before']),
                         common.fmt_bytes(d['after']), d.get('filter'), d.get('spool')))
            else:
                print(u'  %s ｜ %s ｜ 放行：%s' % (d.get('ts'), (d.get('command') or '')[:48], d.get('why')))
    if a.rtk:
        rtk = common.rtk_binary()
        if rtk:
            p = subprocess.run([rtk, 'gain'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=30)
            print(u'--- rtk gain（rtk 自身口径）---')
            print(p.stdout.decode('utf-8', 'replace')[:1200])
        else:
            print(u'未找到 rtk 可执行文件')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
