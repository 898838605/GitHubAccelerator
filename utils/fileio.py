# -*- coding: utf-8 -*-
"""文件原子写入：先写同目录临时文件，再原子替换目标文件。

为什么需要它？
  配置文件、候选清单，尤其是**系统 hosts**，都可能被「写到一半」打断
  （断电、蓝屏、杀毒软件拦截、用户强制结束进程）。直接 open('w') 会先
  把原文件截断成 0 字节，一旦后续写入失败，原内容就永久丢失。
  原子写入保证：任何时刻磁盘上的目标文件要么是旧内容、要么是新内容，
  不存在「空文件」或「半新半旧」的中间状态。

实现要点：
  1. 临时文件必须与目标文件同目录（同一磁盘才能原子 rename）；
  2. fsync 确保数据真正落盘后再替换，避免「rename 成功但数据还在缓存」；
  3. 尽力复制原文件权限（Windows 上是只读属性），保持访问控制一致；
  4. os.replace 本身是原子的（Windows 上等价于 MoveFileEx + REPLACE_EXISTING）；
  5. Windows 上杀毒软件 / 索引服务可能短暂占用目标文件，因此替换带重试。
"""

import logging
import os
import shutil
import tempfile
import time

logger = logging.getLogger(__name__)

# 替换目标文件的重试次数与退避间隔（秒）
REPLACE_RETRIES = 3
REPLACE_RETRY_DELAY = 0.05


def atomic_write_bytes(path, data):
    """把字节串原子写入 path。

    入参：path —— 目标文件路径；data —— 要写入的 bytes。
    返回值：无；失败时抛异常（原文件保持旧内容不变）。
    """
    directory = os.path.dirname(os.path.abspath(path)) or os.getcwd()
    prefix = os.path.basename(path) + '.'
    fd, tmp_path = tempfile.mkstemp(prefix=prefix, suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        _copy_mode(path, tmp_path)
        _replace_with_retry(tmp_path, path)
    except BaseException:
        # 失败时清理临时文件，绝不把半成品留在目标目录（可能被误认为有效文件）
        _remove_quietly(tmp_path)
        raise


def atomic_write_text(path, text, encoding='utf-8', errors='strict'):
    """把文本原子写入 path（编码方式由调用方指定）。

    入参：path —— 目标文件路径；text —— 文本；encoding / errors —— 编码参数。
    返回值：无；失败时抛异常（原文件保持旧内容不变）。
    """
    atomic_write_bytes(path, text.encode(encoding, errors))


def _copy_mode(source, target):
    """尽量把源文件的权限位复制到临时文件（源文件不存在或失败时忽略）。"""
    try:
        shutil.copymode(source, target)
    except OSError:
        pass


def _replace_with_retry(tmp_path, target_path):
    """原子替换目标文件；被杀毒软件短暂占用时重试几次。"""
    last_error = None
    for attempt in range(REPLACE_RETRIES):
        try:
            os.replace(tmp_path, target_path)
            return
        except OSError as error:
            last_error = error
            logger.debug('原子替换失败（第 %d 次）：%s', attempt + 1, error)
            if attempt + 1 < REPLACE_RETRIES:
                time.sleep(REPLACE_RETRY_DELAY * (attempt + 1))
    raise last_error


def _remove_quietly(path):
    """删除临时文件；失败不抛异常（清理动作不能掩盖原始错误）。"""
    try:
        os.unlink(path)
    except OSError:
        pass
