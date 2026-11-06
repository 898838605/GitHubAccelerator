# -*- coding: utf-8 -*-
"""core/download_helper 回归测试：URL 校验、前缀规范化、链接识别与拼接。"""

import pytest

from core import download_helper


@pytest.mark.parametrize('url', [
    'https://github.com/a/b',
    'http://github.com/a/b',
    'https://GitHub.com/a/b',
    'https://github.com',
])
def test_valid_github_url(url):
    assert download_helper.is_valid_github_url(url)


@pytest.mark.parametrize('url', [
    '',
    'ftp://github.com/a/b',
    'https://github.com.evil.com/a/b',
    'https://gist.github.com/a/b',
    'github.com/a/b',
    'https://evil.com/github.com',
])
def test_invalid_github_url(url):
    assert not download_helper.is_valid_github_url(url)


def test_normalize_prefix():
    assert download_helper.normalize_prefix('https://gh-proxy.com') == 'https://gh-proxy.com/'
    assert download_helper.normalize_prefix('https://gh-proxy.com///') == 'https://gh-proxy.com/'


def test_build_accel_url():
    ok, result = download_helper.build_accel_url(
        'https://gh-proxy.com', ' https://github.com/a/b ')
    assert ok
    assert result == 'https://gh-proxy.com/https://github.com/a/b'


def test_build_accel_url_rejects_empty():
    ok, message = download_helper.build_accel_url('https://gh-proxy.com', '   ')
    assert not ok
    assert message == download_helper.MSG_EMPTY


def test_resolve_clone_link():
    kind, resolved = download_helper.resolve_github_url(
        'https://github.com/owner/repo.git')
    assert kind == 'clone'
    assert resolved == 'https://github.com/owner/repo/archive/refs/heads/main.zip'


def test_resolve_clone_honours_branch():
    _kind, resolved = download_helper.resolve_github_url(
        'https://github.com/owner/repo.git', 'master')
    assert resolved.endswith('/archive/refs/heads/master.zip')


def test_resolve_repo_page():
    kind, resolved = download_helper.resolve_github_url('https://github.com/owner/repo')
    assert kind == 'repo'
    assert resolved == 'https://github.com/owner/repo/archive/refs/heads/main.zip'


def test_resolve_file_link():
    url = 'https://github.com/o/r/releases/download/v1/a.zip'
    assert download_helper.resolve_github_url(url) == ('file', url)


def test_resolve_unknown_page():
    assert download_helper.resolve_github_url('https://github.com/o/r/issues/1')[0] == 'unknown'
