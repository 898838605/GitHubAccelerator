# -*- coding: utf-8 -*-
"""utils/admin 单实例互斥量的回归测试（用随机名字，避免与真实实例冲突）。"""

import uuid

from utils import admin


def test_acquire_and_release(monkeypatch):
    name = 'GHA_Test_' + uuid.uuid4().hex
    monkeypatch.setattr(admin, 'MUTEX_NAMES', ('Global\\' + name, name))
    handles = admin.acquire_single_instance()
    assert handles not in (None, 0)
    assert admin._mutex_handles
    admin.release_mutex()
    assert admin._mutex_handles == []
    # 释放后可以再次获取（模拟"旧实例退出、新实例接管"）
    again = admin.acquire_single_instance()
    assert again not in (None, 0)
    admin.release_mutex()


def test_quote_arg():
    from utils.admin import _quote_arg
    assert _quote_arg('--do') == '--do'
    assert _quote_arg('C:/Program Files/app.exe') == '"C:/Program Files/app.exe"'
    assert _quote_arg('') == '""'
