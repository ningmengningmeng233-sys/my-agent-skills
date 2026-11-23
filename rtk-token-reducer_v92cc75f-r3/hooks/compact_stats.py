# -*- coding: utf-8 -*-
"""PreCompact 钩子：上下文将要被压缩时，把 rtk-reducer 的压缩统计报给模型与用户。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


def main():
    io.open(0, encoding='utf-8', errors='replace').read()
    st = common.load_stats()
    line = common.main_stats_line(st)
    n_rules = 0
    path = common.rules_path()
    if os.path.exists(path):
        with io.open(path, encoding='utf-8') as f:
            n_rules = f.read().count('[filters.')
    line += u'｜自学规则 %d 条（%s）' % (n_rules, path)
    sys.stdout.write(json.dumps({
        'hookSpecificOutput': {'hookEventName': 'PreCompact', 'additionalContext': line},
        'systemMessage': line
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write('rtk-reducer PreCompact 钩子异常：%s\n' % exc)
        raise SystemExit(0)
