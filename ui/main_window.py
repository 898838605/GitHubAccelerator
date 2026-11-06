# -*- coding: utf-8 -*-
"""主窗口（tkinter）：状态栏 + 操作按钮 + IP 列表管理 + 连通性结果 + 日志预览。

职责边界：本类只做"界面与交互"，不写任何业务规则——
  什么时候备份、怎么测速选优、怎么写 hosts，全部属于 core 层；
  这里做的事只有两类：
    1. 把用户动作转交给 ui.actions.run_action（后台线程执行业务函数）；
    2. 把 core.app_state 的状态事件画到控件上。

线程模型（本项目最重要的界面约定，子线程绝不直接碰控件）：
  - 耗时操作：run_action 内部起 daemon 线程，完成后广播 op_done；
  - 状态事件：state 的订阅回调可能在任意线程触发，本类的 on_state_event
    只做一件事——经 actions.post_ui（root.after）调度回 tkinter 主线程，
    真正的控件操作都在 _handle_event（主线程）里完成；
  - 每秒倒计时：root.after 定时器天然在主线程，直接刷新。
"""

import ipaddress
import logging
import os
import socket
import time

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

from PIL import ImageTk

from core import service
from core.app_state import state
from core.hosts_manager import (count_accel_entries, is_allowed_domain,
                               is_public_address, looks_like_domain,
                               looks_like_ip)
from ui import actions, fonts
from ui.dialogs import FormDialog
from ui.download_dialog import DownloadDialog
from utils import app_icon
from utils import logger as logger_module

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 界面常量（配色与托盘图标风格统一：绿=启用 / 灰=未启用）
# ---------------------------------------------------------------------------
BADGE_ON_COLOR = '#22c55e'      # 状态徽标：已启用（绿）
BADGE_OFF_COLOR = '#82878c'     # 状态徽标：未启用（灰）
LOG_BG_COLOR = '#F8F9FA'        # 日志预览背景（浅灰）
LOG_COLOR_INFO = '#57606a'      # 日志 INFO 行（深灰）
LOG_COLOR_WARNING = '#b45309'   # 日志 WARNING 行（橙）
LOG_COLOR_ERROR = '#dc2626'     # 日志 ERROR 行（红）
CONN_OK_COLOR = '#16a34a'       # 连通性汇总：正常（绿）
CONN_BAD_COLOR = '#dc2626'      # 连通性汇总：异常（红）
CONN_IDLE_COLOR = '#6b7280'     # 连通性汇总：未测试（灰）
DEFAULT_WINDOW_WIDTH = 720      # 无配置记录时的默认窗口宽
DEFAULT_WINDOW_HEIGHT = 560     # 无配置记录时的默认窗口高
LOG_PREVIEW_LINES = 200         # 日志预览读取的最近行数


def _pairs_for_items(key_to_item, selected_items):
    """把 Treeview 行 ID 反查成候选 (IP, 域名) 列表。

    入参：key_to_item —— {(IP, 域名): 行ID}；selected_items —— 选中的行ID序列。
    返回值：list[(ip, domain)]（按选中顺序，找不到的行忽略）。

    注意方向：内部映射是「候选 -> 行ID」，而 Treeview.selection() 给的是行ID，
    所以这里必须反查。v5.1.1 之前直接拿行ID去索引 key_to_item，导致
    "明明选中了行，程序却提示请先选择" —— 这个纯函数把那一步单独抽出来，
    方便用单元测试守住方向。
    """
    reverse = {item_id: key for key, item_id in key_to_item.items()}
    return [reverse[item_id] for item_id in selected_items if item_id in reverse]


def _fmt_countdown(seconds):
    """把剩余秒数格式化为 HH:MM:SS（负数按 0 处理）。

    入参：seconds —— 剩余秒数（float/int，可为负）。
    返回值：str，形如 '05:59:01'。
    """
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


class AppWindow:
    """主窗口：全部界面控件的构建与状态事件的展示侧实现。

    入参（构造时）：root —— tkinter 根窗口；title —— 窗口标题；
          config —— 运行期配置 dict（恢复窗口尺寸位置用）。

    生命周期约定：本类与根窗口同生共死，不单独 destroy；
    关闭按钮被重定向为 hide()（隐藏到托盘），真正退出由托盘「退出」驱动。
    """

    def __init__(self, root, title, config):
        self.root = root
        self._config = config
        # UI 缩放系数：声明 DPI 感知后，Tk 以物理像素工作（200% 缩放 -> 2.0）。
        # 字体由 tk scaling 自动放大，但窗口尺寸 / 列宽这些"像素单位"必须手动乘系数，
        # 否则高分屏下会出现"字很大、列很窄"的错位。
        self._scale = self._calc_ui_scale()
        root.title(title)
        root.minsize(int(640 * self._scale), int(480 * self._scale))
        self._op_buttons = []       # [(按钮, 原始文字)]：忙状态时统一禁用
        self._busy_text = '处理中…'
        self._key_to_item = {}      # {(IP, 域名): Treeview行ID}，增量刷新与选中项解析
        # 事件合并（P2-7）：一次操作会连续发布多个事件，合并成一次界面刷新
        self._pending_events = set()
        self._event_payloads = {}
        self._flush_scheduled = False
        self._log_signature = None  # 日志文件 (size, mtime) 签名，未变化则不重读
        self._sort_column = None    # 当前排序列（None=按文件原序）
        self._sort_reverse = False  # 是否降序
        self._headings = {'ip': 'IP', 'domain': '域名',
                          'latency': '上次测速延迟(ms)', 'status': '状态'}
        self._icon_photo = None     # iconphoto 图像引用（防垃圾回收）
        self._apply_geometry()
        self._build_ui()
        root.protocol('WM_DELETE_WINDOW', self.hide)
        root.bind('<Configure>', self.on_configure)
        state.subscribe(self.on_state_event)
        # 初始界面数据（构造完成后立即可见，先填一次）
        self.refresh_status()
        self.refresh_accel_count()
        self.refresh_tree()
        self.refresh_connectivity_view()
        self.refresh_log()
        self.tick()  # 启动每秒一次的倒计时刷新
        # 按 DPI 重新分配三个区域的初始高度（见 _set_initial_sashes 的注释）
        self.root.after(150, self._set_initial_sashes)

    # ---------- 布局 ----------

    def _calc_ui_scale(self):
        """计算当前屏幕的 UI 缩放系数（1.0 = 96 DPI，2.0 = 200% 缩放）。

        入参：无。返回值：float，限制在 [1.0, 3.0]。
        """
        try:
            return max(1.0, min(3.0, self.root.winfo_fpixels('1i') / 96.0))
        except Exception:
            logger.warning('读取屏幕 DPI 失败，按 100%% 缩放处理')
            return 1.0

    def _configure_tree_style(self):
        """按当前 DPI 配置 Treeview 的行高与表头字体。

        为什么需要？ttk.Treeview 的默认行高是**固定像素**（主题里写死约 20px），
        不会跟随 tk scaling 放大。声明 DPI 感知后字体变成 1.x~2.x 倍，
        行高却还是 20px，文字就会被上下裁切（高分屏上表现得尤其明显）。
        这里用实际字体的 linespace 计算行高，并同步放大表头字体。
        """
        style = ttk.Style(self.root)
        try:
            line_height = int(tkfont.nametofont('TkDefaultFont')
                              .metrics('linespace'))
        except Exception:
            line_height = int(20 * self._scale)
        row_height = max(int(22 * self._scale),
                         line_height + int(4 * self._scale))
        style.configure('Treeview', rowheight=row_height)
        style.configure('Treeview.Heading',
                        font=fonts.ui_font(self.root, 10, bold=True))
        logger.debug('Treeview 行高配置：%dpx（字体 linespace=%d）',
                     row_height, line_height)

    def _set_initial_sashes(self, attempts=3):
        """按缩放比例设置「表格 / 连通性 / 日志」三个区域的初始高度。

        为什么要手动设置？ttk.Panedwindow 会优先满足子控件的"请求尺寸"，
        而日志区 Text 请求 8 行高度、表格请求 10 行高度，在高 DPI 下两者
        之和会超过可用空间，连通性区被挤到只剩一行、第二行被裁切。
        这里按最小可用高度重新分配一次；之后用户仍可自由拖动分隔条。
        只执行一次（窗口 resize 不会再覆盖用户拖动的结果）。
        """
        if getattr(self, '_sashes_ready', False):
            return
        try:
            total = self._paned.winfo_height()
            if total <= 1:
                if attempts > 0:
                    self.root.after(150, lambda: self._set_initial_sashes(attempts - 1))
                return
            conn_min = int(150 * self._scale)   # 连通性区（两行结果 + 代理行 + 标题）最小高度
            log_min = int(140 * self._scale)    # 日志预览区最小高度
            first = max(int(160 * self._scale),
                        min(int(total * 0.45), total - conn_min - log_min))
            second = min(max(first + conn_min, int(total * 0.63)), total - log_min)
            self._paned.sashpos(0, first)
            self._paned.sashpos(1, max(first + 10, second))
            self._sashes_ready = True
            logger.debug('初始分栏：total=%d first=%d second=%d', total, first, second)
        except Exception:
            logger.debug('设置初始分栏失败（忽略）', exc_info=True)

    def _apply_geometry(self):
        """按 config.json 恢复窗口尺寸位置（无配置则 720x560 居中）。

        入参：无。返回值：无。
        """
        # 用「虚拟桌面」范围而不是主屏尺寸：多显示器时副屏坐标可能是负数，
        # 直接 max(0, ...) 会把窗口强行拉回主屏（用户每次启动都要重拖）。
        vroot_x, vroot_y = self.root.winfo_vrootx(), self.root.winfo_vrooty()
        vroot_w = self.root.winfo_vrootwidth() or self.root.winfo_screenwidth()
        vroot_h = self.root.winfo_vrootheight() or self.root.winfo_screenheight()

        # config 里保存的是"保存时缩放下的物理尺寸"。旧版本没有 ui_scale 字段
        # （按 96 DPI 记录），因此这里按 saved_scale -> 当前 scale 做一次换算，
        # 既兼容旧配置，也避免每次启动都重复放大。
        saved_scale = self._config.get('ui_scale') or 1.0
        saved_scale = saved_scale if saved_scale > 0 else 1.0
        factor = self._scale / saved_scale
        width = int((self._config.get('window_width') or DEFAULT_WINDOW_WIDTH) * factor)
        height = int((self._config.get('window_height') or DEFAULT_WINDOW_HEIGHT) * factor)
        width = max(320, min(width, vroot_w))
        height = max(240, min(height, vroot_h))
        self.root.geometry(f'{width}x{height}')
        x = self._config.get('window_x')
        y = self._config.get('window_y')
        if x is not None:
            x = int(x * factor)
        if y is not None:
            y = int(y * factor)
        if x is None or y is None:  # 无记录则居中偏上显示
            x = vroot_x + max(0, (vroot_w - width) // 2)
            y = vroot_y + max(0, (vroot_h - height) // 3)
        # 只保证标题栏与至少 200px 宽度可见：避免拔掉副显示器后窗口"藏"在
        # 不可达区域找不回来，同时不动用户在多屏上的正常位置。
        x = max(vroot_x - width + 200, min(x, vroot_x + vroot_w - 200))
        y = max(vroot_y, min(y, vroot_y + vroot_h - 100))
        self.root.geometry(f'+{x}+{y}')

    def _build_ui(self):
        """构建全部界面区域（A 状态栏 / B 按钮区 / C 列表区 / D 连通性 / E 日志）。

        入参：无。返回值：无。
        """
        # A. 顶部状态栏：状态徽标 + 生效条目数
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill='x')
        self.status_label = tk.Label(top, text='未启用', fg='white',
                                     bg=BADGE_OFF_COLOR, padx=14, pady=2,
                                     font=fonts.ui_font(self.root, 10, bold=True))
        self.status_label.pack(side='left')
        self.accel_count_var = tk.StringVar(value='生效条目：0')
        ttk.Label(top, textvariable=self.accel_count_var).pack(side='right')

        # 状态栏第二行：上次刷新时间 / 下次定时刷新倒计时（每秒更新）
        self.info_var = tk.StringVar(value='')
        ttk.Label(self.root, textvariable=self.info_var, padding=(12, 0)).pack(fill='x')

        # B. 操作按钮区（水平排列；全部经统一调度器执行）
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill='x')
        for text, action in (('启用加速', service.enable),
                             ('停用加速', service.disable),
                             ('强制还原原始 hosts', service.force_restore),
                             ('更新 IP 列表', service.update_ip_list_online),
                             ('测试连通性', service.test_connectivity)):
            btn = ttk.Button(bar, text=text)
            btn.configure(command=lambda a=action, b=btn: self.on_button(a, b))
            btn.pack(side='left', padx=(0, 8))
            self._op_buttons.append((btn, text))

        # 工具按钮（右置）：即时界面动作，不经 run_action 调度——剪贴板 /
        # webbrowser 都是毫秒级本地调用，无需后台线程。也刻意不加入
        # _op_buttons：它没有"处理中"状态，业务操作忙时也必须能点开。
        tool_btn = ttk.Button(bar, text='🔗 下载加速链接生成',
                              command=self.open_download_dialog)
        tool_btn.pack(side='right')
        # 「取消」按钮：只在有长任务时可用，用于打断测速 / TLS 校验 / 在线更新
        self.cancel_btn = ttk.Button(bar, text='取消', command=self.cancel_current,
                                     state='disabled')
        self.cancel_btn.pack(side='right', padx=(0, 8))

        # C / D / E：垂直可拖拽分割的三个区域
        paned = ttk.Panedwindow(self.root, orient='vertical')
        paned.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self._paned = paned
        # 进度条：空闲时隐藏（不占布局），操作进行中显示在按钮区和内容区之间
        self.progress_bar = ttk.Progressbar(self.root, mode='determinate', maximum=100)

        # C. IP 列表管理区
        ip_frame = ttk.Frame(paned)
        paned.add(ip_frame, weight=5)
        toolbar = ttk.Frame(ip_frame)
        toolbar.pack(fill='x', pady=(0, 6))
        # 注意：这五个 command 是"窗口交互方法"（先在主线程做选中检查 / 确认
        # 弹窗等交互，再由方法内部调 run_action 把业务交给后台线程）。
        # 绝不能再经 on_button 包一层 —— run_action 不可重入：外层已持有
        # op_lock，方法内部再次 run_action 会抢锁失败、操作被"请稍候"吞掉
        # （这正是"全部测速 / 测速选中项点了没反应"的根因）。
        for text, cmd in (('刷新列表', self.refresh_candidates),
                          ('全部测速', self.test_all),
                          ('测速选中项', self.test_selected),
                          ('删除选中项', self.remove_selected),
                          ('手动添加', self.open_add_dialog)):
            btn = ttk.Button(toolbar, text=text)
            # 按钮引用传给交互方法，方法内部转交 run_action——忙状态文字
            # 因此能落在被点击的按钮上
            btn.configure(command=lambda c=cmd, b=btn: c(b))
            btn.pack(side='left', padx=(0, 8))
            self._op_buttons.append((btn, text))
        self.count_var = tk.StringVar(value='共 0 条候选，0 条可用')
        ttk.Label(ip_frame, textvariable=self.count_var).pack(side='bottom', fill='x')
        tree_box = ttk.Frame(ip_frame)
        tree_box.pack(fill='both', expand=True)
        columns = ('ip', 'domain', 'latency', 'status')
        self.tree = ttk.Treeview(tree_box, columns=columns, show='headings',
                                 selectmode='extended')
        for col, text in self._headings.items():
            # 表头点击排序：按列切换升 / 降序（延迟按数值、IP 按段比较）
            self.tree.heading(col, text=text,
                              command=lambda c=col: self.on_header_click(c))
        # 列宽是像素单位，必须按 DPI 缩放系数放大（字体由 tk scaling 自动放大）
        scale = self._scale
        self.tree.column('ip', width=int(150 * scale), anchor='w')
        self.tree.column('domain', width=int(240 * scale), anchor='w')
        self.tree.column('latency', width=int(120 * scale), anchor='center')
        self.tree.column('status', width=int(80 * scale), anchor='center')
        vsb = ttk.Scrollbar(tree_box, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='left', fill='y')
        self._configure_tree_style()
        self.tree.bind('<Double-1>', self.on_double_edit)
        self.tree.bind('<Button-3>', self.on_right_click)  # 右键菜单
        # 右键菜单只创建一次并复用：tkinter 的 Menu 会被父控件持有引用，
        # 每次右键新建而不销毁会造成 root.children 持续增长（内存/Tcl 泄漏）。
        self._context_menu = self._build_context_menu()

        # D. 连通性测试结果区（紧凑布局 + 顶部大字状态汇总）
        conn_frame = ttk.Labelframe(paned, text='连通性测试结果', padding=(8, 2))
        paned.add(conn_frame, weight=2)  # 高 DPI 下字体更大，连通性区需要更多高度
        self.conn_summary_var = tk.StringVar(value='尚未测试')
        self.conn_summary_label = tk.Label(
            conn_frame, textvariable=self.conn_summary_var, anchor='w',
            font=fonts.ui_font(self.root, 12, bold=True), fg=CONN_IDLE_COLOR)
        self.conn_summary_label.pack(fill='x', pady=(2, 0))
        self.conn_vars = {key: tk.StringVar(value='—')
                          for key in ('github', 'raw', 'overall', 'time', 'proxy')}
        grid = ttk.Frame(conn_frame)
        grid.pack(fill='x')
        layout = (('github', 'github.com'), ('raw', 'raw'),
                  ('overall', '整体'), ('time', '测试时间'))
        for index, (key, label) in enumerate(layout):
            row, col = index // 2, index % 2
            ttk.Label(grid, text=label + '：').grid(
                row=row, column=col * 2, sticky='e', padx=4, pady=1)
            ttk.Label(grid, textvariable=self.conn_vars[key]).grid(
                row=row, column=col * 2 + 1, sticky='w', padx=4, pady=1)
        # 代理检测结果（hosts 加速在代理网络下可能不适用，单独一行提示）
        ttk.Label(grid, text='代理：').grid(
            row=2, column=0, sticky='e', padx=4, pady=1)
        ttk.Label(grid, textvariable=self.conn_vars['proxy']).grid(
            row=2, column=1, columnspan=3, sticky='w', padx=4, pady=1)

        # E. 日志预览区（浅灰底 + 等宽字体 + 分级着色）
        log_frame = ttk.Labelframe(paned, text='日志预览（最近 200 行）', padding=4)
        paned.add(log_frame, weight=3)
        log_top = ttk.Frame(log_frame)
        log_top.pack(fill='x')
        ttk.Button(log_top, text='刷新日志', command=self.refresh_log).pack(side='right', padx=2)
        ttk.Button(log_top, text='打开日志文件',
                   command=self._open_log_file).pack(side='right', padx=2)
        self.log_text = tk.Text(log_frame, height=8, wrap='none', state='disabled',
                                bg=LOG_BG_COLOR, fg='#24292e', relief='flat',
                                font=fonts.mono_font(self.root, 10))
        # 分级高亮 tag：INFO 深灰 / WARNING 橙 / ERROR 红 / 其他默认
        self.log_text.tag_configure('info', foreground=LOG_COLOR_INFO)
        self.log_text.tag_configure('warning', foreground=LOG_COLOR_WARNING)
        self.log_text.tag_configure('error', foreground=LOG_COLOR_ERROR)
        self.log_text.tag_configure('other', foreground='#24292e')
        log_scroll = ttk.Scrollbar(log_frame, orient='vertical',
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(fill='both', expand=True)
        log_scroll.pack(side='left', fill='y')

    # ---------- 状态事件（AppState 订阅侧） ----------

    def on_state_event(self, event, payload=None):
        """AppState 订阅回调（可能在任何线程触发）：记录事件并安排一次合并刷新。

        入参：event —— 事件名；payload —— 事件数据。
        返回值：无。

        为什么合并？一次「启用」会连续发布 candidates_changed / accel_changed /
        enabled / message / op_done 等事件，逐个刷新会重复重建表格、重复读日志。
        现在只登记事件，30ms 内的事件会在主线程合并成一次最小刷新。
        """
        self._pending_events.add(event)
        if event in ('progress', 'connectivity'):
            self._event_payloads[event] = payload
        if not self._flush_scheduled:
            self._flush_scheduled = True
            actions.post_ui(self._schedule_flush)

    def _schedule_flush(self):
        """（主线程）稍后统一处理本批事件，给突发更新一个合并窗口。"""
        self.root.after(30, self._flush_events)

    def _flush_events(self):
        """（主线程）把累积事件合并成一次最小刷新集合。

        入参：无。返回值：无。
        任何异常只记日志，绝不能因为一次界面刷新失败拖垮事件发布方。
        """
        self._flush_scheduled = False
        events = self._pending_events
        payloads = self._event_payloads
        self._pending_events = set()
        self._event_payloads = {}
        try:
            refresh_all = 'op_done' in events
            if 'progress' in events:
                self.refresh_progress(payloads.get('progress'))
            if refresh_all or 'enabled' in events:
                self.refresh_status()
            if refresh_all or events & {'enabled', 'accel_changed'}:
                self.refresh_accel_count()
            if refresh_all or 'candidates_changed' in events:
                self.refresh_tree()
            if refresh_all or 'connectivity' in events:
                self.refresh_connectivity_view()
            if refresh_all or 'connectivity' in events:
                self.refresh_log()
            if refresh_all or events & {'enabled', 'message', 'refresh_time'}:
                self.update_info_line()
        except Exception:
            logger.exception('处理状态事件失败：%s', sorted(events))

    def cancel_current(self):
        """「取消」按钮回调：请求取消当前后台操作（真正的停止点由 core 检查）。

        入参：无。返回值：无。
        """
        actions.request_cancel()

    def refresh_progress(self, payload=None):
        """按 progress 事件显示 / 更新 / 隐藏进度条。

        入参：payload —— {'text','done','total'} 或 None。
        返回值：无。
        total>0 时显示确定进度，否则用无限滚动条表示"不确定耗时"。
        """
        if not payload:
            self.progress_bar.stop()
            self.progress_bar.pack_forget()
            return
        done = int(payload.get('done') or 0)
        total = int(payload.get('total') or 0)
        if not self.progress_bar.winfo_manager():
            self.progress_bar.pack(fill='x', padx=10, pady=(0, 6),
                                   before=self._paned)
        if total > 0:
            if str(self.progress_bar.cget('mode')) != 'determinate':
                self.progress_bar.stop()
                self.progress_bar.configure(mode='determinate')
            self.progress_bar['value'] = max(0.0, min(100.0, done * 100.0 / total))
        else:
            if str(self.progress_bar.cget('mode')) != 'indeterminate':
                self.progress_bar.configure(mode='indeterminate')
                self.progress_bar.start(15)

    # ---------- 状态栏 ----------

    def refresh_status(self):
        """刷新顶部状态徽标与主窗口图标（绿=已启用 / 灰=未启用，风格统一）。

        入参：无。返回值：无。
        """
        if state.enabled:
            self.status_label.config(text='已启用', bg=BADGE_ON_COLOR)
            self._set_window_icon('on')
        else:
            self.status_label.config(text='未启用', bg=BADGE_OFF_COLOR)
            self._set_window_icon('off')

    def _set_window_icon(self, variant):
        """设置主窗口 iconphoto（左上角与默认任务栏图标；随启用状态换状态点颜色）。

        入参：variant —— 'on' / 'off'。返回值：无。
        """
        try:
            photo = ImageTk.PhotoImage(app_icon.make_app_icon(variant))
            self._icon_photo = photo  # 持有引用，防止 PhotoImage 被垃圾回收
            self.root.iconphoto(True, photo)
        except Exception:
            logger.exception('设置主窗口图标失败')

    def refresh_accel_count(self):
        """刷新"生效条目"显示（读取 hosts 加速段统计）。

        入参：无。返回值：无。
        """
        self.accel_count_var.set(f'生效条目：{count_accel_entries()}')

    def tick(self):
        """每秒定时器（root.after 主线程调度，不开线程）：刷新信息行并重新调度。

        入参：无。返回值：无（自身周期性执行）。
        """
        try:
            self.update_info_line()
        except Exception:
            logger.exception('刷新状态栏失败')
        finally:
            self.root.after(1000, self.tick)  # 每秒刷新一次（主线程定时器）

    def update_info_line(self):
        """刷新状态栏第二行：上次刷新时间、下次定时刷新倒计时、最近提示消息。

        入参：无。返回值：无。
        （刻意与 tick 分开：op_done 等事件也只调这里，避免重复挂 after 链。）
        """
        if state.last_refresh_time:
            last = time.strftime('%H:%M:%S', time.localtime(state.last_refresh_time))
        else:
            last = '—'
        if state.enabled and state.next_refresh_time:
            remain = state.next_refresh_time - time.time()
            nxt = _fmt_countdown(remain) if remain > 0 else '即将执行'
        else:
            nxt = '—（未启用）'
        text = f'上次刷新：{last}    下次定时刷新：{nxt}'
        if not getattr(state, 'is_admin', False):
            text += '    [受限模式：启用/停用需管理员权限]'
        if state.last_message:
            text += f'    [{state.last_message}]'
        self.info_var.set(text)

    # ---------- IP 列表区 ----------

    def refresh_tree(self):
        """增量刷新表格：只更新变化的行，保留选中项与滚动位置。

        入参：无。返回值：无。
        为什么改增量？旧实现每次 delete + insert 全部行：一次操作会触发多次
        刷新，选中项和滚动位置都会丢；候选上百条时还会明显卡顿。
        现在按 (IP, 域名) 做稳定主键：新增行插入、删除行移除、其余行只在
        显示值变化时原地更新（行 ID 不变，选中状态天然保留）。
        """
        yview = self.tree.yview()  # 刷新前记住滚动位置
        desired = []               # [((ip, domain), (ip, domain, latency, status))]
        available = 0
        for ip, domain in state.candidates:
            if ip in state.ip_latency:
                ms, err = state.ip_latency[ip]
                if ms is not None:
                    latency, status = str(ms), '可用'
                    available += 1
                else:
                    latency = '—'
                    # 失败原因已由 network_tester 归一化为中文
                    status = str(err) if err else '失败'
            else:
                latency, status = '—', '未测'
            desired.append(((ip, domain), (ip, domain, latency, status)))

        # 1) 删除已不在候选列表中的行
        desired_keys = {key for key, _values in desired}
        for key, item_id in list(self._key_to_item.items()):
            if key not in desired_keys:
                self.tree.delete(item_id)
                del self._key_to_item[key]

        # 2) 新增 / 就地更新（值没变化的行完全不动，避免整表闪烁）
        for key, values in desired:
            item_id = self._key_to_item.get(key)
            if item_id is None:
                item_id = self.tree.insert('', 'end', values=values)
                self._key_to_item[key] = item_id
                continue
            current = tuple(str(value) for value in self.tree.item(item_id, 'values'))
            wanted = tuple(str(value) for value in values)
            if current != wanted:
                self.tree.item(item_id, values=values)

        self.count_var.set(f'共 {len(state.candidates)} 条候选，{available} 条可用')
        if self._sort_column:
            self._apply_sort()  # 数据刷新后保持当前排序
        if yview:
            try:
                self.tree.yview_moveto(yview[0])
            except Exception:
                logger.debug('恢复表格滚动位置失败（忽略）', exc_info=True)

    # ---------- 表头排序 ----------

    def on_header_click(self, column):
        """表头点击排序：同列再次点击切换升 / 降序，切换列时默认升序。

        入参：column —— 列标识（'ip' / 'domain' / 'latency' / 'status'）。
        返回值：无。
        """
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._apply_sort()

    def _sort_key(self, item_id):
        """生成排序键（方向感知，恒按升序排，方向写进键值）。

        入参：item_id —— Treeview 行 ID。
        返回值：可比较的排序键（float / tuple / str）。

        - 延迟列：按数值；未测 / 失败条目无论升降序恒排最后；
        - IP 列：按各段数值比较（避免字符串序导致 9.x > 100.x）；
        - 文本列（域名 / 状态）：小写字符串，方向由 reverse 控制。
        """
        index = ('ip', 'domain', 'latency', 'status').index(self._sort_column)
        text = str(self.tree.item(item_id, 'values')[index])
        if self._sort_column == 'latency':
            if not text.isdigit():
                return float('inf')  # 未测 / 失败恒排最后
            value = float(text)
            return -value if self._sort_reverse else value
        if self._sort_column == 'ip':
            # 用 ipaddress 做数值排序：IPv4 / IPv6 混排也正确（IPv4 在前）
            try:
                address = ipaddress.ip_address(text)
                key = (address.version, int(address))
            except ValueError:
                key = (0, 0)
            if self._sort_reverse:
                key = (-key[0], -key[1])
            return key
        return text.lower()

    def _apply_sort(self):
        """按当前排序列重排表格行，并在表头文字上显示 ▲ / ▼ 指示。

        入参：无。返回值：无。
        """
        children = list(self.tree.get_children())
        if self._sort_column in ('latency', 'ip'):
            children.sort(key=self._sort_key)  # 方向已写入键值
        else:
            children.sort(key=self._sort_key, reverse=self._sort_reverse)
        for index, item_id in enumerate(children):
            self.tree.move(item_id, '', index)
        for col, base in self._headings.items():
            arrow = ''
            if col == self._sort_column:
                arrow = ' ▼' if self._sort_reverse else ' ▲'
            self.tree.heading(col, text=base + arrow)

    def refresh_candidates(self, button=None):
        """「刷新列表」：后台重读 github_hosts.txt，刷新表格与日志预览。

        入参：button —— 触发的按钮（忙状态显示用；无按钮调用传 None）。
        返回值：无。

        经 run_action 调度（与其他操作共用互斥与忙状态语义）；重建表格由
        candidates_changed 事件回主线程完成，日志预览由 op_done 全量刷新兜底。
        """
        actions.run_action(service.reload_candidates_state, button)

    def _selected_pairs(self):
        """取表格当前选中的 (IP, 域名) 列表。

        入参：无。返回值：list[(ip, domain)]（可能为空）。
        """
        return _pairs_for_items(self._key_to_item, self.tree.selection())

    def _build_context_menu(self):
        """构建（并在整个窗口生命周期内复用）表格右键菜单。

        入参：无。返回值：tk.Menu。
        菜单命令统一从"当前选中行"取数据，因此复用同一个菜单是安全的，
        也避免了每次右键都新建控件造成的泄漏。
        """
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='仅测速此项', command=self.ctx_test_one)
        menu.add_command(label='复制 IP', command=self.ctx_copy_ip)
        menu.add_separator()
        menu.add_command(label='删除此项', command=self.ctx_delete_one)
        return menu

    def on_right_click(self, event):
        """表格右键菜单：仅测速此项 / 复制 IP / 删除此项。

        入参：event —— tkinter 事件（含坐标）。返回值：无。
        """
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return  # 空白处右键不弹菜单
        if item_id not in self.tree.selection():
            self.tree.selection_set(item_id)  # 右键未选中行时先选中它
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()

    def ctx_test_one(self):
        """右键「仅测速此项」：测速当前选中的条目（不修改 hosts）。

        入参：无。返回值：无。
        """
        pairs = self._selected_pairs()
        if not pairs:
            return
        actions.run_action(lambda: service.test_selected(pairs))

    def ctx_copy_ip(self):
        """右键「复制 IP」：把选中条目的 IP（多选时逐行）复制到系统剪贴板。

        入参：无。返回值：无。
        """
        pairs = self._selected_pairs()
        if not pairs:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append('\n'.join(ip for ip, _ in pairs))
        state.set_message(f'已复制 {len(pairs)} 个 IP 到剪贴板')

    def ctx_delete_one(self):
        """右键「删除此项」：与「删除选中项」一致（带确认）。

        入参：无。返回值：无。
        """
        self.remove_selected()

    def test_all(self, button=None):
        """「全部测速」：后台并发测速所有候选 IP 并刷新表格（不修改 hosts）。

        入参：button —— 触发的按钮（忙状态显示用）。返回值：无。
        （本方法是交互入口，业务经 run_action 交给后台线程；不能再被
          on_button 包一层，见 _build_ui 工具条处的注释。）
        """
        actions.run_action(service.test_all, button)

    def test_selected(self, button=None):
        """「测速选中项」：后台测速所选条目对应的 IP（不修改 hosts）。

        入参：button —— 触发的按钮（忙状态显示用）。返回值：无。
        （选中检查与提示在主线程完成；业务经 run_action 交给后台线程。）
        """
        pairs = self._selected_pairs()
        if not pairs:
            messagebox.showinfo('测速选中项', '请先在表格中选择要测速的条目。')
            return
        actions.run_action(lambda: service.test_selected(pairs), button)

    def remove_selected(self, button=None):
        """「删除选中项」：确认后从 github_hosts.txt 删除所选条目。

        入参：button —— 触发的按钮（忙状态显示用；右键菜单调用传 None）。
        返回值：无。
        （确认弹窗在主线程；删除业务经 run_action 交给后台线程。）
        """
        pairs = self._selected_pairs()
        if not pairs:
            messagebox.showinfo('删除选中项', '请先在表格中选择要删除的条目。')
            return
        if not messagebox.askyesno('删除选中项', f'确定删除选中的 {len(pairs)} 条候选？'):
            return
        actions.run_action(lambda: service.remove_candidates(pairs), button)

    def open_add_dialog(self, button=None):
        """「手动添加」：弹出输入 IP 和域名的小对话框（含「从系统获取」）。

        入参：button —— 触发的按钮（确认添加后忙状态显示用）。返回值：无。
        （对话框必须在主线程构建——tkinter 控件不允许子线程触碰。）
        """
        def on_ok(dialog, values):
            ip = values.get('ip', '')
            domain = values.get('domain', '')
            if not looks_like_ip(ip):
                messagebox.showwarning('手动添加',
                                       'IP 格式不正确，请输入合法 IPv4 / IPv6 地址。',
                                       parent=dialog)
                return
            if not is_public_address(ip):
                messagebox.showwarning(
                    '手动添加',
                    '只支持公网 IP：私网 / 回环 / 保留地址写进 hosts 会导致解析异常。',
                    parent=dialog)
                return
            if not looks_like_domain(domain):
                messagebox.showwarning('手动添加', '域名格式不正确。',
                                       parent=dialog)
                return
            if not is_allowed_domain(domain):
                messagebox.showwarning(
                    '手动添加',
                    '只支持 GitHub 相关域名（github.com / githubusercontent.com 等）。',
                    parent=dialog)
                return
            dialog.destroy()
            actions.run_action(lambda: service.add_candidate(ip, domain), button)

        def fetch_from_system(dialog):
            """「从系统获取」：用系统 DNS 解析 github.com 的当前 IP，填入 IP 输入框。

            入参：dialog —— FormDialog 实例（直接读写其输入框）。返回值：无。

            解析走系统 hosts（若加速已启用则得到优选 IP），方便快速补录条目。
            """
            try:
                ip = socket.gethostbyname('github.com')
            except Exception as err:
                messagebox.showwarning('从系统获取',
                                       f'获取系统解析 IP 失败：{err}', parent=dialog)
                return
            dialog.entries['ip'].delete(0, 'end')
            dialog.entries['ip'].insert(0, ip)
            state.set_message(f'已获取 github.com 当前解析 IP：{ip}')

        FormDialog(self.root, '手动添加候选',
                   [('ip', 'IP 地址：', ''), ('domain', '域名：', '')], on_ok,
                   extra_button=('从系统获取', fetch_from_system))

    def on_double_edit(self, event):
        """双击表格行：弹出小对话框编辑该条目的 IP。

        入参：event —— tkinter 事件。返回值：无。
        """
        pairs = self._selected_pairs()
        if len(pairs) != 1:
            return  # 多选 / 未选中不响应双击编辑
        old_ip, old_domain = pairs[0]

        def on_ok(dialog, values):
            new_ip = values.get('ip', '')
            if not looks_like_ip(new_ip):
                messagebox.showwarning('编辑 IP',
                                       'IP 格式不正确，请输入合法 IPv4 / IPv6 地址。',
                                       parent=dialog)
                return
            if not is_public_address(new_ip):
                messagebox.showwarning(
                    '编辑 IP',
                    '只支持公网 IP：私网 / 回环 / 保留地址不能写入 hosts。',
                    parent=dialog)
                return
            dialog.destroy()
            actions.run_action(
                lambda: service.edit_candidate_ip(old_ip, old_domain, new_ip))

        FormDialog(self.root, '编辑 IP',
                   [('ip', f'新 IP（{old_domain}）：', old_ip)], on_ok)

    # ---------- 连通性结果区 ----------

    def refresh_connectivity_view(self):
        """刷新连通性测试结果区：详情字段 + 大字状态汇总（标绿 / 标红）。

        入参：无。返回值：无。
        """
        data = state.last_connectivity
        if not data:
            return
        self.conn_vars['github'].set(data.get('github', '—'))
        self.conn_vars['raw'].set(data.get('raw', '—'))
        self.conn_vars['overall'].set(data.get('overall', '—'))
        self.conn_vars['time'].set(data.get('time', '—'))
        self.conn_vars['proxy'].set(data.get('proxy', '—'))
        summary = data.get('summary_line', '')
        # 汇总行以 ✅ / ❌ 开头 —— 用首字符选颜色，规则与 core 层文案约定一致
        if summary.startswith('✅'):
            color = CONN_OK_COLOR
        elif summary.startswith('❌'):
            color = CONN_BAD_COLOR
        else:
            color = CONN_IDLE_COLOR
        self.conn_summary_label.config(fg=color)
        self.conn_summary_var.set(summary or '尚未测试')

    # ---------- 日志预览区 ----------

    def refresh_log(self):
        """把 app.log 最近 200 行载入只读日志预览框（分级着色 + 滚动到底部）。

        入参：无。返回值：无。
        增量优化：先用 (大小, mtime) 判断日志有没有变化，没变直接返回，
        避免每次 op_done 都全量读文件并逐行打 tag（托盘常驻时很频繁）。
        """
        path = logger_module.LOG_PATH
        signature = None
        if path:
            try:
                stat = os.stat(path)
                signature = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                signature = None
        if path and signature is not None and signature == self._log_signature:
            return  # 日志没变化，跳过重绘
        self._log_signature = signature
        try:
            self.log_text.config(state='normal')
            self.log_text.delete('1.0', 'end')
            try:
                if not path:
                    # 日志尚未初始化（窗口早于 setup_logging 的极端情况）：
                    # 这不是错误，给友好占位，不要显示"读取失败"吓用户
                    raise FileNotFoundError('log not initialized')
                # 调用时读 LOG_PATH（setup_logging 之后才有值，不能在 import 时绑定）
                with open(path, 'r', encoding='utf-8', errors='replace') as file:
                    lines = file.readlines()[-LOG_PREVIEW_LINES:]
            except FileNotFoundError:
                lines = ['（日志文件尚未生成）\n']
            except Exception as err:
                lines = [f'（读取日志失败：{err}）\n']
            self.log_text.insert('1.0', ''.join(lines))
            self._apply_log_tags()
            self.log_text.see('end')  # 自动滚动到最底部（最新日志）
        except Exception:
            logger.exception('刷新日志预览失败')
        finally:
            self.log_text.config(state='disabled')

    def _apply_log_tags(self):
        """按行内日志级别给日志着色：ERROR 红 / WARNING 橙 / INFO 深灰 / 其他默认。

        入参：无。返回值：无。
        """
        try:
            line_count = int(self.log_text.index('end-1c').split('.')[0])
            for line_no in range(1, line_count + 1):
                line = self.log_text.get(f'{line_no}.0', f'{line_no}.end')
                if '[ERROR]' in line:
                    tag = 'error'
                elif '[WARNING]' in line:
                    tag = 'warning'
                elif '[INFO]' in line:
                    tag = 'info'
                else:
                    tag = 'other'
                self.log_text.tag_add(tag, f'{line_no}.0', f'{line_no}.end')
        except Exception:
            logger.exception('日志分级着色失败')

    def _open_log_file(self):
        """「打开日志文件」按钮：直接在主线程调用（os.startfile 很快，无需后台线程）。

        入参：无。返回值：无。
        """
        ok, message = service.open_log_file()
        if not ok:
            state.set_message(message)

    # ---------- 工具窗口 ----------

    def open_download_dialog(self):
        """「下载加速链接生成」：打开非模态小工具窗口（单例）。

        入参：无。返回值：无。

        已开着就只提升到最前（不重复开多个）；窗口被用户关掉后允许重开。
        与 FormDialog 的区别：非模态（不 grab_set），打开后主窗口仍可操作。
        """
        dialog = getattr(self, '_download_dialog', None)
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            return
        self._download_dialog = DownloadDialog(self.root)

    # ---------- 按钮忙状态与窗口开关 ----------

    def on_button(self, action, button):
        """操作按钮回调：交给统一调度器在后台线程执行。

        入参：action —— 业务函数（core.service 层函数；绝不能是会再次调用
              run_action 的窗口方法 —— run_action 不可重入，双重包装会因
              op_lock 抢不到而把操作吞掉）；
              button —— 触发的按钮（忙状态显示用）。
        返回值：无。
        """
        actions.run_action(action, button)

    def set_busy(self, busy, button=None):
        """操作忙状态：禁用 / 恢复全部操作按钮，被点击的按钮显示"处理中…"。

        入参：busy —— True 进入忙状态 / False 恢复；
              button —— 触发本次操作的按钮（None 表示托盘侧发起，仅禁用）。
        返回值：无。
        （被 actions.init 注入给调度器，操作期间由调度器在主线程调用。）
        """
        for btn, text in self._op_buttons:
            if busy:
                btn.state(['disabled'])
                if button is not None and btn is button:
                    btn.configure(text=self._busy_text)
            else:
                btn.state(['!disabled'])
                btn.configure(text=text)
        # 只有操作进行中才允许点「取消」
        self.cancel_btn.state(['!disabled'] if busy else ['disabled'])

    def hide(self):
        """窗口关闭按钮：隐藏到托盘（进程不退出，托盘「退出」才真正结束）。

        入参：无。返回值：无。
        """
        try:
            self._capture_geometry()
            self.root.withdraw()
            logger.info('主窗口已最小化到托盘')
            state.set_message('已最小化到托盘，双击托盘图标可重新打开')
        except Exception:
            logger.exception('隐藏主窗口失败')

    def show(self):
        """显示并激活主窗口（托盘菜单 / 双击托盘图标触发），并刷新界面数据。

        入参：无。返回值：无。
        （经 actions.post_ui 调度，保证在主线程执行。）
        """
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_status()
        self.refresh_accel_count()
        self.refresh_tree()
        self.refresh_log()
        self.refresh_progress(state.progress)

    def on_configure(self, event):
        """窗口尺寸位置变化时记录几何信息（供退出时保存）。

        入参：event —— tkinter 事件。返回值：无。
        """
        if event.widget is self.root:
            self._capture_geometry()

    def _capture_geometry(self):
        """把当前窗口尺寸位置写入全局状态（纯数据，不触碰控件，线程安全）。

        入参：无。返回值：无。
        （退出时 main.py 读取 state.window_geometry 经 merge_config_values 落盘。）
        """
        try:
            state.window_geometry = (self.root.winfo_width(), self.root.winfo_height(),
                                     self.root.winfo_x(), self.root.winfo_y())
            state.ui_scale = self._scale
        except Exception:
            logger.exception('记录窗口位置失败')
