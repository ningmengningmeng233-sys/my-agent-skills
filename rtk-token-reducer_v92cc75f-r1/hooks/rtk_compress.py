# -*- coding: utf-8 -*-
"""PostToolUse 钩子：bash 输出进入模型上下文之前，先用 rtk 压一遍。

流程：stdin 收钩子 JSON -> 取 tool_response 文本 -> 原文逐字进 spool -> rtk pipe 压缩
     -> 只当真省下字节才改写（updatedToolOutput），并附一行压缩统计与回溯路径。
失败哲学：钩子任何一步出错都安静放行（exit 0、不写 stdout），绝不让压缩功能挡住命令。
开关：RTK_REDUCER_OFF=1 整体停用；RTK_REDUCER_MIN=<字节数> 调短输出阈值；
RTK_REDUCER_TIMEOUT=<秒> 调压缩子进程超时（默认 15）。
旁路：非零退出码的失败输出、二进制/非文本输出一律跳过压缩，原样放行。
"""
import hashlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common


def response_text(tool_response):
    """返回 (可压缩文本, 重建函数)。tool_response 可能是字符串，也可能是 {stdout,stderr,...}。"""
    if isinstance(tool_response, str):
        return tool_response, lambda new: new
    if isinstance(tool_response, dict):
        parts = []
        for key in ('stdout', 'stderr', 'output', 'text'):
            v = tool_response.get(key)
            if isinstance(v, str) and v:
                parts.append((key, v))
        if not parts:
            return '', lambda new: new
        body = u'\n'.join(v for _, v in parts)

        def rebuild(new_body):
            out = dict(tool_response)
            out[parts[0][0]] = new_body
            for key, _ in parts[1:]:
                out[key] = u''
            return json.dumps(out, ensure_ascii=False)
        return body, rebuild
    return '', lambda new: new


def emit(text):
    sys.stdout.write(json.dumps({
        'hookSpecificOutput': {'hookEventName': 'PostToolUse', 'updatedToolOutput': text}
    }, ensure_ascii=False))
    sys.stdout.flush()


def main():
    if os.environ.get('RTK_REDUCER_OFF'):
        return 0
    raw = io.open(0, encoding='utf-8', errors='replace').read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if payload.get('hook_event_name') not in (None, 'PostToolUse'):
        return 0
    command = (payload.get('tool_input') or {}).get('command') or ''
    ts = time.strftime('%Y-%m-%dT%H:%M:%S')
    if common.must_keep(command):
        common.record({'ts': ts, 'command': command[:200], 'why': 'keep-list'}, None)
        return 0
    body, rebuild = response_text(payload.get('tool_response'))
    if common.response_failed(payload.get('tool_response')):
        common.record({'ts': ts, 'command': command[:200], 'why': 'nonzero-exit-bypass'}, None)
        return 0
    if common.is_binary_text(body):
        common.record({'ts': ts, 'command': command[:200], 'why': 'binary-bypass'}, None)
        return 0
    before = len(body.encode('utf-8', 'replace'))
    if before < common.MIN_CHARS:
        return 0
    filt = common.pick_filter(command, body)
    try:
        compressed = common.rtk_pipe(body, filt)
        if compressed is None:
            common.record({'ts': ts, 'command': command[:200], 'filter': filt,
                           'why': 'rtk-unavailable-or-empty'}, None)
            return 0
        after = len(compressed.encode('utf-8', 'replace'))
        if after >= before:
            return 0
        tag = hashlib.md5((command + body[:80]).encode('utf-8', 'replace')).hexdigest()[:8]
        path = common.spool(body, tag)
        notice = (u'[rtk-reducer｜bash 输出已压缩：原文 %s -> %s（-%s，省 %s）｜过滤器 %s｜逐字原文 %s]'
                  % (common.fmt_bytes(before), common.fmt_bytes(after), u'%.1f%%' % (100.0 * (before - after) / before),
                     common.fmt_bytes(before - after), filt, path))
        st = common.record({'ts': ts, 'command': command[:200], 'filter': filt, 'before': before,
                            'after': after, 'spool': path, 'session': payload.get('session_id')},
                           ('compressed', common.pattern_key(command, filt)))
        n_rules = common.learn_rules(st)
        tail = common.main_stats_line(st)
        if n_rules:
            tail += (u'｜新增 %d 条自学规则 -> %s，并进项目 .rtk/filters.toml 后这类命令由 rtk 原生压缩'
                     % (n_rules, common.rules_path()))
        emit(rebuild(u'%s\n%s\n--- 以下为 rtk 压缩后的输出 ---\n%s' % (notice, tail, compressed)))
    except Exception:
        # RT-01：压缩链路上任何异常（压缩器崩溃、spool/账本落盘失败……）
        # 都安静放行：不写 stdout，上游保留原始 stdout/stderr 与退出码。
        return 0
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.stderr.write('rtk-reducer 钩子异常，已原样放行：%s\n' % exc)
        raise SystemExit(0)
