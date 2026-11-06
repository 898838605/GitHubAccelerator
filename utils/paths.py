# -*- coding: utf-8 -*-
r"""路径工具：统一解析「程序目录」与「数据目录」。

两个目录要分开（v4.3 / P1-9）：
  - 程序目录（get_app_dir）：exe / 源码所在位置，只读也无所谓（Program Files 也行）；
  - 数据目录（get_data_dir）：配置 / 日志 / 候选清单 / 运行时图标放这里。
      默认 %LOCALAPPDATA%\GitHubAccelerator（当前用户可写，无需管理员）；
      如果程序目录下存在 portable.flag，则使用程序目录本身（便携模式）。

为什么不再把数据放 exe 旁边？
  exe 放在 Program Files、只读共享盘或 U 盘时，config.json / app.log / 候选清单
  都写不进去（旧版本会静默丢配置）。数据目录与安装位置解耦后，放哪都能正常运行。

旧数据迁移：
  首次使用新数据目录时，会把程序目录下的 config.json / github_hosts.txt 复制过去
  （只复制不删除，保留原文件以防用户回退旧版本）。

本模块属于项目最底层（utils 层）：只依赖标准库，不依赖项目内任何其他模块，
也不引入任何界面库 —— 这样上层（core / ui / main）都可以放心导入它。
"""

import logging
import os
import shutil
import sys

logger = logging.getLogger(__name__)

# 数据目录名（位于 %LOCALAPPDATA% 下）
DATA_DIR_NAME = 'GitHubAccelerator'

# 便携模式开关文件名：放在程序目录下即启用（数据随程序目录走）
PORTABLE_FLAG = 'portable.flag'

# 需要从旧位置（程序目录）迁移的数据文件
MIGRATE_FILES = ('config.json', 'github_hosts.txt')

# 迁移只做一次（进程内标记；目录内容存在时也不会重复复制）
_migrated = False


def get_app_dir():
    """获取程序所在目录（exe / 源码根目录）。

    功能：返回程序运行时应读写数据文件的目录。

    入参：无。
    返回值：str —— 绝对路径目录。

    为什么区分两种情况：
      - 直接运行 .py：返回项目根目录（本文件位于 项目根/utils/ 下，向上两级）；
      - PyInstaller 打包成 exe：返回 exe 所在目录。注意绝不能用 sys._MEIPASS
        （那是 exe 运行时解包出来的临时目录，每次运行都不同）。
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后：sys.executable 指向 exe 本身
        return os.path.dirname(os.path.abspath(sys.executable))
    # 脚本模式：__file__ 是 utils/paths.py，向上两级才是项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable():
    """是否处于便携模式（程序目录下存在 portable.flag）。

    入参：无。返回值：bool。
    """
    return os.path.exists(os.path.join(get_app_dir(), PORTABLE_FLAG))


def get_data_dir():
    r"""获取数据目录（配置 / 日志 / 候选清单的存放位置），并确保它存在。

    入参：无。
    返回值：str —— 绝对路径目录。

    默认 %LOCALAPPDATA%\GitHubAccelerator；便携模式（portable.flag）下
    退回程序目录。首次调用会把旧版留在程序目录的数据文件迁移过来。
    """
    path = _data_dir_path()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # 极端情况（LOCALAPPDATA 不可写）：退回程序目录，至少还能运行
        logger.exception('无法创建数据目录 %s，退回程序目录', path)
        path = get_app_dir()
    _migrate_legacy_data(path)
    return path


def _data_dir_path():
    """计算数据目录路径（不创建、不迁移）。"""
    if is_portable():
        return get_app_dir()
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    return os.path.join(base, DATA_DIR_NAME)


def _migrate_legacy_data(target_dir):
    """把旧版放在程序目录的数据文件复制到数据目录（只复制不删除）。

    入参：target_dir —— 数据目录。返回值：无。
    仅当目标文件不存在时复制，避免覆盖用户在新位置改过的配置。
    """
    global _migrated
    if _migrated:
        return
    _migrated = True
    source_dir = get_app_dir()
    if os.path.abspath(source_dir) == os.path.abspath(target_dir):
        return  # 便携模式 / 数据目录就是程序目录，无需迁移
    for name in MIGRATE_FILES:
        source = os.path.join(source_dir, name)
        target = os.path.join(target_dir, name)
        if not os.path.exists(source) or os.path.exists(target):
            continue
        try:
            shutil.copy2(source, target)
            logger.info('已迁移旧数据文件：%s -> %s', source, target)
        except OSError:
            logger.warning('迁移旧数据文件失败（忽略）：%s', source, exc_info=True)
