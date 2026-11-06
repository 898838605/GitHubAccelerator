# -*- coding: utf-8 -*-
"""下载加速对话框的“项目主页”链接约定（防止误改回第三方地址）。"""

from ui import download_dialog


def test_learn_more_points_to_project_repo():
    assert download_dialog.LEARN_MORE_URL == 'https://github.com/898838605/GitHubAccelerator'
    assert download_dialog.LEARN_MORE_TEXT == '项目主页：GitHubAccelerator'
