# -*- coding: utf-8 -*-
"""版本号一致性：utils/version.py 是唯一来源，其它位置不得漂移。"""

import pathlib
import re

import main
from core import network_tester
from utils.version import __version__

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_pyproject_version_matches():
    text = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
    match = re.search(r'^version = "([^"]+)"', text, re.MULTILINE)
    assert match, 'pyproject.toml 缺少 version 字段'
    assert match.group(1) == __version__


def test_version_info_matches_exe_version():
    text = (ROOT / 'version_info.txt').read_text(encoding='utf-8')
    numbers = [*__version__.split('.'), '0']
    assert 'filevers=(' + ', '.join(numbers) + ')' in text
    assert 'prodvers=(' + ', '.join(numbers) + ')' in text
    assert f"'FileVersion', '{__version__}.0'" in text
    assert f"'ProductVersion', '{__version__}.0'" in text


def test_user_agent_and_window_title_use_version():
    assert network_tester.USER_AGENT == f'GitHubHostsAccelerator/{__version__}'
    assert __version__ in main.APP_TITLE
