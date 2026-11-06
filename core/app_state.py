# -*- coding: utf-8 -*-
"""全局状态中心：唯一的状态对象 + 发布/订阅事件机制 + 全局操作锁。

为什么需要它？（这是本项目状态同步架构的核心，也是第四版修 bug 的成果）
  托盘和主窗口展示的是同一份数据（是否启用、候选列表、测速结果……）。
  如果两侧各自维护一份，任何一侧操作后都要"记得"手动刷新另一侧，
  漏掉一处就是状态不同步 bug。
  现在的规则是：
    1. 数据只存一份（本模块的 state 对象）；
    2. 谁改了数据，谁就调用 publish() 广播对应事件；
    3. 托盘订阅者收到事件直接换图标；主窗口订阅者把刷新动作经
       root.after() 调度回 tkinter 主线程执行（线程安全，见下方线程模型）。
  两侧从此互不认识，只认识 state —— 低耦合。

事件类型一览（publish 的 event 参数）：
  enabled             加速启用状态变化，payload=bool
  candidates_changed  候选列表 / 测速结果变化（表格刷新信号）
  accel_changed       hosts 加速段内容变化（生效条目数刷新信号）
  connectivity        连通性测试完成，payload=详情 dict
  refresh_time        上次 / 下次刷新时间变化
  message             一条界面提示消息
  progress            操作进度（payload=dict 或 None），驱动主窗口进度条
  op_done             任意一个后台操作结束（主窗口做全量刷新的信号）

线程模型（重要）：
  - state 的简单字段赋值不加锁：CPython 的 GIL 保证单个赋值不会"撕裂"，
    且所有读侧的复杂操作（遍历候选列表重建表格等）都只在主线程做；
  - 但"同一时刻只跑一个业务操作"必须显式加锁，见下方 op_lock。
"""

import logging
import threading


class AppState:
    """全局共享状态对象（托盘与主窗口的唯一数据源）。

    使用规则：
      - 读：任何线程随时可读（简单字段）；
      - 写：必须走 set_xxx 系列方法 —— 它们会顺带广播事件，
        直接改属性会漏发事件、造成界面不同步；
      - 订阅：回调签名 fn(event, payload=None)；回调里绝不要直接操作
        tkinter 控件（发布方在后台线程），先经 root.after 调度回主线程。
    """

    def __init__(self):
        self.enabled = False            # 当前是否处于加速启用状态
        self.is_admin = False           # 当前进程是否管理员（受限模式下为 False）
        self.candidates = []            # [(IP, 域名)] 与 github_hosts.txt 同步
        self.ip_latency = {}            # {IP: (延迟ms 或 None, 失败原因)}
        self.last_refresh_time = None   # 上次在线刷新成功的时间戳
        self.next_refresh_time = None   # 下次定时刷新的时间戳
        self.last_connectivity = None   # 最近一次连通性测试结果（dict）
        self.last_message = ''          # 最近一条界面提示消息
        self.progress = None            # 进行中任务的进度 {text, done, total}；None=空闲
        self.window_geometry = None     # (宽, 高, x, y) 供退出时保存窗口位置
        self.ui_scale = 1.0             # 记录窗口几何时的 DPI 缩放系数（高 DPI 换算用）
        self._subscribers = []          # 事件订阅者列表：fn(event, payload)

    # ------------------------------------------------------------------
    # 订阅 / 发布
    # ------------------------------------------------------------------

    def subscribe(self, callback):
        """注册状态事件订阅者。

        入参：callback —— fn(event, payload=None)，异常会被 publish 捕获。
        返回值：无。
        """
        self._subscribers.append(callback)

    def publish(self, event, payload=None):
        """发布状态事件给所有订阅者。

        入参：event —— 事件名字符串（见模块 docstring 的事件表）；
              payload —— 附带数据，可为 None。
        返回值：无。

        为什么逐个 try-except？
          单个订阅者出错不应该拖垮发布方（往往是后台线程），
          也不应该影响其他订阅者收到事件。
        """
        for callback in list(self._subscribers):
            try:
                callback(event, payload)
            except Exception:
                logging.exception('状态事件处理异常：%s', event)

    # ------------------------------------------------------------------
    # 带广播的写入方法（改状态必须走这里，保证事件不漏发）
    # ------------------------------------------------------------------

    def set_enabled(self, enabled):
        """更新启用状态并广播 enabled 事件。

        入参：enabled —— bool。返回值：无。
        """
        self.enabled = enabled
        self.publish('enabled', enabled)

    def set_candidates(self, pairs):
        """更新候选列表并广播 candidates_changed 事件。

        入参：pairs —— [(IP, 域名), ...]。返回值：无。
        """
        self.candidates = list(pairs)
        self.publish('candidates_changed')

    def set_ip_result(self, ip, ms, err):
        """记录单个 IP 最近一次测速结果（只记录、不广播）。

        入参：ip —— IP 字符串；ms —— 延迟毫秒或 None；err —— 失败原因或 None。
        返回值：无。
        为什么不广播？批量测速几十个 IP，逐个广播会刷新表格几十次；
        由调用方在全部完成后统一 publish('candidates_changed')。
        """
        self.ip_latency[ip] = (ms, err)

    def set_message(self, message):
        """更新界面提示消息并广播 message 事件。

        入参：message —— 提示文字（空串表示清除）。返回值：无。
        """
        self.last_message = message
        self.publish('message')

    def set_progress(self, text='', done=0, total=0):
        """更新任务进度并广播 progress 事件。

        入参：text —— 阶段文字（空串表示任务结束、隐藏进度条）；
              done / total —— 已完成 / 总数；total<=0 表示"不确定进度"（无限滚动条）。
        返回值：无。
        为什么用事件而不是让 UI 直接读 service：ui 与 core 解耦，
        进度只是"状态"，谁产生谁广播，窗口在哪个线程刷新由订阅方负责。
        """
        if text:
            self.progress = {'text': text, 'done': int(done), 'total': int(total)}
        else:
            self.progress = None
        self.publish('progress', self.progress)


# 全局唯一实例：所有模块 import 这一个对象，不自己 new
state = AppState()

# 全局操作互斥锁：同一时刻只允许一个业务操作（启用/停用/测速/更新…）在跑。
# 为什么放在状态中心？托盘按钮、窗口按钮、定时刷新线程三个地方都要抢
# 同一把锁，而它们分属不同模块 —— 共享的协调原语就放在共享状态旁边。
op_lock = threading.Lock()

# 全局取消信号：用户点「取消」时由 ui 层置位，service 的各阶段（测速 / TLS 校验 /
# 写 hosts 前）检查它，把长任务尽快停下来。每次 run_action 开始时清空。
op_cancel = threading.Event()
