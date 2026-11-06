# -*- coding: utf-8 -*-
"""系统托盘图标：pystray 常驻图标 + 右键菜单 + 气泡通知。

pystray 的线程模型（理解它，才知道这个类为什么这样设计）：
  - Icon.run_detached() 在**独立的托盘线程**里跑自己的消息循环，
    与 tkinter 主线程完全并行、互不干扰；
  - 菜单点击回调由托盘线程调用 —— 回调里绝不能碰 tkinter 控件；
    所以所有业务操作都经 ui.actions.run_action 丢后台线程，结果
    通过 state 事件经 root.after 回主线程，托盘本身只负责"发起点"；
  - 托盘图标的更新（Icon.icon 属性）内部由 pystray 自己做线程封送，
    任意线程赋值都安全 —— 所以"启用状态变化就换图标"直接在订阅回调里做。

与 core 的关系（依赖倒置的另一端）：
  core/service 通过 set_notifier() 注入的"通知函数"，就是本类的 notify。
  托盘认识 service 和 state，service 不认识托盘 —— 依赖方向单向。
"""

import logging

import pystray

from core import service
from core.app_state import state
from ui import actions
from utils import app_icon
from utils.logger import fatal_popup

logger = logging.getLogger(__name__)


class TrayIcon:
    """系统托盘图标：菜单发起业务操作、订阅状态换图标、对外提供气泡通知。

    依赖注入（为什么不用全局变量）：
      main.py 创建主窗口后构造本类，把"显示窗口"和"退出程序"两个动作
      注入进来 —— 这两个动作分别涉及 tkinter 主窗口和进程级收尾
      （停线程、存配置、还原 hosts），属于 main.py 的职责；
      托盘只管"什么时候触发它们"。
    """

    def __init__(self, title, on_show_window, on_exit):
        """构造托盘图标（尚未显示，调用 start() 后才出现在托盘区）。

        入参：title —— 托盘提示文字（与应用标题一致）；
              on_show_window —— fn()，显示主窗口（实现方自行保证线程安全）；
              on_exit —— fn(tray_icon)，退出程序，收尾后须调用 tray_icon.stop()。
        返回值：无。
        """
        self._title = title
        self._on_show_window = on_show_window
        self._on_exit = on_exit
        # 真正的 pystray 图标对象：初始图标按当前启用状态选色（绿/灰）
        self._icon = pystray.Icon(
            title, app_icon.make_app_icon('on' if state.enabled else 'off'),
            title, self._build_menu())
        # 订阅启用状态变化：换托盘图标（订阅永不退订 —— 生命周期同进程）
        state.subscribe(self._on_state_event)

    # ------------------------------------------------------------------
    # 菜单构建
    # ------------------------------------------------------------------

    def _build_menu(self):
        """构建托盘右键菜单（分隔线分组：启停操作 / 维护操作 / 窗口与退出）。

        入参：无。
        返回值：pystray.Menu。
        """
        return pystray.Menu(
            # 启停操作
            pystray.MenuItem('启用加速', self._guarded(service.enable)),
            pystray.MenuItem('停用加速', self._guarded(service.disable)),
            pystray.MenuItem('强制还原原始 hosts', self._guarded(service.force_restore)),
            pystray.Menu.SEPARATOR,
            # 维护与测试操作
            pystray.MenuItem('更新 IP 列表', self._guarded(service.update_ip_list_online)),
            pystray.MenuItem('测试 GitHub 连通性', self._guarded(service.test_connectivity)),
            pystray.Menu.SEPARATOR,
            # 窗口与退出（default=True：左键 / 双击托盘图标即触发此项）
            pystray.MenuItem('打开主窗口', self._on_menu_show_window, default=True),
            pystray.MenuItem('打开日志', self._guarded(service.open_log_file)),
            pystray.MenuItem('退出', self._on_menu_exit),
        )

    def _guarded(self, action):
        """把业务函数包装成 pystray 菜单回调（经统一调度器执行）。

        入参：action —— core.service 中的业务函数。
        返回值：fn(icon, menu_item) —— pystray 要求的回调签名。

        为什么叫 guarded？与旧版单文件同名概念：托盘回调在托盘线程执行，
        必须走 run_action（互斥锁 + 后台线程 + 忙状态 + op_done 广播），
        而不是直接执行业务函数。
        """
        def handler(icon, menu_item):
            actions.run_action(action)
        return handler

    def _on_menu_show_window(self, icon, menu_item):
        """「打开主窗口」回调（含双击托盘图标）：调用注入的显示窗口动作。"""
        self._on_show_window()

    def _on_menu_exit(self, icon, menu_item):
        """「退出」回调：调用注入的退出动作（收尾后由它回调 stop()）。"""
        self._on_exit(self)

    # ------------------------------------------------------------------
    # 通知 / 状态订阅
    # ------------------------------------------------------------------

    def notify(self, message, popup=True):
        """托盘气泡通知；托盘不可用时降级为系统弹窗，保证用户能看到结果。

        入参：message —— 气泡文字；
              popup —— 降级时是否允许弹系统窗（后台静默操作传 False）。
        返回值：无。

        为什么有降级？service 在托盘启动失败 / 尚未就绪时仍会发通知
        （如启用加速的完成提示），此时消息不能凭空消失。
        """
        try:
            self._icon.notify(message, self._title)
            return
        except Exception:
            logger.exception('托盘气泡通知失败：%s', message)
        if popup:
            fatal_popup(message)

    def _on_state_event(self, event, payload=None):
        """AppState 订阅回调：启用状态变化时同步托盘图标（绿点=启用，灰点=未启用）。

        入参：event —— 事件名；payload —— 事件数据（enabled 事件为 bool）。
        返回值：无。
        （Icon.icon 属性赋值线程安全，见模块 docstring 的线程模型说明。）
        """
        if event == 'enabled':
            try:
                self._icon.icon = app_icon.make_app_icon('on' if payload else 'off')
            except Exception:
                logger.exception('更新托盘图标失败')

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self):
        """在独立线程启动托盘（不阻塞调用方，主线程继续跑 tkinter 主循环）。

        入参：无。返回值：无。
        启动失败只记日志不抛出 —— 托盘是"锦上添花"，主窗口功能不受影响。
        """
        try:
            self._icon.run_detached(self._on_ready)
        except Exception:
            logger.exception('托盘图标启动失败（主窗口仍可正常使用）')

    def _on_ready(self, icon):
        """托盘就绪回调（托盘线程内执行）：设置可见并记录日志。

        入参：icon —— pystray.Icon 对象。返回值：无。
        """
        try:
            icon.visible = True
            logger.info('托盘图标已就绪')
        except Exception:
            logger.exception('初始化托盘显示失败')

    def stop(self):
        """停止托盘图标（退出程序时由 main.py 的退出流程调用）。

        入参：无。返回值：无。
        """
        try:
            self._icon.stop()
        except Exception:
            logger.exception('停止托盘失败')
