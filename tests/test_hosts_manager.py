# -*- coding: utf-8 -*-
"""core/hosts_manager 回归测试：原子写入、冲突处理、备份一致性与兜底还原。

测试通过 monkeypatch 把 HOSTS_PATH / BACKUP_PATH 指向临时目录，
并禁用 ipconfig 调用，绝不触碰真实的系统 hosts 文件。
"""

import pytest

from core import hosts_manager

ORIGINAL_HOSTS = (
    '127.0.0.1 localhost\r\n'
    '1.2.3.4 github.com\r\n'
)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    hosts = tmp_path / 'hosts'
    hosts.write_bytes(ORIGINAL_HOSTS.encode('utf-8'))
    backup = tmp_path / 'hosts.github_backup'
    monkeypatch.setattr(hosts_manager, 'HOSTS_PATH', str(hosts))
    monkeypatch.setattr(hosts_manager, 'BACKUP_PATH', str(backup))
    monkeypatch.setattr(hosts_manager, 'flush_dns', lambda: True)
    return hosts, backup


def read(path):
    return path.read_bytes().decode('utf-8')


def test_write_accel_section_wins_over_existing_entry(sandbox):
    hosts, _ = sandbox
    mismatched = hosts_manager.write_accel_section({'github.com': '140.82.113.3'})
    assert mismatched == []
    text = read(hosts)
    assert text.startswith(hosts_manager.START_MARKER)
    # Windows 按文件顺序首个匹配生效：新段必须排在旧条目之前
    assert hosts_manager.find_hosts_ip('github.com') == '140.82.113.3'
    # 原条目仍然保留，停用时整体还原
    assert '1.2.3.4 github.com' in text


def test_write_accel_section_preserves_newline_style(sandbox):
    hosts, _ = sandbox
    hosts_manager.write_accel_section({'github.com': '140.82.113.3'})
    assert hosts_manager.START_MARKER + '\r\n' in read(hosts)


def test_write_accel_section_replaces_previous_section(sandbox):
    hosts, _ = sandbox
    hosts_manager.write_accel_section({'github.com': '1.1.1.1'})
    hosts_manager.write_accel_section({'github.com': '2.2.2.2'})
    text = read(hosts)
    assert text.count(hosts_manager.START_MARKER) == 1
    assert hosts_manager.find_hosts_ip('github.com') == '2.2.2.2'


def test_count_accel_entries(sandbox):
    hosts_manager.write_accel_section(
        {'github.com': '1.1.1.1', 'api.github.com': '2.2.2.2'})
    assert hosts_manager.count_accel_entries() == 2


def test_remove_accel_sections_handles_unclosed():
    text = 'a\n' + hosts_manager.START_MARKER + '\n1.1.1.1 github.com\n'
    assert hosts_manager.remove_accel_sections(text) == 'a\n'


def test_ensure_backup_strips_contaminated_section(sandbox):
    hosts, backup = sandbox
    hosts_manager.write_accel_section({'github.com': '1.1.1.1'})
    ok, _message = hosts_manager.ensure_backup()
    assert ok
    assert backup.exists()
    backup_text = read(backup)
    assert hosts_manager.START_MARKER not in backup_text
    assert '127.0.0.1 localhost' in backup_text


def test_ensure_backup_never_overwrites(sandbox):
    _hosts, backup = sandbox
    backup.write_text('ORIGINAL-BACKUP', encoding='utf-8')
    ok, message = hosts_manager.ensure_backup()
    assert ok
    assert message == '备份已存在'
    assert read(backup) == 'ORIGINAL-BACKUP'


def test_restore_from_backup(sandbox):
    hosts, _backup = sandbox
    hosts_manager.ensure_backup()
    hosts_manager.write_accel_section({'github.com': '140.82.113.3'})
    ok, _message = hosts_manager.restore_from_backup()
    assert ok
    text = read(hosts)
    assert hosts_manager.START_MARKER not in text
    assert '1.2.3.4 github.com' in text


def test_restore_without_backup_falls_back_to_marker_removal(sandbox):
    hosts, backup = sandbox
    hosts_manager.write_accel_section({'github.com': '140.82.113.3'})
    assert not backup.exists()
    ok, message = hosts_manager.restore_from_backup()
    assert ok
    assert '移除加速段' in message
    assert hosts_manager.START_MARKER not in read(hosts)


def test_restore_without_backup_and_without_section(sandbox):
    ok, message = hosts_manager.restore_from_backup()
    assert not ok
    assert '未找到备份文件' in message


def test_is_public_ipv4():
    assert hosts_manager.is_public_ipv4('140.82.113.3')
    for bad in ('0.0.0.0', '127.0.0.1', '192.168.1.1', '10.0.0.1', '255.255.255.255'):
        assert not hosts_manager.is_public_ipv4(bad)
    assert not hosts_manager.is_public_ipv4('not-an-ip')


def test_is_allowed_domain():
    assert hosts_manager.is_allowed_domain('github.com')
    assert hosts_manager.is_allowed_domain('api.github.com')
    assert hosts_manager.is_allowed_domain('raw.githubusercontent.com')
    assert not hosts_manager.is_allowed_domain('ckasa.com')
    assert not hosts_manager.is_allowed_domain('evilgithub.com')
    assert not hosts_manager.is_allowed_domain('github.com.evil.com')


def test_filter_allowed_entries():
    pairs = [('140.82.113.3', 'github.com'),
             ('192.168.1.1', 'ckasa.com'),
             ('8.8.8.8', 'bank.example')]
    allowed, rejected = hosts_manager.filter_allowed_entries(pairs)
    assert allowed == [('140.82.113.3', 'github.com')]
    assert len(rejected) == 2


def test_merge_prunes_remote_entries_and_keeps_user_lines(tmp_path, monkeypatch):
    candidate = tmp_path / 'github_hosts.txt'
    candidate.write_text('9.9.9.9 github.com\n', encoding='utf-8')
    monkeypatch.setattr(hosts_manager, 'get_candidate_path', lambda: str(candidate))
    for index in range(hosts_manager.MAX_REMOTE_PER_DOMAIN + 3):
        hosts_manager.merge_remote_into_local([(f'1.1.1.{index}', 'github.com')])
    lines = candidate.read_text(encoding='utf-8').splitlines()
    marked = [line for line in lines if hosts_manager.REMOTE_LINE_MARK in line]
    assert len(marked) == hosts_manager.MAX_REMOTE_PER_DOMAIN
    assert '9.9.9.9 github.com' in lines
