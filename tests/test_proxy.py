# -*- coding: utf-8 -*-
"""utils/proxy 系统代理检测的回归测试（不读写真实注册表）。"""

import pytest

from utils import proxy


def _clear_env(monkeypatch):
    for name in proxy._ENV_PROXY_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_env_proxy_has_priority(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    info = proxy.detect_proxy()
    assert info['enabled'] is True
    assert info['server'] == 'http://127.0.0.1:7890'
    assert info['source'] == '环境变量'
    assert '7890' in proxy.describe_proxy(info)


def test_registry_proxy(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(proxy, '_read_registry', lambda: {
        'enable': 1,
        'server': 'http=127.0.0.1:7890;https=127.0.0.1:7891',
        'pac': None, 'bypass': 'localhost;127.*'})
    info = proxy.detect_proxy()
    assert info['enabled'] is True
    assert info['server'] == '127.0.0.1:7891'
    assert info['source'] == '系统设置'
    assert info['bypass'] == 'localhost;127.*'


def test_pac_only(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(proxy, '_read_registry', lambda: {
        'enable': 0, 'server': None,
        'pac': 'http://pac.example/proxy.pac', 'bypass': None})
    info = proxy.detect_proxy()
    assert info['enabled'] is True
    assert info['source'] == '自动配置脚本(PAC)'
    assert info['server'] is None


def test_no_proxy(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setattr(proxy, '_read_registry', lambda: {
        'enable': 0, 'server': None, 'pac': None, 'bypass': None})
    info = proxy.detect_proxy()
    assert info['enabled'] is False
    assert proxy.describe_proxy(info) == '未检测到'


@pytest.mark.parametrize('raw,expected', [
    (None, None),
    ('', None),
    ('127.0.0.1:7890', '127.0.0.1:7890'),
    ('http=127.0.0.1:7890;https=127.0.0.1:7891', '127.0.0.1:7891'),
    ('http=127.0.0.1:7890', '127.0.0.1:7890'),
])
def test_primary_server(raw, expected):
    assert proxy._primary_server(raw) == expected
