# -*- coding: utf-8 -*-
"""core/config_manager 回归测试：默认值、损坏文件、字段回退与合并写回。"""

import json

from core import config_manager


def test_defaults_created_when_missing(tmp_path):
    path = tmp_path / 'config.json'
    config = config_manager.load_config(str(path))
    assert config['refresh_interval_hours'] == 6
    assert config['auto_update_on_enable'] is True
    assert path.exists()
    assert json.loads(path.read_text(encoding='utf-8')) == config_manager.DEFAULT_CONFIG


def test_corrupt_config_not_overwritten(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{not json', encoding='utf-8')
    config = config_manager.load_config(str(path))
    assert config['refresh_interval_hours'] == 6
    # 损坏的原文件必须原样保留（不能被程序偷偷重置）
    assert path.read_text(encoding='utf-8') == '{not json'
    # 同时另存一份 .bad 副本方便排查
    assert (tmp_path / 'config.json.bad').exists()


def test_bad_field_falls_back(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'refresh_interval_hours': 0,
                                'auto_update_on_enable': 'no'}), encoding='utf-8')
    config = config_manager.load_config(str(path))
    assert config['refresh_interval_hours'] == 6
    assert config['auto_update_on_enable'] is False


def test_merge_keeps_unknown_fields(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'custom': 'keep', 'window_width': 700}), encoding='utf-8')
    assert config_manager.merge_config_values({'window_height': 500}, str(path))
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['custom'] == 'keep'
    assert data['window_width'] == 700
    assert data['window_height'] == 500


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / 'config.json'
    assert config_manager.save_config({'auto_update_on_enable': False,
                                       'refresh_interval_hours': 3}, str(path))
    config = config_manager.load_config(str(path))
    assert config['refresh_interval_hours'] == 3
    assert config['auto_update_on_enable'] is False
