# -*- coding: utf-8 -*-
"""Windows 进程环境工具：管理员权限检测、UAC 提权重启、单实例互斥量。

为什么这些逻辑放在 utils 层？
  - 它们都是与业务无关的"系统机械动作"，只跟 Windows API（ctypes）打交道；
  - core 层的 hosts 读写、ui 层的窗口都不需要关心"当前有没有管理员权限"——
    由 main.py 在启动最开始统一把权限问题处理完，后面所有代码都可以
    默认自己已经是管理员，职责单一。

提供三个能力：
  1. is_admin()                    判断当前是否管理员身份；
  2. relaunch_as_admin()           非管理员时通过 UAC 弹窗重启自身拿权限；
  3. acquire_single_instance() / release_mutex()
                                   命名互斥量保证全系统只跑一个实例。
"""

import ctypes
import logging
import os
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

# 单实例命名互斥量。名字要足够独特（掺了随机后缀），避免和其他软件撞名。
# 优先使用 Global 命名空间 命名空间：这样"同一台机器上的多个用户会话"也只允许一个实例；
# 个别环境拿不到全局命名空间权限时，自动降级为当前会话的本地名字。
MUTEX_BASE_NAME = 'GitHubHostsAccelerator_Mutex_9F2C27A0'
MUTEX_NAMES = ('Global\\' + MUTEX_BASE_NAME, MUTEX_BASE_NAME)

# 重启自身时附加的命令行标记：防止"提权 -> 还是非管理员 -> 再提权"死循环
ELEVATED_FLAG = '--elevated'

# 本进程持有的互斥量句柄列表（acquire 成功后才有值，退出时 release 用）。
# Global 命名空间用于跨用户会话互斥，本地名字用于兼容升级期间仍在运行的
# 旧版本（旧版只持有本地名字）。两者都拿到才算单实例。
_mutex_handles = []


def is_admin():
    """判断当前进程是否以管理员身份运行。

    入参：无。
    返回值：bool —— True 表示是管理员；任何异常（如非 Windows 平台调试）
             返回 False。
    """
    try:
        # IsUserAnAdmin 是 shell32 的标准 API，返回 0（否）/ 1（是）
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(pending_action=None):
    """通过 UAC 以管理员身份重启自身。

    功能：调用 ShellExecuteW(None, "runas", ...) 触发 Windows UAC 弹窗；
    用户点"是"后系统启动一个新的管理员进程，本进程随即退出。

    入参：pending_action —— 可选的动作名（如 'enable'）。提权重启后，新实例
          通过命令行 --do <动作> 自动执行该操作，用户不用再点一次。
    返回值：bool —— True 表示新进程已成功拉起（调用方应立即退出当前进程）；
             False 表示用户取消 UAC 或启动失败（调用方应提示后退出）。

    为什么需要 --elevated 标记？
      如果新进程由于某种原因（组策略限制等）仍然不是管理员，没有这个标记
      它会再次尝试提权 -> 无限弹窗循环。main.py 检测到该标记且仍非管理员时，
      直接报错退出。
    """
    args = []
    if getattr(sys, 'frozen', False):
        # 打包成 exe：直接重启 exe 自身
        target_exe = sys.executable
        args.append(ELEVATED_FLAG)
    else:
        # 脚本模式：优先用 pythonw.exe 重启（避免再次弹出控制台黑窗）
        pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
        target_exe = pythonw if os.path.exists(pythonw) else sys.executable
        # sys.argv[0] 是入口脚本 main.py 的路径（不能用本模块的 __file__，
        # 那是 utils/admin.py 的路径）
        args.append(os.path.abspath(sys.argv[0]))
        args.append(ELEVATED_FLAG)
    if pending_action:
        args.extend(['--do', str(pending_action)])
    params = ' '.join(_quote_arg(value) for value in args)

    logger.info('未以管理员身份运行，正在通过 UAC 请求管理员权限并重启自身...')
    try:
        # "runas" 动词 = 以管理员运行；返回值 > 32 表示启动成功（Windows 惯例）
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, 'runas', target_exe, params, None, 1)
    except Exception as error:
        logger.exception('权限请求异常：%s', error)
        return False
    if int(ret) > 32:
        logger.info('管理员实例已启动，当前进程退出')
        return True
    logger.error('权限请求失败（ShellExecuteW 返回 %s），可能被用户取消', int(ret))
    return False


def _quote_arg(value):
    """给命令行参数加引号（Windows 规则：含空格/制表符/引号时需要）。

    入参：value —— 参数字符串。返回值：str。
    动作名只含字母下划线，不会走到转义分支；这里主要保护可能含空格/中文的路径。
    """
    if value and not any(char in value for char in ' \t"'):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def acquire_single_instance():
    """用命名互斥量保证程序全系统单实例运行。

    功能：依次尝试 Global 命名空间（跨用户会话）与本地命名空间，两者都拿到所有权
    才算成功；任意一个被占用说明已有实例在运行。

    入参：无。
    返回值：
      - 非零句柄列表：取得所有权（含接管异常退出的旧实例），程序可继续运行；
      - None：已有实例在运行，调用方应提示用户并退出；
      - 0：互斥量创建失败（降级处理：跳过单实例检查，不影响正常使用）。

    为什么用互斥量而不是"遍历进程列表 / 锁文件"？
      - 互斥量由内核保证原子性，没有竞态条件；
      - 进程崩溃时内核自动释放；即使没释放，下一个进程也能以
        WAIT_ABANDONED 状态接管，不会"锁死"；
      - "提权重启时旧进程退出、新进程接管"的交接场景天然被覆盖。

    为什么先 Global 命名空间 再本地？
      - Global 命名空间 让同一台机器上的多个登录会话也只允许一个实例（hosts 是
        全系统共享的，两个用户同时改会互相破坏）；
      - 本地名字用于兼容 v4.0 及更早版本（它们只持有本地互斥量），
        避免升级期间出现"旧实例还在跑、新实例又起来"的双开。
    """
    global _mutex_handles
    kernel32 = ctypes.windll.kernel32
    # 显式声明参数类型，避免 64 位 Python 下句柄被截断成 32 位
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.LPCWSTR]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]

    acquired = []
    # 第一个名字等 3 秒（提权交接时旧进程可能正在退出）；第二个只做 0 超时探测
    plans = ((MUTEX_NAMES[0], 3000), (MUTEX_NAMES[1], 0))
    for name, wait_ms in plans:
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            logger.warning('创建互斥量失败（name=%s, GetLastError=%s），跳过该命名空间',
                           name, kernel32.GetLastError())
            continue
        result = kernel32.WaitForSingleObject(handle, wait_ms)
        if result in (0x00000000, 0x00000080):
            # WAIT_OBJECT_0 (0)：正常取得所有权
            # WAIT_ABANDONED (0x80)：上一个持有者没释放就退出了，由本实例接管
            if result == 0x00000080:
                logger.info('旧实例未释放互斥量即退出，本实例接管所有权（%s）', name)
            acquired.append(handle)
            continue
        # 创建成功但等待超时 = 已有实例在运行
        kernel32.CloseHandle(handle)
        _release_handles(acquired)
        logger.warning('互斥量等待超时，已有实例在运行（name=%s）', name)
        return None

    if not acquired:
        logger.warning('创建互斥量失败，跳过单实例检查')
        return 0
    _mutex_handles = acquired
    logger.info('单实例互斥量已取得（%d 个命名空间）', len(acquired))
    return acquired


def _release_handles(handles):
    """释放一组互斥量句柄：先 ReleaseMutex 再 CloseHandle（失败只记日志）。"""
    kernel32 = ctypes.windll.kernel32
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    for handle in handles:
        try:
            kernel32.ReleaseMutex(handle)
        except Exception:
            logger.debug('释放互斥量所有权失败（可能已非持有者）', exc_info=True)
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            logger.debug('关闭互斥量句柄失败', exc_info=True)


def release_mutex():
    """释放单实例互斥量（正常退出时调用）。

    说明：进程结束本身也会让内核自动释放互斥量，这里显式释放只是
    "把借的还回去"的好习惯；句柄存在模块内，避免各层传来传去。

    入参：无。返回值：无。
    """
    global _mutex_handles
    if not _mutex_handles:
        return
    _release_handles(_mutex_handles)
    _mutex_handles = []
