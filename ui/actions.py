# -*- coding: utf-8 -*-
"""统一操作调度器：托盘菜单与主窗口按钮共用的"后台线程 + 忙状态"机制。

为什么需要一个调度器？
  启用 / 停用 / 测速 / 联网更新都是耗时操作，直接在按钮回调里跑会卡死
  界面线程；托盘和窗口两侧如果各写一套线程逻辑，忙状态（"处理中…"）、
  操作互斥、完成刷新就会各漏各的。
  现在两侧都只做一件事：actions.run_action(业务函数)。

它保证四件事：
  1. 互斥：同一时刻只允许一个操作在跑（core.app_state.op_lock）；
  2. 后台执行：业务函数在 daemon 线程中运行，托盘 / 界面都不卡；
  3. 忙状态：执行期间禁用所有操作按钮，被点的按钮显示"处理中…"；
  4. 完成广播：结束后发布 op_done 事件，主窗口做一次全量刷新
     （这是修复"状态不同步"bug 的关键一环）。

为什么用注入（init）而不是直接 import 主窗口？
  main_window 要 import 本模块拿 run_action，本模块又要调窗口的
  set_busy —— 互相 import 会形成循环导入。注入打破循环：
  main.py 创建窗口后把引用接进来。
"""

import logging
import threading

from tkinter import messagebox

from core import service
from core.app_state import op_cancel, op_lock, state
from utils import admin

logger = logging.getLogger(__name__)

# 允许通过「重启为管理员并自动继续」执行的动作白名单（与 main.PENDING_ACTIONS 对应）。
# 只允许这些内部动作名，避免命令行入口被滥用。
_ALLOWED_PENDING_ACTIONS = frozenset({'enable', 'disable', 'force_restore'})

# tkinter 根窗口引用（main.py 注入；None 表示 UI 未就绪）
_ui_root = None
# 忙状态回调 fn(busy: bool, button)（main.py 注入主窗口的 set_busy）
_busy_hook = None


def init(root, busy_hook):
    """注入 UI 引用（main.py 在创建主窗口后调用一次）。

    入参：root —— tkinter 根窗口（root.after 调度用）；
          busy_hook —— fn(busy, button)，切换按钮忙状态。
    返回值：无。
    """
    global _ui_root, _busy_hook
    _ui_root = root
    _busy_hook = busy_hook


def post_ui(callback):
    """把回调调度回 tkinter 主线程执行（跨线程更新 UI 的唯一通道）。

    入参：callback —— 无参函数，将在主线程执行。
    返回值：无。

    为什么必须经过 root.after？
      tkinter 不是线程安全的：子线程直接调用控件方法可能崩溃或竞态。
      root.after(0, cb) 把任务投递到主线程的事件队列，由主线程执行 ——
      "数据在子线程算，画界面回主线程画"。
    窗口未就绪 / 已销毁时只记日志忽略（如托盘早于窗口的动作）。
    """
    if _ui_root is None:
        logger.debug('UI 未就绪，忽略界面更新调度')
        return
    try:
        _ui_root.after(0, callback)
    except Exception:
        logger.exception('调度 UI 更新失败')


def run_action(action, button=None):
    """统一的操作调度入口（托盘菜单与主窗口按钮共用）。

    入参：action —— 无参函数，在后台线程执行（通常是 service 里的函数或
           闭包，如 lambda: service.add_candidate(ip, domain)）；
          button —— 触发本次操作的 ttk.Button（执行期间显示"处理中…"），
           托盘调用传 None。
    返回值：无（立即返回；结果经状态事件回界面）。
    """
    # 0) 按需提权：会修改系统 hosts 的动作在受限模式下先请求管理员权限，
    #    绝不等到写文件时报 PermissionError。弹窗与重启必须在主线程做。
    if _requires_admin(action) and not admin.is_admin():
        post_ui(lambda: _request_elevation(action))
        return

    # 1) 操作互斥：抢不到锁说明有操作在跑，直接提示并放弃
    if not op_lock.acquire(blocking=False):
        service.notify_user('正在执行其他操作，请稍候…', popup=False)
        state.set_message('正在执行其他操作，请稍候…')
        return
    op_cancel.clear()  # 新操作开始：清掉上一次「取消」留下的信号
    state.set_message('')  # 清空上一条提示信息
    post_ui(lambda: _set_busy(True, button))

    def worker():
        try:
            action()
        except Exception:
            # 业务函数的异常在这里统一兜底：记日志 + 通知，绝不让线程静默死亡
            logger.exception('操作执行出错')
            service.notify_user('操作出错，详见 app.log')
            state.set_message('操作出错，详见 app.log')
        finally:
            op_lock.release()
            post_ui(lambda: _set_busy(False, None))
            state.set_progress('')  # 隐藏进度条（任何结束路径都要收尾）
            # 广播"操作完成"：主窗口收到后全量刷新（徽标/条目数/表格/日志/信息行），
            # 无论操作从托盘还是窗口发起，两侧都能回到一致状态
            state.publish('op_done')

    threading.Thread(target=worker, daemon=True).start()


def _requires_admin(action):
    """判断某个业务函数是否需要管理员权限。

    入参：action —— 无参业务函数。返回值：bool。
    （service.enable / disable / force_restore 在模块末尾带了 requires_admin 标记。）
    """
    return bool(getattr(action, 'requires_admin', False))


def _request_elevation(action):
    """询问用户并以管理员身份重启（只在主线程调用）。

    提权重启会带上 --do <动作名>，新实例启动后自动继续执行该操作，
    用户不需要重新点一次。当前实例随即退出（避免两个窗口并存太久）。
    """
    name = getattr(action, '__name__', '')
    if name not in _ALLOWED_PENDING_ACTIONS:
        logger.warning('动作 %s 需要管理员权限，但不在可自动继续的白名单内', name)
        state.set_message('该操作需要管理员权限，请以管理员身份运行本程序')
        return
    logger.info('操作 %s 需要管理员权限，询问用户是否提权重启', name)
    if not messagebox.askyesno(
            '需要管理员权限',
            '修改系统 hosts 需要管理员权限。\n\n'
            '是否以管理员身份重新启动？\n'
            '（当前窗口会关闭，重启后自动继续刚才的操作）',
            parent=_ui_root):
        state.set_message('已取消：该操作需要管理员权限')
        return
    state.set_message('正在请求管理员权限…')
    if admin.relaunch_as_admin(pending_action=name):
        logger.info('已请求提权重启（动作：%s），当前实例退出', name)
        if _ui_root is not None:
            _ui_root.after(200, _ui_root.quit)
    else:
        logger.warning('获取管理员权限失败（动作：%s）', name)
        state.set_message('获取管理员权限失败，操作未执行')


def request_cancel():
    """请求取消当前后台操作（由主窗口「取消」按钮在主线程调用）。

    入参：无。返回值：无。
    只是置位全局取消信号；真正的停止点由 core.service 在各阶段边界检查，
    所以取消不是"立刻杀线程"，而是"让它尽快收尾"（正在进行的网络请求
    最多再等一次超时，通常 1~3 秒）。
    """
    op_cancel.set()
    state.set_message('正在取消当前操作…')
    logger.info('用户请求取消当前操作')


def _set_busy(busy, button):
    """内部工具：安全调用注入的忙状态回调（回调异常不影响调度流程）。"""
    if _busy_hook is None:
        return
    try:
        _busy_hook(busy, button)
    except Exception:
        logger.exception('切换忙状态失败')
