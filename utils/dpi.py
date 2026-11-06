# -*- coding: utf-8 -*-
"""高 DPI 适配：在创建任何窗口之前声明进程的 DPI 感知级别。

为什么需要它？
  未声明 DPI 感知的程序在 125% / 150% 缩放下会被 Windows 按位图拉伸，
  界面发虚、文字模糊。声明「每显示器 DPI 感知」后由程序自己按真实 DPI
  渲染，配合 tune_tk_scaling() 调整 Tk 的缩放系数，高分屏下才清晰。

调用时机（重要）：
  enable_dpi_awareness() 必须在创建 Tk 根窗口之前调用；
  tune_tk_scaling() 必须在根窗口创建之后调用。
"""

import ctypes
import logging

logger = logging.getLogger(__name__)

# Windows 10 1703+ 的 DPI 感知上下文：每显示器 v2（支持运行时 DPI 变化）
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
# shcore.SetProcessDpiAwareness 的取值：2 = PROCESS_PER_MONITOR_DPI_AWARE
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable_dpi_awareness():
    """声明进程 DPI 感知（按 Win10 1703+ -> Win8.1 -> Win7 的顺序降级）。

    入参：无。返回值：bool —— 是否成功声明（失败只记日志，不影响运行）。
    """
    try:
        user32 = ctypes.windll.user32
        if user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            logger.info('已启用每显示器 DPI 感知（Per-Monitor V2）')
            return True
    except Exception:
        logger.debug('SetProcessDpiAwarenessContext 不可用，尝试旧接口')

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
        logger.info('已启用每显示器 DPI 感知（shcore）')
        return True
    except Exception:
        logger.debug('SetProcessDpiAwareness 不可用，继续降级')

    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            logger.info('已启用系统 DPI 感知（旧接口）')
            return True
    except Exception:
        logger.warning('DPI 感知设置失败，高分屏下界面可能发虚')
    return False


def tune_tk_scaling(root):
    """按真实屏幕 DPI 调整 Tk 缩放系数（根窗口创建后调用）。

    入参：root —— tkinter 根窗口。
    返回值：无；失败只记日志。
    """
    try:
        dpi = float(root.winfo_fpixels('1i'))
        if dpi > 0:
            root.tk.call('tk', 'scaling', dpi / 72.0)
            logger.info('Tk 缩放系数已按 DPI 调整：%.1f dpi -> %.3f', dpi, dpi / 72.0)
    except Exception:
        logger.warning('调整 Tk 缩放系数失败（不影响运行）', exc_info=True)
