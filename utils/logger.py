# -*- coding: utf-8 -*-
"""日志系统：集中初始化 logging，保证全项目共用同一套日志配置。

为什么要单独一个模块？
  - 日志配置散落在各文件里会导致格式不统一、同一行日志重复输出等问题；
  - 集中在这里后，其他任何模块只写两行代码：
        logger = logging.getLogger(__name__)
        logger.info('...')
    不需要关心日志写到哪、什么格式。

Python logging 的层级传播机制（理解了它，就明白为什么全项目只需配置一次）：
  - logging.getLogger('core.hosts_manager') 拿到的记录器是根记录器的"孩子"；
  - 子记录器默认没有自己的处理器（handler），日志会逐级向上传播（propagate）；
  - 最终统一由根记录器上挂的文件处理器写出 —— 各模块的 getLogger(__name__)
    只是"领一个孩子"，不产生第二份配置。
"""

import ctypes
import logging
import os
import tempfile
from logging.handlers import RotatingFileHandler

from utils import paths

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
LOG_FILENAME = 'app.log'      # 日志文件名（存放在程序目录）
LOG_MAX_BYTES = 512 * 1024    # 单个日志文件上限：512KB
LOG_BACKUP_COUNT = 2          # 轮转保留的历史文件个数（app.log.1 / app.log.2）

# 初始化后实际生效的日志文件路径（主窗口「打开日志文件」按钮会用到）。
# 用模块级变量而不是函数返回值传递，是因为"打开日志"按钮触发时离初始化
# 已经很远，从全局取最直接。
LOG_PATH = None


def fatal_popup(text):
    """兜底错误弹窗：用 Win32 MessageBoxW 显示（不依赖任何界面库）。

    功能：在 tkinter 窗口尚未创建、或程序在启动极早期就崩溃的场合，
    保证用户能看到错误信息，而不是"双击后一闪而过、什么都没发生"。

    入参：text —— 要显示的完整文字。
    返回值：无。
    """
    try:
        # 0x10 = MB_ICONERROR（红色叉号图标）
        ctypes.windll.user32.MessageBoxW(0, text, 'GitHub Hosts 加速', 0x10)
    except Exception:
        # 连 MessageBox 都失败时已无路可走，静默吞掉，避免二次崩溃
        pass


def setup_logging(console=False):
    """初始化全局日志配置（由 main.py 在启动最开头调用，整个生命周期只调一次）。

    入参：
      console —— bool，是否同时向控制台输出。脚本调试时可设 True；
                 打包成 --noconsole 的 exe 后没有控制台，保持 False。

    返回值：str 或 None —— 实际生效的日志文件完整路径；彻底失败时为 None。
    """
    global LOG_PATH
    root = logging.getLogger()          # 根记录器：全项目唯一的配置点
    root.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    # 依次尝试：程序目录 -> 系统临时目录。
    # 为什么要回退？如果程序被放在 Program Files 这类需要管理员权限才可写
    # 的目录而当前又没提权成功，直接写会 PermissionError——
    # 不能因为"日志写不了"就让整个工具无法启动。
    last_error = None
    for directory in (paths.get_data_dir(), tempfile.gettempdir()):
        file_path = os.path.join(directory, LOG_FILENAME)
        try:
            # RotatingFileHandler：日志超过 512KB 自动轮转成 app.log.1，
            # 最多保留 2 份历史，防止常年驻守的托盘程序把日志写爆磁盘
            handler = RotatingFileHandler(
                file_path, maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT, encoding='utf-8')
            handler.setFormatter(formatter)
            root.addHandler(handler)
            if console:
                stream = logging.StreamHandler()
                stream.setFormatter(formatter)
                root.addHandler(stream)
            LOG_PATH = file_path
            return file_path
        except Exception as error:
            last_error = error

    # 文件日志彻底不可用：至少挂一个 NullHandler 占位，避免 logging 内部
    # 再抛 "No handlers" 类异常；同时弹窗告知用户。
    root.addHandler(logging.NullHandler())
    fatal_popup(f'无法创建日志文件，日志功能不可用：{last_error}')
    return None


def get_logger(name=None):
    """获取指定名称的记录器（各模块获取日志器的标准入口）。

    入参：name —— 通常传 __name__（如 'core.hosts_manager'，自动形成
          层级化的日志分类）；传 None 返回根记录器。
    返回值：logging.Logger 对象。
    """
    return logging.getLogger(name)
