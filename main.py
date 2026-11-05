# -*- coding: utf-8 -*-
"""程序入口：装配各层组件，并管理启动与退出的全流程。

main.py 的定位（为什么它不写任何"业务"）：
  各层已各司其职——utils 提供基础设施，core 实现业务，ui 实现界面；
  main.py 只做三件事：
    1. 进程环境准备：日志 -> UAC 提权 -> 单实例互斥量（顺序不能乱）；
    2. 装配：读一次配置注入 service，创建主窗口与托盘，把"托盘气泡"
       注入 core（依赖倒置的供给端），把窗口引用注入 ui.actions；
    3. 生命周期：启动定时刷新线程 -> 跑 tkinter 主循环 -> 退出收尾
       （停线程 -> 启用中则还原 hosts -> 保存窗口位置 -> 停托盘 -> 释放互斥量）。

启动顺序为什么是这样（每一步都有依赖关系）：
  日志最先      —— 之后任何一步失败都有迹可循；
  提权其次      —— 改系统 hosts 必须管理员，不满足就别往下走；
  单实例再次    —— 防止提权重启的旧实例与新实例同时运行；
  配置在窗口前  —— 窗口尺寸位置、定时刷新间隔都来自它；
  窗口在托盘前  —— 托盘菜单要引用窗口实例（显示主窗口）；
  主循环最后    —— 主线程从此阻塞，直到托盘「退出」触发 root.quit。
"""

import ctypes
import logging
import sys
import threading
import tkinter as tk

from core import config_manager
from core import hosts_manager
from core import service
from core.app_state import op_lock, state
from ui import actions
from ui import main_window, tray_icon
from utils import admin, app_icon
from utils.logger import fatal_popup, setup_logging

logger = logging.getLogger(__name__)

APP_TITLE = 'GitHub Hosts 加速'

# 定时自动刷新线程的退出信号（handle_exit 置位 -> 线程立刻被唤醒结束）
stop_event = threading.Event()


def ensure_admin():
    """管理员权限检查：非管理员时通过 UAC 重启自身拿权限。

    入参：无。返回值：无（拿不到权限直接 sys.exit）。

    --elevated 防循环：提权重启后仍非管理员（如组策略禁止）时，
    看到标记就报错退出，绝不再次弹 UAC。
    """
    if admin.is_admin():
        return
    if admin.ELEVATED_FLAG in sys.argv[1:]:
        logging.error('已提权重启但仍未获得管理员权限，程序退出')
        fatal_popup('未能获得管理员权限，无法修改系统 hosts。')
        sys.exit(1)
    if admin.relaunch_as_admin():
        sys.exit(0)  # 管理员实例已拉起，本进程退出
    logging.error('权限请求失败，程序退出')
    fatal_popup('管理员权限请求被取消，程序无法运行。')
    sys.exit(1)


def ensure_single_instance():
    """单实例检查：全系统只允许运行一个本程序实例。

    入参：无。返回值：无（已有实例时 sys.exit）。
    （互斥量创建失败返回 0 —— 降级为不检查，不影响正常使用。）
    """
    if admin.acquire_single_instance() is None:
        logging.error('检测到程序已在运行，本实例退出')
        # 0x40 = MB_ICONINFORMATION（信息图标；与 fatal_popup 的错误图标区分）
        ctypes.windll.user32.MessageBoxW(
            0, f'{APP_TITLE} 已在运行，请勿重复启动。', APP_TITLE, 0x40)
        sys.exit(0)


def save_window_config():
    """把主窗口位置尺寸合并写回 config.json（保留其他配置项，退出时调用）。

    入参：无。返回值：无；失败只记日志（退出流程不因存配置中断）。
    """
    try:
        geo = state.window_geometry
        if not geo:
            return  # 窗口未捕获过几何信息（如从未显示）则跳过
        keys = ('window_width', 'window_height', 'window_x', 'window_y')
        values = dict(zip(keys, geo))
        if config_manager.merge_config_values(values):
            logger.info('已保存主窗口位置尺寸：%s', values)
    except Exception:
        logger.exception('保存窗口配置失败')


def handle_exit(root, tray):
    """退出流程（托盘「退出」触发；在托盘线程执行，注意不要碰 tkinter 控件）。

    入参：root —— tkinter 根窗口（结束主循环用）；
          tray —— TrayIcon 实例（收尾时停止）。
    返回值：无。

    收尾顺序有讲究：
      1. 先抢 op_lock —— 有操作正在跑时拒绝退出（此时写 hosts 有竞态风险）；
      2. 停定时线程（stop_event 立刻唤醒等待中的线程）；
      3. 启用中则还原 hosts —— 绝不带着加速状态退出（失败弹窗警告，不能静默）；
      4. 保存窗口位置 -> 停托盘 -> 经 post_ui 在主线程结束主循环。
    """
    if not op_lock.acquire(blocking=False):
        service.notify_user('正在执行其他操作，请稍候后再退出', popup=False)
        return
    try:
        logger.info('收到退出请求')
        stop_event.set()  # 唤醒并结束定时刷新线程
        if state.enabled:
            ok, message = hosts_manager.restore_from_backup()
            if ok:
                # 同步内存状态（发布事件，让仍存活的订阅者一起回到一致）
                state.set_enabled(False)
                logger.info('退出前已自动停用加速，hosts 已还原')
            else:
                logger.error('退出前还原 hosts 失败：%s', message)
                # 还原失败必须让用户看到：hosts 还处于加速状态，不能无声退出
                ctypes.windll.user32.MessageBoxW(
                    0,
                    f'退出前还原 hosts 失败：\n{message}\n\n'
                    f'请手动检查 {hosts_manager.HOSTS_PATH}\n'
                    f'（备份位于 {hosts_manager.BACKUP_PATH}）',
                    APP_TITLE, 0x10)
        save_window_config()   # 保存主窗口位置与尺寸
        tray.stop()
        # 结束 tkinter 主循环必须回主线程（root.quit 不是线程安全的）
        actions.post_ui(root.quit)
    finally:
        op_lock.release()


def main():
    """程序入口：日志 -> 提权 -> 单实例 -> 配置 -> 状态检测 -> 窗口 + 托盘 -> 主循环。

    入参：无。返回值：无（主循环结束后正常返回）。
    """
    if sys.platform != 'win32':
        sys.stderr.write('本工具仅支持 Windows 平台。\n')
        sys.exit(1)

    setup_logging()  # 日志最先初始化：之后任何步骤失败都有迹可循

    # --- 权限与单实例（不满足条件直接退出，见函数内注释） ---
    ensure_admin()
    logger.info('===== 程序启动（管理员权限）=====')
    ensure_single_instance()

    # --- 配置：只读一次，注入 service（各模块不重复 IO） ---
    config = config_manager.load_config()
    service.init(config)

    # --- 启动状态检测：以 hosts 磁盘内容为准（程序可能上次没正常退出）---
    # 此时还没有任何订阅者，直接赋值即可；窗口 / 托盘构造时会读它作为初始状态
    state.enabled = service.detect_enabled_on_startup()
    service.reload_candidates_state()  # 候选列表装载到状态（窗口表格的数据源）

    # --- 应用图标：任务栏分组 ID + 生成 icon.png / icon.ico（缺失时） ---
    app_icon.set_app_user_model_id()
    app_icon.ensure_icon_files()

    # --- 主窗口（主线程创建，启动即显示）+ 调度器注入 ---
    root = tk.Tk()
    app = main_window.AppWindow(root, APP_TITLE, config)
    actions.init(root, app.set_busy)  # 打破 main_window <-> actions 循环导入

    # --- 托盘：注入"显示窗口 / 退出"两个进程级动作 ---
    # core 的通知出口（service.notify_user）接到托盘气泡 —— 依赖倒置的供给端
    tray = tray_icon.TrayIcon(
        APP_TITLE,
        on_show_window=lambda: actions.post_ui(app.show),
        on_exit=lambda t: handle_exit(root, t))
    service.set_notifier(tray.notify)
    tray.start()

    # --- 定时自动刷新守护线程（循环体在 core.service） ---
    threading.Thread(target=service.refresh_scheduler_loop,
                     args=(stop_event,), name='auto-refresh',
                     daemon=True).start()

    # --- tkinter 主循环（主线程阻塞于此） ---
    try:
        root.mainloop()
    finally:
        logger.info('===== 程序退出 =====')
        tray.stop()            # 与 handle_exit 中的调用幂等（异常已捕获）
        admin.release_mutex()  # 释放单实例互斥量（进程结束内核也会自动释放）


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # 兜底：任何未处理异常都记录 + 弹窗，避免"双击后一闪而过"
        import traceback
        error_text = traceback.format_exc()
        try:
            logging.error('程序发生未处理异常并退出：%s', error_text)
        except Exception:
            pass
        fatal_popup(f'程序发生未处理异常已退出，详见 app.log。\n\n{error_text[-1200:]}')
        sys.exit(1)
