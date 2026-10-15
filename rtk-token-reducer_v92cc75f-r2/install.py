# -*- coding: utf-8 -*-
"""把 RTK Token Reducer 的钩子登记进 Qoder 的 settings.json。

改的是配置文件，所以默认只出「影响清单 + 将要写入的内容 + 回滚命令」，
必须显式加 --apply 才落盘；落盘前自动留时间戳备份。

用法：
  python -X utf8 install.py --scope user                 # 看要写什么（不动手）
  python -X utf8 install.py --scope project --root .     # 项目级预演
  python -X utf8 install.py --scope user --apply         # 真写（需你明确同意）
  python -X utf8 install.py --scope user --remove        # 只摘掉本扩展的钩子条目
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import time

MARK = 'rtk_compress.py'
HOOK_DIR = os.path.dirname(os.path.abspath(__file__))


def entries(root_expr):
    """root_expr 为 '${QODER_PLUGIN_ROOT}'（插件安装态）或本目录绝对路径（免安装态）。"""
    def py(name):
        return u'"%s" -X utf8 "%s/%s"' % (u'python', root_expr.replace('\\', '/'), name)
    return {
        u'PostToolUse': [{
            u'matcher': u'Bash|run_in_terminal',
            u'hooks': [{u'type': u'command', u'command': py('rtk_compress.py'),
                        u'timeout': 25, u'async': False}]
        }],
        u'PreCompact': [{
            u'hooks': [{u'type': u'command', u'command': py('compact_stats.py'),
                        u'timeout': 15, u'async': False}]
        }]
    }


def target_path(scope, root):
    if scope == 'user':
        return os.path.join(os.path.expanduser('~'), '.qoder', 'settings.json')
    return os.path.join(os.path.abspath(root), '.qoder', 'settings.json')


def load(path):
    if not os.path.exists(path):
        return {}, u'\n'
    with io.open(path, encoding='utf-8', newline='') as f:
        raw = f.read()
    eol = u'\r\n' if u'\r\n' in raw else u'\n'
    return (json.loads(raw) if raw.strip() else {}), eol


def merge(cfg, entries):
    hooks = cfg.setdefault('hooks', {})
    added = []
    for event, blocks in entries.items():
        cur = hooks.setdefault(event, [])
        for block in blocks:
            for h in block['hooks']:
                exists = any(MARK in (x.get('command') or '') or
                             'compact_stats.py' in (x.get('command') or '')
                             for b in cur for x in b.get('hooks', []))
                if exists:
                    continue
                target = next((b for b in cur if b.get('matcher') == block.get('matcher')
                               or (event == 'PreCompact' and 'matcher' not in block)), None)
                if target is None:
                    cur.append(json.loads(json.dumps(block, ensure_ascii=False)))
                else:
                    target.setdefault('hooks', []).append(h)
                added.append((event, h['command']))
    return added


def strip(cfg):
    removed = 0
    for event, blocks in list(cfg.get('hooks', {}).items()):
        kept = []
        for block in blocks:
            hs = [h for h in block.get('hooks', [])
                  if not (MARK in (h.get('command') or '') or 'compact_stats.py' in (h.get('command') or ''))]
            removed += len(block.get('hooks', [])) - len(hs)
            if hs:
                b = dict(block)
                b['hooks'] = hs
                kept.append(b)
        if kept:
            cfg['hooks'][event] = kept
        else:
            cfg['hooks'].pop(event, None)
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scope', choices=['user', 'project'], default='user')
    ap.add_argument('--root', default='.')
    ap.add_argument('--installed', action='store_true',
                    help='按插件安装态写，用 ${QODER_PLUGIN_ROOT} 占位（需已把本目录装成插件）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--remove', action='store_true')
    a = ap.parse_args()

    path = target_path(a.scope, a.root)
    cfg, eol = load(path)
    root_expr = u'${QODER_PLUGIN_ROOT}/hooks' if a.installed else os.path.join(HOOK_DIR, 'hooks')
    ent = entries(root_expr)

    print(u'目标配置：%s（存在=%s）' % (path, os.path.exists(path)))
    if a.remove:
        n = strip(cfg)
        print(u'影响清单：从 hooks 里摘掉本扩展的 %d 条钩子条目，其余键一律不动。' % n)
        if not a.apply:
            print(u'回滚方案：cp "%s" "%s.restore" 先备份，再 --apply 执行删除。' % (path, path))
            return 0
        bak = path + '.bak-rtkreducer-' + time.strftime('%Y%m%d-%H%M%S')
        shutil.copy2(path, bak)
        write(path, cfg, eol)
        print(u'已摘除 %d 条；备份 %s' % (n, bak))
        return 0

    added = merge(cfg, ent)
    print(u'影响清单：将新增 %d 条钩子条目（PostToolUse/Bash 压缩、PreCompact 统计回报）；'
          u'不新建文件、不动其他键。' % len(added))
    for event, cmd in added:
        print(u'  %s -> %s' % (event, cmd))
    if not added:
        print(u'（该配置里已含本扩展钩子，无需重复登记）')
        return 0
    print(u'回滚方案：copy "%s.bak-<时间戳>" "%s"，或跑 `python -X utf8 install.py --scope %s --remove --apply`'
          % (path, path, a.scope))
    if not a.apply:
        print(u'预演模式：未写入。确认无误后加 --apply。')
        return 0
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    if os.path.exists(path):
        bak = path + '.bak-rtkreducer-' + time.strftime('%Y%m%d-%H%M%S')
        shutil.copy2(path, bak)
        print(u'已备份：%s' % bak)
    write(path, cfg, eol)
    print(u'已写入 %s；重启 Qoder 会话生效，跑一条长输出的 bash 命令后看是否出现 [rtk-reducer｜...] 提示行'
          % path)
    return 0


def write(path, cfg, eol):
    txt = json.dumps(cfg, ensure_ascii=False, indent=2) + u'\n'
    txt = re.sub(r'\r?\n', lambda m: eol, txt)
    with io.open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(txt)


if __name__ == '__main__':
    raise SystemExit(main())
