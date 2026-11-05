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

import logging
import socket
import time

import tkinter as tk
from tkinter import ttk, messagebox

from PIL import ImageTk

from core import service
from core.app_state import state
from core.hosts_manager import count_accel_entries, looks_like_domain, looks_like_ipv4
from ui import actions
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
        root.title(title)
        root.minsize(640, 480)
        self._op_buttons = []       # [(按钮, 原始文字)]：忙状态时统一禁用
        self._busy_text = '处理中…'
        self._row_map = {}          # {Treeview行ID: (IP, 域名)}，供选中项解析
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

    # ---------- 布局 ----------

    def _apply_geometry(self):
        """按 config.json 恢复窗口尺寸位置（无配置则 720x560 居中）。

        入参：无。返回值：无。
        """
        width = self._config.get('window_width') or DEFAULT_WINDOW_WIDTH
        height = self._config.get('window_height') or DEFAULT_WINDOW_HEIGHT
        self.root.geometry(f'{width}x{height}')
        x, y = self._config.get('window_x'), self._config.get('window_y')
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        if x is None or y is None:  # 无记录则居中偏上显示
            x, y = max(0, (screen_w - width) // 2), max(0, (screen_h - height) // 3)
        # 钳制到屏幕内：避免拔掉副显示器后窗口"藏"在不可达区域找不回来
        x = max(0, min(x, screen_w - 200))
        y = max(0, min(y, screen_h - 100))
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
                                     font=('Microsoft YaHei UI', 10, 'bold'))
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

        # C / D / E：垂直可拖拽分割的三个区域
        paned = ttk.Panedwindow(self.root, orient='vertical')
        paned.pack(fill='both', expand=True, padx=10, pady=(0, 10))

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
        self.tree.column('ip', width=150, anchor='w')
        self.tree.column('domain', width=240, anchor='w')
        self.tree.column('latency', width=120, anchor='center')
        self.tree.column('status', width=80, anchor='center')
        vsb = ttk.Scrollbar(tree_box, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        vsb.pack(side='left', fill='y')
        self.tree.bind('<Double-1>', self.on_double_edit)
        self.tree.bind('<Button-3>', self.on_right_click)  # 右键菜单

        # D. 连通性测试结果区（紧凑布局 + 顶部大字状态汇总）
        conn_frame = ttk.Labelframe(paned, text='连通性测试结果', padding=(8, 2))
        paned.add(conn_frame, weight=1)
        self.conn_summary_var = tk.StringVar(value='尚未测试')
        self.conn_summary_label = tk.Label(
            conn_frame, textvariable=self.conn_summary_var, anchor='w',
            font=('Microsoft YaHei UI', 12, 'bold'), fg=CONN_IDLE_COLOR)
        self.conn_summary_label.pack(fill='x', pady=(2, 0))
        self.conn_vars = {key: tk.StringVar(value='—')
                          for key in ('github', 'raw', 'overall', 'time')}
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

        # E. 日志预览区（浅灰底 + 等宽字体 + 分级着色）
        log_frame = ttk.Labelframe(paned, text='日志预览（最近 200 行）', padding=4)
        paned.add(log_frame, weight=2)
        log_top = ttk.Frame(log_frame)
        log_top.pack(fill='x')
        ttk.Button(log_top, text='刷新日志', command=self.refresh_log).pack(side='right', padx=2)
        ttk.Button(log_top, text='打开日志文件',
                   command=self._open_log_file).pack(side='right', padx=2)
        self.log_text = tk.Text(log_frame, height=8, wrap='none', state='disabled',
                                bg=LOG_BG_COLOR, fg='#24292e', relief='flat',
                                font=('Consolas', 10))
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
        """AppState 订阅回调：可能在后台线程触发，调度回 tkinter 主线程处理。

        入参：event —— 事件名；payload —— 事件数据。
        返回值：无。
        """
        actions.post_ui(lambda: self._handle_event(event, payload))

    def _handle_event(self, event, payload=None):
        """主线程内处理状态事件；任何异常只记日志，不影响事件发布方。

        入参：event —— 事件名；payload —— 事件数据。返回值：无。
        """
        try:
            if event == 'enabled':
                self.refresh_status()
                self.refresh_accel_count()
            elif event == 'accel_changed':
                self.refresh_accel_count()
            elif event == 'candidates_changed':
                self.refresh_tree()
            elif event == 'connectivity':
                self.refresh_connectivity_view()
                self.refresh_log()
            elif event == 'op_done':
                # 任一操作结束（含托盘侧发起的）：做一次全量刷新，
                # 保证徽标 / 生效条目 / 表格 / 连通性 / 日志 / 信息行与实际状态一致。
                # 这是修复"托盘改了状态、窗口没跟上"这类不同步 bug 的兜底机制。
                self.refresh_status()
                self.refresh_accel_count()
                self.refresh_tree()
                self.refresh_connectivity_view()
                self.refresh_log()
                self.update_info_line()
            elif event == 'message':
                self.update_info_line()
        except Exception:
            logger.exception('处理状态事件失败：%s', event)

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
        if state.last_message:
            text += f'    [{state.last_message}]'
        self.info_var.set(text)

    # ---------- IP 列表区 ----------

    def refresh_tree(self):
        """按全局候选状态重建表格并更新"共 N 条候选，M 条可用"。

        入参：无。返回值：无。
        """
        self.tree.delete(*self.tree.get_children())
        self._row_map = {}
        available = 0
        for ip, domain in state.candidates:
            if ip in state.ip_latency:
                ms, err = state.ip_latency[ip]
                if ms is not None:
                    latency, status = str(ms), '可用'
                    available += 1
                else:
                    latency = '—'
                    status = '超时' if 'timed out' in str(err).lower() else '失败'
            else:
                latency, status = '—', '未测'
            item_id = self.tree.insert('', 'end', values=(ip, domain, latency, status))
            self._row_map[item_id] = (ip, domain)
        self.count_var.set(f'共 {len(state.candidates)} 条候选，{available} 条可用')
        if self._sort_column:
            self._apply_sort()  # 数据刷新后保持当前排序

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
            try:
                parts = tuple(int(part) for part in text.split('.'))
            except ValueError:
                parts = (0, 0, 0, 0)
            # 降序时取负数仍按"升序"排，等效于数值降序
            return tuple(-part for part in parts) if self._sort_reverse else parts
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
        return [self._row_map[item_id] for item_id in self.tree.selection()]

    def on_right_click(self, event):
        """表格右键菜单：仅测速此项 / 复制 IP / 删除此项。

        入参：event —— tkinter 事件（含坐标）。返回值：无。
        """
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return  # 空白处右键不弹菜单
        if item_id not in self.tree.selection():
            self.tree.selection_set(item_id)  # 右键未选中行时先选中它
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label='仅测速此项', command=self.ctx_test_one)
        menu.add_command(label='复制 IP', command=self.ctx_copy_ip)
        menu.add_separator()
        menu.add_command(label='删除此项', command=self.ctx_delete_one)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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
            if not looks_like_ipv4(ip):
                messagebox.showwarning('手动添加', 'IP 格式不正确，请输入合法 IPv4 地址。',
                                       parent=dialog)
                return
            if not looks_like_domain(domain):
                messagebox.showwarning('手动添加', '域名格式不正确。',
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
            if not looks_like_ipv4(new_ip):
                messagebox.showwarning('编辑 IP', 'IP 格式不正确，请输入合法 IPv4 地址。',
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
        """
        try:
            self.log_text.config(state='normal')
            self.log_text.delete('1.0', 'end')
            try:
                # 调用时读 LOG_PATH（setup_logging 之后才有值，不能在 import 时绑定）
                with open(logger_module.LOG_PATH, 'r', encoding='utf-8',
                          errors='replace') as file:
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
        except Exception:
            logger.exception('记录窗口位置失败')
