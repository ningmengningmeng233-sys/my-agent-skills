# DramaCraft Engine

import sys
from typing import Any, Iterable, List


def harden_stdio(streams: Iterable[Any] = ()) -> List[Any]:
    """标准输出/错误的编码降级：窄编码终端遇到编不出的字符，转义显示而不是抛异常。

    Windows 默认控制台（gbk）编不出 emoji 与 ✓/✗，`print()` 会抛 UnicodeEncodeError 把整个
    CLI 打成崩溃——退出码、机读报告、上游调用方跟着一起失真。这里只改**输出流的错误处理**：
    支持 UTF-8 的终端原样输出，窄编码终端把编不出的字符写成 `\\U0001fa7a` 这类可读转义。
    机读件一律显式 `encoding="utf-8"` 落盘，不走这条通道，内容与退出码都不受影响。
    """
    hardened: List[Any] = []
    for stream in list(streams) or [sys.stdout, sys.stderr]:
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError, LookupError):
            continue        # 没有 reconfigure（被测试框架接管等）就跳过：绝不因此影响启动
        hardened.append(stream)
    return hardened


# 包一被导入就装上：所有 CLI 都经 `dramacraft.*` 进来（python -m dramacraft.X 与 run_batch.py 都一样）
harden_stdio()
