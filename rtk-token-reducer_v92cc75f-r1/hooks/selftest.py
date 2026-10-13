# -*- coding: utf-8 -*-
"""自检：用隔离的临时数据目录跑钩子子进程，逐条断言压缩/放行/回溯/统计行为。

跑法：python -X utf8 selftest.py     （全绿 exit 0；任一红 exit 1；rtk 缺失 exit 2 并说明）
"""
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, 'rtk_compress.py')
FAILS = []
N = [0]


def check(name, cond, detail=u''):
    N[0] += 1
    if cond:
        print(u'  PASS  %s' % name)
    else:
        print(u'  FAIL  %s ｜ %s' % (name, detail))
        FAILS.append(name)


def run_hook(payload_text, home, extra_env=None):
    env = dict(os.environ)
    env['RTK_REDUCER_HOME'] = home
    env['PYTHONIOENCODING'] = 'utf-8'
    if extra_env:
        env.update(extra_env)
    p = subprocess.run([sys.executable, '-X', 'utf8', HOOK], input=payload_text.encode('utf-8'),
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60,
                       cwd=HERE)
    return p.returncode, p.stdout.decode('utf-8', 'replace'), p.stderr.decode('utf-8', 'replace')


def hook_json(command, tool_response, event='PostToolUse'):
    return json.dumps({'session_id': 'selftest', 'hook_event_name': event, 'cwd': HERE,
                       'tool_name': 'Bash', 'tool_input': {'command': command},
                       'tool_response': tool_response}, ensure_ascii=False)


def main():
    sys.path.insert(0, HERE)
    import common
    home = tempfile.mkdtemp(prefix='rtkreducer_selftest_')
    os.environ['RTK_REDUCER_HOME'] = home
    print(u'数据目录（临时）：%s' % home)
    if not common.rtk_binary():
        print(u'FAIL：PATH 里没找到 rtk（本自检只查 PATH，不探测任何固定盘符），自检无法验证压缩行为。')
        return 2
    big = u'\n'.join(u'src/mod%03d.py:%d: 一条重复的构建日志 module %d status OK' % (i, i, i)
                     for i in range(1, 501))
    print(u'用例 1 长输出必须压缩且原文可回溯')
    rc, out, err = run_hook(hook_json(u'find . -name "*.py" -printf line\\n', big), home)
    check('exit 0', rc == 0, err)
    ok = out.strip().startswith('{')
    check('stdout 是钩子 JSON', ok, out[:120])
    payload = json.loads(out) if ok else {}
    new = (payload.get('hookSpecificOutput') or {}).get('updatedToolOutput') or ''
    check('含 rtk-reducer 提示行', u'rtk-reducer' in new, new[:160])
    check('含累计压缩统计', u'累计' in new, new[:200])
    check('输出确实变短', 0 < len(new.encode('utf-8')) < len(big.encode('utf-8')),
          u'%d -> %d' % (len(big), len(new)))
    spools = os.listdir(os.path.join(home, 'spool'))
    check('原文已落 spool', len(spools) == 1, str(spools))
    if spools:
        with io.open(os.path.join(home, 'spool', spools[0]), encoding='utf-8') as f:
            check('spool 内容逐字等于原文', f.read() == big, u'长度不符')
    check('stats.json 计数 1', common.load_stats().get('count') == 1,
          json.dumps(common.load_stats(), ensure_ascii=False))

    print(u'用例 2 短输出放行（不改写）')
    rc, out, err = run_hook(hook_json(u'find . -name x', u'很短的输出' * 3), home)
    check('exit 0 且无 stdout', rc == 0 and not out.strip(), out[:120])

    print(u'用例 3 保留清单命令永不压缩')
    rc, out, err = run_hook(hook_json(u'md5sum payload.bin', big), home)
    check('exit 0 且无 stdout', rc == 0 and not out.strip(), out[:120])

    print(u'用例 3b 行首 RTK_PLAIN=1 随手豁免看全文')
    rc, out, err = run_hook(hook_json(u'RTK_PLAIN=1 find . -name "*.py"', big), home)
    check('exit 0 且无 stdout', rc == 0 and not out.strip(), out[:120])

    print(u'用例 4 坏 stdin 必须安静放行')
    rc, out, err = run_hook(u'this is not json{{{', home)
    check('exit 0 且无 stdout', rc == 0 and not out.strip(), u'%s %s' % (rc, out[:120]))

    print(u'用例 5 dict 形态的 tool_response 保持 JSON 结构')
    rc, out, err = run_hook(hook_json(u'python -u build.py',
                                      {'stdout': big, 'stderr': u'', 'interrupted': False}), home)
    payload = json.loads(out) if out.strip().startswith('{') else {}
    new = (payload.get('hookSpecificOutput') or {}).get('updatedToolOutput') or ''
    inner = json.loads(new) if new.strip().startswith('{') else {}
    check('updatedToolOutput 仍是对象 JSON', bool(inner), new[:120])
    check('stdout 字段被压缩', 0 < len(inner.get('stdout', '')) < len(big), u'%d' % len(inner.get('stdout', '')))
    check('非文本字段保留', inner.get('interrupted') is False, json.dumps(inner, ensure_ascii=False)[:120])

    print(u'用例 5b 过滤器省不下字节时原样放行（不做假压缩）')
    rc, out, err = run_hook(hook_json(u'grep -rn foo .', big), home)
    check('exit 0 且无 stdout', rc == 0 and not out.strip(), out[:120])

    print(u'用例 6 RTK_REDUCER_OFF=1 整体停用')
    rc, out, err = run_hook(hook_json(u'find .', big), home, {'RTK_REDUCER_OFF': '1'})
    check('exit 0 且无 stdout', rc == 0 and not out.strip(), out[:120])

    print(u'用例 7 同形态累计到阈值自动落自学规则')
    for _ in range(4):
        run_hook(hook_json(u'find . -name "*.py"', big), home)
    rules = os.path.join(home, 'learned-rules.toml')
    exists = os.path.exists(rules)
    content = io.open(rules, encoding='utf-8').read() if exists else u''
    check('learned-rules.toml 已生成', exists, rules)
    check('规则含 match_command 与过滤器名', 'match_command' in content and 'find' in content, content[:200])

    print(u'用例 8 PreCompact 统计钩子出 JSON')
    p = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(HERE, 'compact_stats.py')],
                       input=hook_json(u'x', u'y').encode('utf-8'), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, env=dict(os.environ, RTK_REDUCER_HOME=home), timeout=30)
    txt = p.stdout.decode('utf-8', 'replace')
    check('exit 0', p.returncode == 0, txt[:120])
    check('回报含累计统计', u'累计' in txt, txt[:200])

    print(u'用例 10 高多样性清单不许折叠成语义摘要（压缩不能靠撒谎）')
    listing = u'\n'.join(u'-rw-r--r-- 1 u g %6d Sep 28 12:00 file_%03d.py' % (i * 37, i)
                         for i in range(1, 121))
    rc, out, err = run_hook(hook_json(u'ls -la *.py', listing), home)
    payload = json.loads(out) if out.strip().startswith('{') else {}
    new = (payload.get('hookSpecificOutput') or {}).get('updatedToolOutput') or ''
    check('清单也被压缩', u'rtk-reducer' in new and len(new.encode('utf-8')) < len(listing.encode('utf-8')),
          out[:160])
    kept = sum(1 for i in range(1, 121) if u'file_%03d.py' % i in new)
    check('清单仍以逐行文件名形式存在（不是零信息摘要）', kept >= 5, u'保住 %d 个文件名' % kept)
    check('没退化成 Log Summary', u'Log Summary' not in new, new[:200])

    print(u'用例 9 report.py 可跑')
    p = subprocess.run([sys.executable, '-X', 'utf8', os.path.join(HERE, 'report.py'), '--tail', '2'],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=dict(os.environ, RTK_REDUCER_HOME=home), timeout=30)
    check('exit 0 且有输出', p.returncode == 0 and u'数据目录' in p.stdout.decode('utf-8', 'replace'),
          p.stdout.decode('utf-8', 'replace')[:200])

    print(u'合计 %d 项断言，红 %d' % (N[0], len(FAILS)))
    for f in FAILS:
        print(u'  未通过：%s' % f)
    return 1 if FAILS else 0


if __name__ == '__main__':
    raise SystemExit(main())
