# -*- coding: utf-8 -*-
"""下载加速链接生成器：用代理镜像前缀加速 GitHub 大文件下载的独立小工具。

与 hosts 加速的关系（为什么需要两个加速机制）：
  hosts 加速解决的是"域名解析到不可达 / 很差的 IP"，对整站生效、程序无感知；
  但 release 附件 / 源码包走的是对象存储与 CDN 的大文件链路，单靠换 IP
  提升有限。镜像代理（gh-proxy 一类）则把整条链接原样转发给代理服务器，
  由它代为下载再回传——不改系统配置、只对单条链接生效，适合偶尔下载
  大文件的场景。两个机制互不依赖、互不影响。

链接识别与自动转换（生成前的前置逻辑）：
  - 克隆链接（github.com/{user}/{repo}.git）与仓库主页（github.com/{user}/{repo}）：
    镜像代理不支持裸仓库地址（直接拼接会得到 Invalid input），自动转为
    源码包直链 github.com/{user}/{repo}/archive/refs/heads/{分支}.zip，
    分支默认 main、可在窗口切换 master（本地无法得知仓库真实默认分支）；
  - 文件直链（含 /archive/、/releases/、/raw/、/blob/ 路径）：原样拼接代理前缀；
  - 其余 GitHub 页面（issues 等）：不是文件直链，给出提示而非生成必败链接。

分层位置：纯 ui 层工具——只读用户输入、拼接字符串、调系统剪贴板 / 浏览器，
不碰 core 业务、不改任何文件；所有动作写日志便于追溯。

线程与模态说明：
  - 本窗口是非模态 Toplevel（刻意不 grab_set）：打开后主窗口仍可正常操作；
  - 剪贴板 / webbrowser 都是毫秒级本地调用，在主线程直接执行即可，
    无需走 ui.actions 后台线程。
"""

import logging
import webbrowser

import tkinter as tk
from tkinter import ttk

from core import download_helper
from ui import fonts

logger = logging.getLogger(__name__)

# 镜像节点、文件直链判定、分支选项与 URL 校验规则统一由 core.download_helper
# 维护（以前这里有一份重复的 MIRROR_NODES 和只 startswith 的校验，与 core
# 标准不一致；现在只有一处定义）。
DEFAULT_NODE = download_helper.DEFAULT_NODE

# 自动转换时在结果框上方显示的小字提示
HINT_CLONE = '检测到克隆链接，已自动转为源码包下载地址'
HINT_REPO = '检测到仓库主页链接，已自动转为源码包下载地址'

# 底部项目主页链接：指向本项目仓库（介绍 Hosts 加速 / 镜像加速的实现原理与更新）
LEARN_MORE_URL = 'https://github.com/898838605/GitHubAccelerator'
LEARN_MORE_TEXT = '项目主页：GitHubAccelerator'

# 窗口尺寸（需求约 500x300；因新增分支选择与转换提示两行，高度略放宽）
DIALOG_WIDTH = 520
DIALOG_HEIGHT = 340

# 状态提示配色（与主窗口一致：成功绿 / 错误红 / 中性灰）
COLOR_OK = '#16a34a'
COLOR_ERROR = '#dc2626'
COLOR_INFO = '#57606a'


class DownloadDialog(tk.Toplevel):
    """GitHub 文件下载加速小工具窗口（非模态，随开随关不常驻）。

    入参（构造时）：parent —— 父窗口（主窗口），用于定位与依附关系。
    使用方式：main_window 的按钮直接构造本类；重复打开由调用方做单例处理。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.title('GitHub 文件下载加速')
        self.resizable(False, False)
        self._build_ui()
        self._center_over_parent(parent)
        self.transient(parent)  # 依附主窗口（不单独出现在任务栏）
        # 刻意不调用 grab_set：本窗口是非模态的，打开后主窗口的其他
        # 操作不受影响（这是与 FormDialog 模态对话框的关键区别）
        self.url_entry.focus_set()
        logger.info('打开下载加速链接生成器')

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------

    def _build_ui(self):
        """构建窗口控件：提示 / 链接输入 / 节点选择 / 结果 / 按钮组 / 状态行 / 原理链接。

        入参：无。返回值：无。
        """
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)

        # 提示标签 + 原始链接输入框
        ttk.Label(frame, text='请粘贴 GitHub 文件原始链接（如 release 包、源码 zip）：').pack(
            anchor='w', pady=(0, 4))
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(frame, textvariable=self.url_var)
        self.url_entry.pack(fill='x', pady=(0, 8))
        # 回车即生成：粘贴链接后的最快操作路径
        self.url_entry.bind('<Return>', lambda event: self.generate())

        # 加速节点选择（只读下拉框，值来自模块顶部 MIRROR_NODES）
        node_row = ttk.Frame(frame)
        node_row.pack(fill='x', pady=(0, 8))
        ttk.Label(node_row, text='加速节点：').pack(side='left')
        self.node_var = tk.StringVar(value=DEFAULT_NODE)
        self.node_box = ttk.Combobox(node_row, textvariable=self.node_var,
                                     values=list(download_helper.MIRROR_PREFIXES),
                                     state='readonly', width=28)
        self.node_box.pack(side='left', padx=(0, 8))
        # 切换节点即用新前缀重新生成（需求：切换时与点「生成链接」同逻辑）
        self.node_box.bind('<<ComboboxSelected>>', lambda event: self.generate())

        # 转换分支选择：克隆 / 仓库链接转源码包时使用（文件直链忽略此项）
        branch_row = ttk.Frame(frame)
        branch_row.pack(fill='x', pady=(0, 8))
        ttk.Label(branch_row, text='分支：').pack(side='left')
        self.branch_var = tk.StringVar(value=download_helper.DEFAULT_BRANCH)
        self.branch_box = ttk.Combobox(branch_row, textvariable=self.branch_var,
                                       values=list(download_helper.BRANCH_CHOICES),
                                       state='readonly', width=8)
        self.branch_box.pack(side='left')
        ttk.Label(branch_row, text='（克隆 / 仓库链接转源码包时使用）').pack(side='left')
        # 切换分支立即重新生成（与节点切换同一逻辑）
        self.branch_box.bind('<<ComboboxSelected>>', lambda event: self.generate())

        # 生成结果（只读展示：内容只能由程序写入，防止误改）
        ttk.Label(frame, text='加速链接：').pack(anchor='w')
        # 转换提示小字：仅克隆 / 仓库链接被自动转换时出现（无内容时不占布局）
        self.hint_var = tk.StringVar(value='')
        self.hint_label = tk.Label(frame, textvariable=self.hint_var,
                                   anchor='w', fg=COLOR_INFO,
                                   font=fonts.ui_font(self, 9))
        self.result_var = tk.StringVar()
        self.result_entry = ttk.Entry(frame, textvariable=self.result_var)
        self.result_entry.configure(state='readonly')
        self.result_entry.pack(fill='x', pady=(0, 8))

        # 操作按钮组
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(0, 4))
        ttk.Button(buttons, text='生成链接',
                   command=self.generate).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='一键复制',
                   command=self.copy_result).pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='在浏览器中打开',
                   command=self.open_in_browser).pack(side='left')

        # 状态提示行：校验错误红字 / 操作成功绿字——用它代替弹窗提示，
        # 反馈不打断操作（复制 / 打开失败的异常路径也走这里，见各方法）
        self.status_var = tk.StringVar(value='')
        self.status_label = tk.Label(frame, textvariable=self.status_var,
                                     anchor='w', fg=COLOR_INFO)
        self.status_label.pack(fill='x', pady=(2, 0))

        # 底部原理链接（Link 样式：蓝色 + 手型光标，点击用浏览器打开）
        link = tk.Label(frame, text=LEARN_MORE_TEXT,
                        fg='#0969da', cursor='hand2')
        link.pack(anchor='e', pady=(6, 0))
        link.bind('<Button-1>', lambda event: self.open_learn_more())

    def _dialog_size(self):
        """按当前屏幕 DPI 计算对话框的物理像素尺寸（1.0 = 96 DPI，2.0 = 200%）。

        入参：无。返回值：(width, height)。
        """
        try:
            scale = max(1.0, min(3.0, self.winfo_fpixels('1i') / 96.0))
        except Exception:
            scale = 1.0
        return int(DIALOG_WIDTH * scale), int(DIALOG_HEIGHT * scale)

    def _center_over_parent(self, parent):
        """把窗口定位到主窗口客户区中央（"居中显示在主窗口上方"）。

        入参：parent —— 主窗口。返回值：无。
        """
        self.update_idletasks()  # 先让 tk 算出本窗口的实际尺寸
        width, height = self._dialog_size()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        if pw <= 1 or ph <= 1:
            # 父窗口尚未完成布局（极端情况）：退化为屏幕居中
            pw, ph = self.winfo_screenwidth(), self.winfo_screenheight()
            px = py = 0
        x = px + max(0, (pw - width) // 2)
        y = py + max(0, (ph - height) // 2)
        self.geometry(f'{width}x{height}+{x}+{y}')

    # ------------------------------------------------------------------
    # 核心动作（生成 / 复制 / 打开 / 状态反馈）
    # ------------------------------------------------------------------

    def generate(self):
        """「生成链接」与「切换节点 / 分支」共用：识别链接类型并拼接加速链接。

        入参：无（读取输入框、节点与分支下拉框）。返回值：无。

        流程：去空白 -> GitHub 合法性校验 -> 类型识别（必要时自动转换）
        -> 拼接代理前缀 -> 写结果框 + 日志。识别与转换规则见 _resolve_github_url；
        重新生成不依赖任何缓存状态（分支 / 节点切换都从输入框重新推导），
        所以"节点切换时自动转换后的链接也会用新节点重新拼接"自然成立。
        """
        url = self.url_var.get().strip()
        if not url:
            self._set_hint('')
            self._set_status(download_helper.MSG_EMPTY, ok=False)
            return
        if not download_helper.is_valid_github_url(url):
            self._set_hint('')
            self._set_status(download_helper.MSG_INVALID, ok=False)
            return
        kind, resolved = download_helper.resolve_github_url(url, self.branch_var.get())
        if kind == 'unknown':
            # 普通页面（issues / wiki 等）不是文件直链：镜像代理无法代理
            # 网页，直接拼接只会得到 Invalid input——给出提示而不是生成
            # 一条必然失败的链接。
            self._set_hint('')
            self._set_status('未识别为文件直链或仓库链接（支持 release / archive / raw / 仓库主页）',
                             ok=False)
            logger.info('未生成加速链接：无法识别的 GitHub 页面链接：%s', url)
            return
        hint = HINT_CLONE if kind == 'clone' else (HINT_REPO if kind == 'repo' else '')
        # 拼接规则由 core.download_helper 统一实现：前缀规范化为单个 / 结尾，
        # 追加的原始链接自带 https:// 是镜像站的固定调用格式，不是错误。
        ok, result = download_helper.build_accel_url(self.node_var.get(), resolved)
        if not ok:
            self._set_hint('')
            self._set_status(result, ok=False)
            return
        self.result_var.set(result)
        self._set_hint(hint)
        self._set_status('已生成加速链接，可复制或直接打开', ok=True)
        # 记录转换行为：原始 URL、识别类型（clone/repo/file）、转换后结果
        logger.info('生成下载加速链接：原始=%s，类型=%s，结果=%s', url, kind, result)

    def copy_result(self):
        """「一键复制」：把生成的加速链接复制到系统剪贴板。

        入参：无。返回值：无。
        剪贴板异常只显示红字 + 记 ERROR 日志，绝不让程序崩溃。
        """
        result = self.result_var.get().strip()
        if not result:
            self._set_status('请先生成加速链接', ok=False)
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(result)
        except Exception as error:
            logger.error('复制加速链接到剪贴板失败：%s', error)
            self._set_status(f'复制失败：{error}', ok=False)
            return
        logger.info('加速链接已复制到剪贴板：%s', result)
        self._set_status('已复制到剪贴板', ok=True)

    def open_in_browser(self):
        """「在浏览器中打开」：用默认浏览器打开生成的加速链接。

        入参：无。返回值：无。
        打开异常只显示红字 + 记 ERROR 日志，绝不让程序崩溃。
        """
        result = self.result_var.get().strip()
        if not result:
            self._set_status('请先生成加速链接', ok=False)
            return
        try:
            opened = webbrowser.open(result)
        except Exception as error:
            logger.error('打开加速链接失败：%s', error)
            self._set_status(f'打开浏览器失败：{error}', ok=False)
            return
        if not opened:
            # webbrowser 找不到可用浏览器时返回 False 而不是抛异常，单独处理
            logger.error('打开加速链接失败：未找到可用的默认浏览器')
            self._set_status('打开失败：未找到可用的默认浏览器', ok=False)
            return
        logger.info('已在浏览器中打开加速链接：%s', result)
        self._set_status('已在浏览器中打开', ok=True)

    def open_learn_more(self):
        """「项目主页」链接：用浏览器打开本项目仓库。

        入参：无。返回值：无。
        """
        try:
            opened = webbrowser.open(LEARN_MORE_URL)
        except Exception as error:
            logger.error('打开原理介绍页面失败：%s', error)
            self._set_status(f'打开浏览器失败：{error}', ok=False)
            return
        if opened:
            logger.info('已打开项目主页：%s', LEARN_MORE_URL)
        else:
            logger.error('打开项目主页失败：未找到可用的默认浏览器')
            self._set_status('打开失败：未找到可用的默认浏览器', ok=False)

    def _set_hint(self, text):
        """控制结果框上方的转换提示小字（无内容时隐藏，不占布局空间）。

        入参：text —— 提示文字，'' 表示隐藏。返回值：无。
        """
        self.hint_var.set(text)
        if text:
            # before=result_entry：重新显示时位置保证仍在结果框上方
            self.hint_label.pack(anchor='w', before=self.result_entry)
        else:
            self.hint_label.pack_forget()

    def _set_status(self, text, ok=None):
        """更新窗口内状态提示行（红字错误 / 绿字成功 / 灰字中性）。

        入参：text —— 提示文字；ok —— True 绿 / False 红 / None 灰。
        返回值：无。
        """
        self.status_var.set(text)
        if ok is True:
            color = COLOR_OK
        elif ok is False:
            color = COLOR_ERROR
        else:
            color = COLOR_INFO
        self.status_label.config(fg=color)
