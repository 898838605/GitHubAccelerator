# -*- coding: utf-8 -*-
"""对话框：手动添加候选 / 编辑 IP 的通用模态小窗。

设计成"通用表单 + 回调"模式：
  - FormDialog 只负责画输入框、收输入、管回车 / Esc 快捷键；
  - 数据校验和后续动作全部交给 on_ok 回调 —— 对话框不知道什么叫 IP，
    复用性好（加新表单不用改对话框类）。
"""

import logging

import tkinter as tk
from tkinter import ttk, messagebox

logger = logging.getLogger(__name__)

APP_TITLE = 'GitHub Hosts 加速'   # 弹窗标题兜底（错误提示框用）


class FormDialog(tk.Toplevel):
    """通用模态小对话框：若干「标签 + 输入框」，点击确定后回调 on_ok(dialog, values)。

    入参（构造时）：
      parent       —— 父窗口（模态效果相对它）；
      title        —— 对话框标题；
      fields       —— [(键, 标签文字, 初始值), ...]，顺序即显示顺序；
      on_ok        —— 确定按钮回调 fn(dialog, values)；校验失败时弹提示并
                       不关闭对话框（由回调决定何时 dialog.destroy()）；
      extra_button —— 可选 (按钮文字, 处理函数)，处理函数接收对话框实例，
                       用于「从系统获取」这类辅助按钮（直接读写输入框）。

    快捷键：回车 = 确定；Esc = 取消。
    """

    def __init__(self, parent, title, fields, on_ok, extra_button=None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.on_ok = on_ok
        self.entries = {}                      # {键: Entry 控件}
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        for row, (key, label, initial) in enumerate(fields):
            ttk.Label(frame, text=label).grid(
                row=row, column=0, sticky='e', padx=(0, 8), pady=4)
            entry = ttk.Entry(frame, width=30)
            entry.insert(0, initial)
            entry.grid(row=row, column=1, pady=4)
            self.entries[key] = entry
        buttons = ttk.Frame(frame)
        buttons.grid(row=len(fields), column=0, columnspan=2, pady=(10, 0))
        if extra_button is not None:
            # 额外辅助按钮（如「从系统获取」）放最左边，主按钮靠右
            extra_text, extra_handler = extra_button
            ttk.Button(buttons, text=extra_text,
                       command=lambda: self._safe_call(extra_handler)).pack(
                side='left', padx=4)
        ttk.Button(buttons, text='确定', command=self._confirm).pack(side='left', padx=4)
        ttk.Button(buttons, text='取消', command=self.destroy).pack(side='left', padx=4)
        # 模态三件套：transient(依附父窗) + grab_set(独占输入) + 焦点到第一个框
        self.transient(parent)
        self.grab_set()
        first = self.entries[fields[0][0]]
        first.focus_set()
        self.bind('<Return>', lambda event: self._confirm())
        self.bind('<Escape>', lambda event: self.destroy())

    def _confirm(self):
        """确定按钮：收集输入交给 on_ok 回调（回调负责校验与关闭）。"""
        values = {key: entry.get().strip() for key, entry in self.entries.items()}
        try:
            self.on_ok(self, values)
        except Exception:
            # 校验回调里的意外错误不能炸掉对话框：记录 + 提示，窗体保留
            logger.exception('对话框确认处理异常')
            messagebox.showerror(APP_TITLE, '操作出错，详见 app.log。', parent=self)

    def _safe_call(self, handler):
        """执行辅助按钮回调；异常记录并提示，不关闭对话框。"""
        try:
            handler(self)
        except Exception:
            logger.exception('对话框按钮处理异常')
            messagebox.showerror(APP_TITLE, '操作出错，详见 app.log。', parent=self)
