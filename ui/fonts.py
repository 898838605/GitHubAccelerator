# -*- coding: utf-8 -*-
"""界面字体：优先中文字体，找不到时逐级回退。

为什么单独一个模块？
  旧代码把 'Microsoft YaHei UI' / 'Consolas' 硬编码在各个控件上：非中文版
  Windows 上这些字体可能不存在，Tk 会回退到默认字体，中文可能显示成方框，
  等宽日志区也会走样。集中在这里做"探测一次、全项目复用"。

分层说明：字体探测依赖 tkinter（tkfont），所以放在 ui 层而不是 utils 层
（utils 的约定是只依赖标准库、不碰界面库）。
"""

import logging

import tkinter.font as tkfont

logger = logging.getLogger(__name__)

# 界面正文字体候选（中文优先，其次系统通用无衬线字体）
UI_CANDIDATES = ('Microsoft YaHei UI', 'Microsoft YaHei', 'Segoe UI',
                 'Arial', 'Helvetica')
# 等宽字体候选（日志预览、代码感区域）
MONO_CANDIDATES = ('Consolas', 'Cascadia Mono', 'Cascadia Code',
                   'Courier New', 'Lucida Console')

_cache = {}


def ui_family(root):
    """返回可用的界面正文字体族名（结果缓存）。

    入参：root —— 任意 tkinter 控件/窗口（用于查询字体列表）。
    返回值：str —— 字体族名。
    """
    return _pick(root, 'ui', UI_CANDIDATES)


def mono_family(root):
    """返回可用的等宽字体族名（结果缓存）。

    入参：root —— 任意 tkinter 控件/窗口。返回值：str。
    """
    return _pick(root, 'mono', MONO_CANDIDATES)


def ui_font(root, size, bold=False):
    """构造界面正文的字体元组。

    入参：root —— 控件；size —— 字号（点）；bold —— 是否加粗。
    返回值：(family, size) 或 (family, size, 'bold')，可直接传给 tkinter。
    """
    family = ui_family(root)
    return (family, size, 'bold') if bold else (family, size)


def mono_font(root, size):
    """构造等宽字体元组（入参/返回值同 ui_font，不加粗）。"""
    return (mono_family(root), size)


def _pick(root, key, candidates):
    """在候选列表里挑第一个系统真实存在的字体；都没有则用 Tk 默认字体族。"""
    if key in _cache:
        return _cache[key]
    family = None
    try:
        available = set(tkfont.families(root))
        for name in candidates:
            if name in available:
                family = name
                break
    except Exception:
        logger.warning('查询系统字体列表失败（忽略）', exc_info=True)
    if family is None:
        try:
            # 兜底：问 Tk 自己的默认字体实际用了哪个族
            family = str(root.tk.call('font', 'actual', 'TkDefaultFont', '-family'))
        except Exception:
            family = 'TkDefaultFont'
        logger.warning('未找到候选字体，使用 Tk 默认字体族：%s', family)
    _cache[key] = family
    return family
