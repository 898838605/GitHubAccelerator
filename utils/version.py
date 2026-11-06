# -*- coding: utf-8 -*-
"""应用版本号（运行时唯一来源）。

约定：版本号只在这里改一次，其他位置同步使用：
  - exe 文件版本：version_info.txt（tests/test_version.py 会校验一致性）
  - 构建元数据：pyproject.toml（同上校验）
  - 网络请求标识：core/network_tester.py 的 USER_AGENT
  - 窗口 / 托盘标题：main.py 的 APP_TITLE

版本语义：5.0.0 = 在原始项目（第四版，内部基线 4.0）之上的第一个正式大版本，
包含写入安全、测速严格校验、数据目录、按需提权、工程化等改动。
"""

__version__ = '5.1.2'
