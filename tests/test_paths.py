# -*- coding: utf-8 -*-
"""utils/paths 数据目录、便携模式与旧数据迁移的回归测试。"""

import os

from utils import paths


def _sandbox(monkeypatch, tmp_path):
    app_dir = tmp_path / 'app'
    app_dir.mkdir()
    local = tmp_path / 'localappdata'
    local.mkdir()
    monkeypatch.setattr(paths, 'get_app_dir', lambda: str(app_dir))
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setattr(paths, '_migrated', False)
    return app_dir, local


def test_default_data_dir_under_localappdata(monkeypatch, tmp_path):
    app_dir, local = _sandbox(monkeypatch, tmp_path)
    data_dir = paths.get_data_dir()
    assert data_dir == str(local / paths.DATA_DIR_NAME)
    assert os.path.isdir(data_dir)
    assert not os.path.exists(os.path.join(str(app_dir), paths.PORTABLE_FLAG))


def test_portable_mode_uses_app_dir(monkeypatch, tmp_path):
    app_dir, _local = _sandbox(monkeypatch, tmp_path)
    (app_dir / paths.PORTABLE_FLAG).write_text('', encoding='utf-8')
    assert paths.get_data_dir() == str(app_dir)


def test_legacy_files_migrated_once(monkeypatch, tmp_path):
    app_dir, local = _sandbox(monkeypatch, tmp_path)
    (app_dir / 'config.json').write_text('{"legacy": true}', encoding='utf-8')
    (app_dir / 'github_hosts.txt').write_text('140.82.113.3 github.com\n',
                                              encoding='utf-8')
    paths.get_data_dir()
    assert (local / paths.DATA_DIR_NAME / 'config.json').exists()
    assert (local / paths.DATA_DIR_NAME / 'github_hosts.txt').exists()
    # 原文件保留（方便用户回退旧版本）
    assert (app_dir / 'config.json').exists()
    # 已存在同名文件时不覆盖
    (local / paths.DATA_DIR_NAME / 'config.json').write_text('{"new": true}',
                                                             encoding='utf-8')
    monkeypatch.setattr(paths, '_migrated', False)
    paths.get_data_dir()
    assert (local / paths.DATA_DIR_NAME / 'config.json').read_text(
        encoding='utf-8') == '{"new": true}'


def test_is_portable_flag(monkeypatch, tmp_path):
    app_dir, _local = _sandbox(monkeypatch, tmp_path)
    assert not paths.is_portable()
    (app_dir / paths.PORTABLE_FLAG).write_text('', encoding='utf-8')
    assert paths.is_portable()
