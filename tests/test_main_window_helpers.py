# -*- coding: utf-8 -*-
"""主窗口纯函数回归测试（不创建窗口）。"""

from ui.main_window import _pairs_for_items


def test_pairs_for_items_reverse_mapping():
    key_to_item = {
        ('140.82.113.3', 'github.com'): 'I001',
        ('2606:50c0:8000::153', 'github.com'): 'I002',
    }
    assert _pairs_for_items(key_to_item, ('I002',)) == [
        ('2606:50c0:8000::153', 'github.com')]
    assert _pairs_for_items(key_to_item, ('I001', 'I002')) == [
        ('140.82.113.3', 'github.com'), ('2606:50c0:8000::153', 'github.com')]
    assert _pairs_for_items(key_to_item, ('I999',)) == []
    assert _pairs_for_items(key_to_item, ()) == []
