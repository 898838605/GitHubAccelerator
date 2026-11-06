# -*- coding: utf-8 -*-
"""core/app_state 发布订阅回归测试。"""

from core.app_state import AppState


def test_publish_isolates_subscriber_errors():
    state = AppState()
    seen = []

    def bad_callback(event, payload=None):
        raise RuntimeError('boom')

    def good_callback(event, payload=None):
        seen.append((event, payload))

    state.subscribe(bad_callback)
    state.subscribe(good_callback)
    state.set_enabled(True)

    assert state.enabled is True
    assert ('enabled', True) in seen
