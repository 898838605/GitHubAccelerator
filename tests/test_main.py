# -*- coding: utf-8 -*-
"""main.py 命令行解析与按需提权约定的回归测试。"""

import pytest

import main
from core import service


@pytest.mark.parametrize('argv,expected', [
    ([], None),
    (['--elevated'], None),
    (['--do', 'enable'], 'enable'),
    (['--elevated', '--do', 'disable'], 'disable'),
    (['--do=force_restore'], 'force_restore'),
    (['--do', 'ENABLE'], 'enable'),
])
def test_parse_pending_action(argv, expected):
    assert main.parse_pending_action(argv) == expected


def test_pending_actions_whitelist():
    assert set(main.PENDING_ACTIONS) == {'enable', 'disable', 'force_restore'}
    assert main.PENDING_ACTIONS['enable'] is service.enable


def test_requires_admin_markers():
    assert service.enable.requires_admin is True
    assert service.disable.requires_admin is True
    assert service.force_restore.requires_admin is True
    # 只读 / 不写 hosts 的操作不需要管理员
    assert not getattr(service.test_all, 'requires_admin', False)
    assert not getattr(service.update_ip_list_online, 'requires_admin', False)
