# -*- coding: utf-8 -*-
"""core/network_tester 纯函数回归测试。"""

from core import network_tester as nt_mod

network_tester = nt_mod  # 兼容旧用例的模块别名


def test_pick_best_from_results():
    pairs = [('1.1.1.1', 'github.com'),
             ('2.2.2.2', 'github.com'),
             ('3.3.3.3', 'api.github.com')]
    results = {'1.1.1.1': (120, None),
               '2.2.2.2': (80, None),
               '3.3.3.3': (None, 'TimeoutError: timed out')}
    assert network_tester.pick_best_from_results(pairs, results) == {
        'github.com': '2.2.2.2'}


def test_pick_best_returns_empty_when_all_failed():
    pairs = [('1.1.1.1', 'github.com')]
    results = {'1.1.1.1': (None, 'TimeoutError: timed out')}
    assert network_tester.pick_best_from_results(pairs, results) == {}


def test_classify_error_labels():
    assert nt_mod._classify_error(TimeoutError('timed out')) == '超时'
    assert nt_mod._classify_error(ConnectionRefusedError()) == '拒绝连接'


def test_progress_callback(monkeypatch):
    monkeypatch.setattr(nt_mod, '_probe_ip', lambda ip, timeout, attempts: (5, None))
    calls = []
    results = nt_mod.test_ips_concurrent(
        ['1.1.1.1', '2.2.2.2'],
        progress_cb=lambda done, total: calls.append((done, total)))
    assert results == {'1.1.1.1': (5, None), '2.2.2.2': (5, None)}
    assert calls[-1] == (2, 2)


def test_cancel_returns_immediately(monkeypatch):
    import threading

    event = threading.Event()
    event.set()
    monkeypatch.setattr(nt_mod, '_probe_ip', lambda ip, timeout, attempts: (5, None))
    calls = []
    results = nt_mod.test_ips_concurrent(
        ['1.1.1.1', '2.2.2.2'], progress_cb=lambda done, total: calls.append(done),
        cancel_event=event)
    assert results == {}
    assert calls == []


def test_probe_takes_min_of_samples(monkeypatch):
    samples = iter([(120, None), (80, None)])
    monkeypatch.setattr(nt_mod, '_tcp_connect',
                        lambda host, port, timeout: next(samples))
    assert nt_mod._probe_ip('1.1.1.1', 2.0, 2) == (80, None)


def test_probe_no_retry_when_failed(monkeypatch):
    calls = []

    def fake_connect(host, port, timeout):
        calls.append(host)
        return None, '超时'

    monkeypatch.setattr(nt_mod, '_tcp_connect', fake_connect)
    assert nt_mod._probe_ip('1.1.1.1', 2.0, 3) == (None, '超时')
    assert len(calls) == 1


def test_verify_tls_batch(monkeypatch):
    def fake_verify(domain, ip, timeout):
        ok = domain != 'bad.github.com'
        return ok, (None if ok else 'TLS 校验失败')

    monkeypatch.setattr(nt_mod, 'verify_tls', fake_verify)
    results = nt_mod.verify_tls_batch([('github.com', '1.1.1.1'),
                                       ('bad.github.com', '2.2.2.2')])
    assert results['github.com'][0] is True
    assert results['bad.github.com'] == (False, 'TLS 校验失败')


def test_address_family_selection():
    import socket
    assert nt_mod._address_family('140.82.113.3') == socket.AF_INET
    assert nt_mod._address_family('github.com') == socket.AF_INET
    assert nt_mod._address_family('2606:50c0:8000::153') == socket.AF_INET6
