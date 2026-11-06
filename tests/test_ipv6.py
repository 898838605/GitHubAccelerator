# -*- coding: utf-8 -*-
"""IPv6 支持：校验、解析、写入 hosts 与过滤。"""

import pytest

from core import hosts_manager

ORIGINAL_HOSTS = '127.0.0.1 localhost\n'
IPV6_GITHUB = '2606:50c0:8000::153'


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    hosts = tmp_path / 'hosts'
    hosts.write_text(ORIGINAL_HOSTS, encoding='utf-8')
    backup = tmp_path / 'hosts.github_backup'
    monkeypatch.setattr(hosts_manager, 'HOSTS_PATH', str(hosts))
    monkeypatch.setattr(hosts_manager, 'BACKUP_PATH', str(backup))
    monkeypatch.setattr(hosts_manager, 'flush_dns', lambda: True)
    return hosts, backup


def test_ipv6_detection():
    assert hosts_manager.looks_like_ipv6(IPV6_GITHUB)
    assert hosts_manager.looks_like_ipv6('::1')
    assert not hosts_manager.looks_like_ipv6('140.82.113.3')
    assert not hosts_manager.looks_like_ipv6('not-an-ip')
    assert hosts_manager.looks_like_ip(IPV6_GITHUB)
    assert hosts_manager.looks_like_ip('140.82.113.3')


def test_public_address_rules():
    assert hosts_manager.is_public_address(IPV6_GITHUB)
    assert hosts_manager.is_public_address('140.82.113.3')
    for bad in ('::1', 'fe80::1', 'fc00::1', '192.168.1.1', '127.0.0.1',
                '0.0.0.0', '2001:db8::1'):
        assert not hosts_manager.is_public_address(bad)


def test_parse_pairs_accepts_ipv6():
    text = '140.82.113.3 github.com\n' + IPV6_GITHUB + ' github.com\n'
    pairs = hosts_manager.parse_pairs(text, 'test')
    assert ('140.82.113.3', 'github.com') in pairs
    assert (IPV6_GITHUB, 'github.com') in pairs


def test_filter_allowed_entries_accepts_public_ipv6():
    pairs = [(IPV6_GITHUB, 'github.com'),
             ('fe80::1', 'github.com'),
             ('140.82.113.3', 'github.com')]
    allowed, rejected = hosts_manager.filter_allowed_entries(pairs)
    assert (IPV6_GITHUB, 'github.com') in allowed
    assert ('fe80::1', 'github.com') in rejected
    assert ('140.82.113.3', 'github.com') in allowed


def test_write_and_find_ipv6(sandbox):
    hosts, _backup = sandbox
    mismatched = hosts_manager.write_accel_section(
        {'github.com': IPV6_GITHUB, 'api.github.com': '140.82.113.6'})
    assert mismatched == []
    text = hosts.read_text(encoding='utf-8')
    assert IPV6_GITHUB + ' github.com' in text
    assert hosts_manager.find_hosts_ip('github.com') == IPV6_GITHUB
    assert hosts_manager.count_accel_entries() == 2
