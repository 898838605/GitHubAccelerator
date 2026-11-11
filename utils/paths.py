# -*- coding: utf-8 -*-
r"""路径工具：程序目录与数据目录。

v5.4 起：配置、日志、候选清单、图标等运行时数据默认放在程序所在目录
（exe 同目录 / 源码根目录），不再默认写 %LOCALAPPDATA%。

原因：用户希望绿色版/单文件 exe 的所有自动生成文件都跟着 exe 走，
复制整个目录即可迁移，不需要去 %LOCALAPPDATA% 找配置。

兼容与兜底：
  - 如果程序目录不可写（例如放在 Program Files），自动退回
    %LOCALAPPDATA%\GitHubAccelerator，并写日志说明；
  - 首次使用新位置时，会把旧版数据目录里的 config.json / github_hosts.txt /
    app.log / 图标 / 备份等复制到程序目录（只复制不删除，不覆盖已有文件）；
  - portable.flag 保留为兼容常量：现在数据始终跟随程序目录，该文件不再必需。

本模块属于项目最底层（utils 层）：只依赖标准库，不依赖项目内其他模块。
"""

import logging
import os
import shutil
import sys

logger = logging.getLogger(__name__)

# 旧版数据目录名（位于 %LOCALAPPDATA% 下）；现在仅作为不可写时的兜底
DATA_DIR_NAME = 'GitHubAccelerator'

# 兼容旧版本的便携开关文件名；v5.4 起数据始终在程序目录，不再依赖该文件
PORTABLE_FLAG = 'portable.flag'

# 从旧数据目录复制到程序目录的文件（只复制不删除，已存在则跳过）
MIGRATE_FILES = (
    'config.json',
    'github_hosts.txt',
    'app.log',
    'app.log.1',
    'app.log.2',
    'icon.png',
    'icon.ico',
    'profiles.json',
    'git_mirror_backup.json',
    'git_proxy_backup.json',
)

_migrated = False


def get_app_dir():
    """获取程序所在目录（exe / 源码根目录）。

    返回值：str —— 绝对路径目录。
    PyInstaller 打包后返回 exe 所在目录（绝不能用 sys._MEIPASS，那是临时解包目录）。
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable():
    """兼容旧接口：数据现在始终跟随程序目录，因此恒为 True。"""
    return True


def _fallback_data_dir():
    """旧版数据目录 / 程序目录不可写时的兜底目录。"""
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        base = os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    return os.path.join(base, DATA_DIR_NAME)


def _is_writable_dir(path):
    """用创建一个临时文件的方式判断目录是否真的可写。"""
    probe = os.path.join(path, '.gha_write_test')
    try:
        with open(probe, 'w', encoding='utf-8') as file:
            file.write('')
        os.remove(probe)
        return True
    except OSError:
        try:
            if os.path.exists(probe):
                os.remove(probe)
        except OSError:
            pass
        return False


def get_data_dir():
    r"""获取数据目录并确保可写。

    默认返回程序目录；程序目录不可写时退回 %LOCALAPPDATA%\GitHubAccelerator。
    首次使用程序目录时，会从旧 %LOCALAPPDATA% 目录复制数据文件。
    """
    preferred = get_app_dir()
    path = preferred
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        logger.warning('无法创建程序目录 %s，退回旧数据目录', path, exc_info=True)
        path = _fallback_data_dir()

    if path == preferred and not _is_writable_dir(path):
        fallback = _fallback_data_dir()
        logger.warning('程序目录不可写：%s；退回 %s', path, fallback)
        try:
            os.makedirs(fallback, exist_ok=True)
            path = fallback
        except OSError:
            logger.exception('兜底数据目录也不可写：%s', fallback)

    _migrate_legacy_data(path)
    return path


def _data_dir_path():
    """兼容旧测试/调用：返回首选数据目录（不创建、不迁移）。"""
    return get_app_dir()


def _migrate_legacy_data(target_dir):
    """把旧 %LOCALAPPDATA% 数据复制到程序目录（只复制不删除）。

    规则：
      - 目标不存在：复制；
      - 目标已存在且比源文件新：保留目标（用户已在新位置改过）；
      - 目标已存在但比源文件旧：用源文件覆盖（把当前配置带到 exe 旁边）。
    """
    global _migrated
    if _migrated:
        return
    _migrated = True

    source_dir = _fallback_data_dir()
    if os.path.abspath(source_dir) == os.path.abspath(target_dir):
        return
    if not os.path.isdir(source_dir):
        return

    for name in MIGRATE_FILES:
        source = os.path.join(source_dir, name)
        target = os.path.join(target_dir, name)
        if not os.path.isfile(source):
            continue
        try:
            if os.path.exists(target):
                if os.path.getmtime(source) <= os.path.getmtime(target):
                    continue
                logger.info('旧数据文件较新，覆盖目标：%s -> %s', source, target)
            shutil.copy2(source, target)
        except OSError:
            logger.warning('迁移旧数据文件失败（忽略）：%s', source, exc_info=True)
