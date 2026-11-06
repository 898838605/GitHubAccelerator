# -*- coding: utf-8 -*-
"""core/service 端到端流程测试（全部 IO 指向临时目录，不碰真实 hosts）。"""

from core import hosts_manager
from core import network_tester
from core import service
from core.app_state import op_cancel, state


def _use_temp_hosts(tmp_path, monkeypatch):
    hosts = tmp_path / 'hosts'
    hosts.write_text('127.0.0.1 localhost\n1.2.3.4 github.com\n', encoding='utf-8')
    backup = tmp_path / 'hosts.github_backup'
    candidate = tmp_path / 'github_hosts.txt'
    candidate.write_text('140.82.113.3 github.com\n'
                         '185.199.108.133 raw.githubusercontent.com\n'
                         '192.168.1.1 ckasa.com\n', encoding='utf-8')
    monkeypatch.setattr(hosts_manager, 'HOSTS_PATH', str(hosts))
    monkeypatch.setattr(hosts_manager, 'BACKUP_PATH', str(backup))
    monkeypatch.setattr(hosts_manager, 'get_candidate_path', lambda: str(candidate))
    monkeypatch.setattr(hosts_manager, 'flush_dns', lambda: True)
    service.init({'auto_update_on_enable': False, 'refresh_interval_hours': 6,
                  'test_timeout_seconds': 1.0, 'strict_tls_verify': False})
    service.set_notifier(None)
    return hosts, backup, candidate


def test_enable_and_disable_flow(tmp_path, monkeypatch):
    hosts, _backup, _candidate = _use_temp_hosts(tmp_path, monkeypatch)
    monkeypatch.setattr(network_tester, 'test_ips_concurrent',
                        lambda ips, **kwargs: {ip: (10, None) for ip in ips})

    ok, message = service.enable()
    assert ok, message
    text = hosts.read_text(encoding='utf-8')
    # 加速段写在最前面，且原条目保留
    assert text.startswith(hosts_manager.START_MARKER)
    assert '1.2.3.4 github.com' in text
    assert hosts_manager.find_hosts_ip('github.com') == '140.82.113.3'
    # 白名单外的域名绝不能进入系统 hosts
    assert 'ckasa.com' not in text

    ok, _message = service.disable()
    assert ok
    text = hosts.read_text(encoding='utf-8')
    assert hosts_manager.START_MARKER not in text
    assert '1.2.3.4 github.com' in text
    assert state.enabled is False


def test_enable_rejects_when_all_candidates_filtered(tmp_path, monkeypatch):
    hosts, _backup, candidate = _use_temp_hosts(tmp_path, monkeypatch)
    candidate.write_text('192.168.1.1 ckasa.com\n', encoding='utf-8')
    monkeypatch.setattr(network_tester, 'test_ips_concurrent',
                        lambda ips, **kwargs: {ip: (10, None) for ip in ips})
    ok, message = service.enable()
    assert not ok
    assert '没有可用' in message or '不可用' in message
    assert hosts_manager.START_MARKER not in hosts.read_text(encoding='utf-8')


def test_update_ip_list_filters_remote_data(tmp_path, monkeypatch):
    _hosts, _backup, candidate = _use_temp_hosts(tmp_path, monkeypatch)
    candidate.write_text('', encoding='utf-8')
    remote = ('140.82.113.3 github.com\n'
              '192.168.1.1 ckasa.com\n'
              '8.8.8.8 bank.example\n')
    monkeypatch.setattr(network_tester, 'fetch_remote_hosts', lambda: (True, remote))

    ok, message, _added, _total = service.update_ip_list_online(silent=True)
    assert ok, message
    text = candidate.read_text(encoding='utf-8')
    assert 'github.com' in text
    assert 'ckasa.com' not in text
    assert 'bank.example' not in text
    assert '过滤' in message


def test_enable_cancelled_before_start(tmp_path, monkeypatch):
    _use_temp_hosts(tmp_path, monkeypatch)
    monkeypatch.setattr(network_tester, 'test_ips_concurrent',
                        lambda ips, **kwargs: {ip: (10, None) for ip in ips})
    op_cancel.set()
    try:
        ok, message = service.enable()
    finally:
        op_cancel.clear()
    assert not ok
    assert '取消' in message


def test_strict_tls_drops_failed_domains(tmp_path, monkeypatch):
    hosts, _backup, _candidate = _use_temp_hosts(tmp_path, monkeypatch)
    service.init({'auto_update_on_enable': False, 'refresh_interval_hours': 6,
                  'test_timeout_seconds': 1.0, 'strict_tls_verify': True})
    monkeypatch.setattr(network_tester, 'test_ips_concurrent',
                        lambda ips, **kwargs: {ip: (10, None) for ip in ips})

    def fake_verify(checks, **kwargs):
        return {domain: (domain != 'api.github.com', None)
                for domain, _ip in checks}

    monkeypatch.setattr(network_tester, 'verify_tls_batch', fake_verify)
    ok, message = service.enable()
    assert ok, message
    text = hosts.read_text(encoding='utf-8')
    assert 'api.github.com' not in text
    assert hosts_manager.find_hosts_ip('github.com') == '140.82.113.3'
    service.disable()


def test_strict_tls_all_failed_aborts_without_writing(tmp_path, monkeypatch):
    hosts, _backup, _candidate = _use_temp_hosts(tmp_path, monkeypatch)
    service.init({'auto_update_on_enable': False, 'refresh_interval_hours': 6,
                  'test_timeout_seconds': 1.0, 'strict_tls_verify': True})
    monkeypatch.setattr(network_tester, 'test_ips_concurrent',
                        lambda ips, **kwargs: {ip: (10, None) for ip in ips})
    monkeypatch.setattr(network_tester, 'verify_tls_batch',
                        lambda checks, **kwargs: {domain: (False, 'TLS 校验失败')
                                                  for domain, _ip in checks})
    ok, message = service.enable()
    assert not ok
    assert hosts_manager.START_MARKER not in hosts.read_text(encoding='utf-8')


def test_connectivity_includes_proxy_info(monkeypatch):
    from core import network_tester
    from utils import proxy as proxy_mod

    monkeypatch.setattr(network_tester, 'run_connectivity_test',
                        lambda: {'time': 'x', 'github': '1ms ✓', 'raw': '2ms ✓',
                                 'overall': '可达', 'summary': 'github: 1ms ✓',
                                 'summary_line': '✅ 整体连通正常'})
    monkeypatch.setattr(proxy_mod, 'detect_proxy', lambda: {
        'enabled': True, 'server': '127.0.0.1:7890', 'source': '系统设置',
        'pac': None, 'bypass': None})
    details = service.test_connectivity()
    assert details['proxy_enabled'] is True
    assert '127.0.0.1:7890' in details['proxy']
