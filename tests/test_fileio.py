# -*- coding: utf-8 -*-
"""utils/fileio 原子写入的回归测试。"""

import pytest

from utils import fileio


def test_atomic_write_text_creates_and_replaces(tmp_path):
    target = tmp_path / 'x.txt'
    target.write_text('old', encoding='utf-8')
    fileio.atomic_write_text(str(target), 'new')
    assert target.read_text(encoding='utf-8') == 'new'


def test_atomic_write_keeps_original_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / 'x.txt'
    target.write_text('old', encoding='utf-8')

    def boom(*args, **kwargs):
        raise OSError('locked by antivirus')

    monkeypatch.setattr(fileio, '_replace_with_retry', boom)
    with pytest.raises(OSError):
        fileio.atomic_write_text(str(target), 'new')
    # 原文件必须是旧内容；临时文件必须被清理干净
    assert target.read_text(encoding='utf-8') == 'old'
    assert not list(tmp_path.glob('*.tmp'))
