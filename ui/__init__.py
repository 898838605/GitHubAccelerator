# -*- coding: utf-8 -*-
"""ui 包：界面层（tkinter 主窗口 + pystray 托盘）。

分层规则：
  1. 本包可以 import core 与 utils；core / utils 绝不 import 本包（单向依赖）；
  2. 所有耗时操作（测速 / 联网 / 读写 hosts）一律通过 ui.actions.run_action
     丢到后台线程，完成后经 root.after 回主线程刷新 —— 子线程绝不碰控件；
  3. 本包不写业务规则（什么时候备份、怎么选 IP），只做"调 core + 展示结果"。
"""
