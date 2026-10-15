# -*- coding: utf-8 -*-
"""RTK Token Reducer 的公共内核：定位 rtk、选过滤器、记账、学规则。

约定：本模块任何函数都不抛异常到钩子外层之外的一层以上——调用方统一 try 住并原样放行。
数据目录默认当前用户目录 ~/.qoder/rtk-reducer（便携合同：不探测任何作者盘符），
环境变量 RTK_REDUCER_HOME 显式设置时优先。rtk 可执行文件只从 PATH 查找；
找不到时钩子原样放行原文，不压缩、不丢数据。
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time

MIN_CHARS = int(os.environ.get('RTK_REDUCER_MIN', '700'))
RULE_THRESHOLD = int(os.environ.get('RTK_REDUCER_RULE_N', '3'))
RTK_TIMEOUT = 15


def home():
    h = os.environ.get('RTK_REDUCER_HOME') or os.path.join(
        os.path.expanduser('~'), '.qoder', 'rtk-reducer')
    for sub in ('', 'spool'):
        d = os.path.join(h, sub) if sub else h
        if not os.path.isdir(d):
            os.makedirs(d)
    return h


def rules_path():
    return os.path.join(home(), 'learned-rules.toml')


def ledger_path():
    return os.path.join(home(), 'ledger.jsonl')


def rtk_binary():
    return shutil.which('rtk') or shutil.which('rtk.exe')


# 命令形态 -> rtk 内置过滤器名（过滤器名取自 `rtk pipe -f` 实测可用清单）
FILTER_RULES = [
    (r'^\s*(?:rtk\s+)?(?:git\s+(?:log|whatchanged|shortlog))\b', 'git-log'),
    (r'^\s*(?:rtk\s+)?git\s+diff\b', 'git-diff'),
    (r'^\s*(?:rtk\s+)?git\s+status\b', 'git-status'),
    (r'^\s*(?:rtk\s+)?(?:find|fd)\b', 'find'),
    (r'^\s*(?:rtk\s+)?(?:ls|dir|tree|ll)\b', 'find'),
    (r'^\s*(?:rtk\s+)?(?:grep|rg|egrep)\b', 'grep'),
    (r'^\s*(?:rtk\s+)?(?:pytest|py\.test|python\s+-m\s+pytest)\b', 'pytest'),
    (r'^\s*(?:rtk\s+)?(?:tsc|npx\s+tsc)\b', 'tsc'),
    (r'^\s*(?:rtk\s+)?(?:vitest|jest)\b', 'vitest'),
    (r'^\s*(?:rtk\s+)?cargo\s+test\b', 'cargo-test'),
    (r'^\s*(?:rtk\s+)?go\s+test\b', 'go-test'),
    (r'^\s*(?:rtk\s+)?mypy\b', 'mypy'),
    (r'^\s*(?:rtk\s+)?ruff\s+check\b', 'ruff-check'),
]

# 输出必须逐字进上下文的命令：永不压缩（引用行号 / 补丁 / 摘要 / 编码内容）
# 前缀 RTK_PLAIN=1 是随手豁免：某条命令想看全文时在行首加它。
KEEP_RE = re.compile(
    r'^\s*(?:RTK_PLAIN=1\s|git\s+(?:apply|checkout|cherry-pick|format-patch|show|am)|'
    r'base64|xxd|od\s|md5sum|sha1sum|sha256sum|certutil|openssl|'
    r'rtk\s+(?:recall|gain|config|pipe)|sed\s+-n\s+\d|awk\s+.*print)', re.I)

# 追加在管道里的第二段命令也算数（如 find . | head）；用整条命令再判一次
KEEP_ANY_RE = re.compile(
    r'\b(?:md5sum|sha256sum|base64 -d|xxd|git apply|rtk recall)\b', re.I)


def pick_filter(command, body=u''):
    """命令形态决定首选过滤器；形态不明时按行多样性兜底。

    实测（rtk 0.49）：`log` 过滤器会把「每行都是不同数据」的输出（如 ls 清单）折叠成
    一句 "Log Summary"，省了 98% 但把内容全丢了。所以高多样性输出只准走保行的 `find`，
    低多样性（重复日志）才交给 `log` 折叠——压缩不能靠撒谎。
    """
    for pat, name in FILTER_RULES:
        if re.search(pat, command or '', re.I):
            return name
    lines = [l for l in (body or u'').splitlines() if l.strip()]
    if len(lines) >= 8 and float(len(set(lines))) / len(lines) >= 0.6:
        return 'find'
    return 'log'


def must_keep(command):
    cmd = command or ''
    return bool(KEEP_RE.search(cmd) or KEEP_ANY_RE.search(cmd))


# dict 形态 tool_response 里常见的退出码字段名（不同 harness 命名不一）。
# 宁可多认不错杀：误判只会导致一次放行不压缩，漏判则可能把报错现场改得面目全非。
_EXIT_KEYS = ('exit_code', 'exitCode', 'exitcode', 'return_code', 'returncode',
              'code', 'status')

# 字符串形态 status 里视为成功的取值，其余非空字符串一律按失败旁路。
_OK_STATUS = (u'', u'0', u'ok', u'success', u'succeeded', u'pass', u'passed')


def response_failed(tool_response):
    """dict 形态 tool_response 是否携带失败信号（RT-01）。

    非零退出码、中断/错误标记都算失败：这类输出必须原样透传，
    钩子不得改写，避免把报错现场压缩丢了，也绝不吞真实退出码。
    非 dict（纯字符串输出）无处得知退出码，一律按成功处理。
    """
    if not isinstance(tool_response, dict):
        return False
    for key in _EXIT_KEYS:
        if key not in tool_response:
            continue
        v = tool_response[key]
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            if v != 0:
                return True
        elif isinstance(v, str):
            if v.strip().lower() not in _OK_STATUS:
                return True
    for key in ('interrupted', 'failed', 'error', 'timed_out', 'timeout'):
        if tool_response.get(key):
            return True
    return False


def is_binary_text(text):
    """输出正文是否二进制/非文本（RT-02）：含 NUL 或控制字符占比过高。

    二进制输出一旦喂给压缩器再改写，模型看到的就是面目全非的版本；
    返回 True 时调用方必须 bypass，原样放行。ANSI 颜色转义（ESC）是合法文本，不计入。
    """
    if not text:
        return False
    if u'\x00' in text:
        return True
    sample = text[:4096]
    bad = sum(1 for ch in sample
              if (ord(ch) < 32 and ch not in u'\t\n\r\x1b') or ord(ch) == 127)
    if not bad:
        return False
    return bad > 16 or bad / float(len(sample)) > 0.10


def rtk_timeout():
    """rtk 子进程超时秒数（RT-03）：RTK_REDUCER_TIMEOUT 可覆盖，非法值回落默认。"""
    try:
        v = int(os.environ.get('RTK_REDUCER_TIMEOUT', str(RTK_TIMEOUT)))
    except (TypeError, ValueError):
        return RTK_TIMEOUT
    return v if v > 0 else RTK_TIMEOUT


def rtk_pipe(text, filt):
    """把文本喂给 rtk pipe，返回压缩后的文本；失败返回 None（调用方原样放行）。

    RT-03：子进程超时（默认 15 秒，RTK_REDUCER_TIMEOUT 可配）同样返回 None，
    挂死的 rtk 拖不住钩子，更丢不了原文。
    """
    rtk = rtk_binary()
    if not rtk:
        return None
    try:
        p = subprocess.run([rtk, 'pipe', '-f', filt],
                           input=text.encode('utf-8', 'replace'),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=rtk_timeout())
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    if p.returncode != 0 or p.stdout is None:
        return None
    out = p.stdout.decode('utf-8', 'replace')
    return out if len(out.strip()) else None


def spool(text, tag):
    """压缩前先把原文逐字落盘，保证「压缩=可回溯」而非「压缩=丢数据」。"""
    name = '%s-%s.txt' % (time.strftime('%Y%m%d-%H%M%S'), tag)
    path = os.path.join(home(), 'spool', name)
    with io.open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    return path


def _read_json(path, default):
    try:
        with io.open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def load_stats():
    return _read_json(os.path.join(home(), 'stats.json'),
                      {'count': 0, 'before': 0, 'after': 0, 'skipped': 0, 'patterns': {}})


def save_stats(st):
    with io.open(os.path.join(home(), 'stats.json'), 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def pattern_key(command, filt):
    toks = re.split(r'\s+', (command or '').strip())
    head = ' '.join(toks[:2]).lower() if toks else '?'
    return '%s|%s' % (filt, head)


def record(entry, stats_update):
    with io.open(ledger_path(), 'a', encoding='utf-8', newline='') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')
    st = load_stats()
    if stats_update:
        kind, key = stats_update
        if kind == 'compressed':
            st['count'] = st.get('count', 0) + 1
            st['before'] = st.get('before', 0) + entry['before']
            st['after'] = st.get('after', 0) + entry['after']
            pats = st.setdefault('patterns', {})
            pats[key] = pats.get(key, 0) + 1
        else:
            st['skipped'] = st.get('skipped', 0) + 1
    save_stats(st)
    return st


def learn_rules(st):
    """某个命令形态被压缩过 RULE_THRESHOLD 次且尚未落规则 -> 写进 learned-rules.toml。

    rtk 0.49 的 `rtk gain` 没有 --save-rule 子命令（实测其 help 无此项），所以规则由本扩展
    自己按 rtk 官方 filters.toml 模板格式生成，用户把该文件内容并进项目 .rtk/filters.toml
    或 %APPDATA%\\rtk\\filters.toml 后，rtk 就能原生压缩这类命令，不再依赖钩子。
    """
    path = rules_path()
    existing = ''
    if os.path.exists(path):
        with io.open(path, encoding='utf-8') as f:
            existing = f.read()
    fresh = []
    for key, n in (st.get('patterns') or {}).items():
        if n < RULE_THRESHOLD or '[%s]' % key in existing:
            continue
        filt, head = key.split('|', 1)
        fresh.append(u'[filters."%s"]\ndescription = "RTK Reducer 自学规则：形态 %s 已压缩 %d 次，改由 rtk 原生过滤"\n'
                     u'match_command = "^%s"\nstrip_ansi = true\nmax_lines = 40\n'
                     % (key, head, n, re.escape(head).replace('\\ ', r'\s+')))
    if fresh:
        with io.open(path, 'a', encoding='utf-8', newline='') as f:
            f.write(u'\n# ---- %s ----\n' % time.strftime('%Y-%m-%d %H:%M'))
            f.write(u'\n'.join(fresh) + u'\n')
    return len(fresh)


def fmt_bytes(n):
    if n < 1024:
        return u'%dB' % n
    if n < 1024 ** 2:
        return u'%.1fKB' % (n / 1024.0)
    return u'%.2fMB' % (n / 1024.0 ** 2)


def main_stats_line(st):
    saved = st.get('before', 0) - st.get('after', 0)
    ratio = (100.0 * saved / st['before']) if st.get('before') else 0.0
    return (u'rtk-reducer 累计：压缩 %d 次，%s -> %s（省 %s，%.1f%%）；放行未压缩 %d 次'
            % (st.get('count', 0), fmt_bytes(st.get('before', 0)), fmt_bytes(st.get('after', 0)),
               fmt_bytes(saved), ratio, st.get('skipped', 0)))


if __name__ == '__main__':
    st = load_stats()
    print(main_stats_line(st))
    print(u'数据目录：%s' % home())
    print(u'可用 rtk：%s' % (rtk_binary() or '未找到，钩子将原样放行'))
    print(u'示例过滤器选择：find . -> %s ｜ git log -> %s ｜ python x.py -> %s'
          % (pick_filter('find . -name *.py'), pick_filter('git log --oneline'), pick_filter('python -u a.py')))
    print(u'保留清单命中：git diff -> %s ｜ md5sum -> %s ｜ grep foo -> %s'
          % (must_keep('git diff a b'), must_keep('md5sum f'), must_keep('grep -rn foo .')))
    sys.exit(0)
