# -*- coding: utf-8 -*-
"""T05 便携路径合同：显式 HOME 优先、默认绝不探测作者盘符、binary 只查 PATH、
缺 binary 时原样放行（原文进上下文，不压缩不丢数据）。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402


def test_explicit_home_wins(monkeypatch, tmp_path):
    monkeypatch.setenv('RTK_REDUCER_HOME', str(tmp_path / 'custom'))
    assert common.home() == str(tmp_path / 'custom')
    assert (tmp_path / 'custom' / 'spool').is_dir(), 'spool 目录必须随 home 初始化'


def test_default_home_never_probes_author_drive(monkeypatch, tmp_path):
    monkeypatch.delenv('RTK_REDUCER_HOME', raising=False)
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.setenv('HOME', str(tmp_path))
    # 模拟"作者盘符目录存在"：只对 F: 开头的路径返回真，其余走真实 exists
    # （不能全局恒真——会破坏 os.makedirs 的父目录检查，那是测试自身的缺陷）
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, 'exists',
                        lambda p: True if str(p).lower().startswith('f:') else real_exists(p))
    h = common.home()
    assert h == os.path.join(str(tmp_path), '.qoder', 'rtk-reducer')
    assert not h.lower().startswith('f:')


def test_binary_uses_path_only(monkeypatch):
    monkeypatch.setattr(common.shutil, 'which',
                        lambda name: 'C:/fake/rtk.exe' if name == 'rtk' else None)
    assert common.rtk_binary() == 'C:/fake/rtk.exe'
    # PATH 找不到时：即使作者盘符上的 rtk.exe "存在"，也不得回退探测
    monkeypatch.setattr(common.shutil, 'which', lambda name: None)
    monkeypatch.setattr(os.path, 'exists', lambda p: True)
    assert common.rtk_binary() is None


def test_missing_binary_preserves_original_output(monkeypatch):
    monkeypatch.setattr(common, 'rtk_binary', lambda: None)
    assert common.rtk_pipe('some long original output', 'log') is None, \
        '缺 binary 必须返回 None（调用方原样放行原文），不得吞数据'
