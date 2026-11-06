# -*- coding: utf-8 -*-
"""程序入口：装配各层组件，并管理启动与退出的全流程。

main.py 的定位（为什么它不写任何"业务"）：
  各层已各司其职——utils 提供基础设施，core 实现业务，ui 实现界面；
  main.py 只做三件事：
    1. 进程环境准备：日志 -> 权限探测 -> 单实例互斥量（顺序不能乱）；
    2. 装配：读一次配置注入 service，创建主窗口与托盘，把"托盘气泡"
       注入 core（依赖倒置的供给端），把窗口引用注入 ui.actions；
    3. 生命周期：启动定时刷新线程 ->（可选）执行提权重启携带的动作 ->
       跑 tkinter 主循环 -> 退出收尾
       （停线程 -> 启用中则还原 hosts -> 保存窗口位置 -> 停托盘 -> 释放互斥量）。

v4.3 起改为「按需提权」（P1-9）：
  启动不再强制要求管理员，普通用户也能打开界面、查看状态、管理候选列表、
  使用下载加速链接生成器；只有启用/停用/还原（要改系统 hosts）时才提示
  以管理员身份重启，并通过 --do <动作> 让新实例自动继续刚才的操作。
  提权后仍非管理员（组策略禁止等）时直接报错退出，绝不反复弹 UAC。

启动顺序为什么是这样：
  日志最先      —— 之后任何一步失败都有迹可循（日志已按数据目录解析）；
  权限其次      —— 决定本次以管理员还是受限模式运行；
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
from utils import admin, app_icon, dpi
from utils import proxy as proxy_detector
from utils.logger import fatal_popup, setup_logging
from utils.version import __version__

logger = logging.getLogger(__name__)

APP_TITLE = f'GitHub Hosts 加速 v{__version__}'

# 定时自动刷新线程的退出信号（handle_exit 置位 -> 线程立刻被唤醒结束）
stop_event = threading.Event()

# 提权重启时可以自动继续执行的动作（--do <name>）。只允许白名单内的动作，
# 避免命令行被构造成任意调用入口。
PENDING_ACTIONS = {
    'enable': service.enable,
    'disable': service.disable,
    'force_restore': service.force_restore,
}


def parse_pending_action(argv):
    """从命令行参数中解析 --do <动作>（提权重启时由旧实例传入）。

    入参：argv —— 命令行参数列表（不含程序名本身）。
    返回值：str 或 None —— 动作名（小写）；没有该参数时返回 None。
    同时兼容 "--do enable" 与 "--do=enable" 两种写法。
    """
    for index, arg in enumerate(argv):
        if arg == '--do' and index + 1 < len(argv):
            return argv[index + 1].strip().lower()
        if arg.startswith('--do='):
            return arg.split('=', 1)[1].strip().lower()
    return None


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


def install_exception_hooks(root):
    """安装全局异常兜底：GUI 回调与后台线程的未处理异常统一写进日志。

    入参：root —— tkinter 根窗口。返回值：无。

    为什么需要？打包成 -w（无控制台）后，tkinter 回调里的异常只会写到
    不存在的 stderr，界面表现就是"点了没反应"，日志里也没有任何记录。
    """
    def _tk_callback_error(exc, value, tb):
        logging.error('Tk 回调未处理异常：%s', value, exc_info=(exc, value, tb))

    root.report_callback_exception = _tk_callback_error

    def _thread_error(args):
        logging.error('后台线程未处理异常：%s', getattr(args, 'exc_value', ''),
                      exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = _thread_error


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
        values['ui_scale'] = state.ui_scale
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
      3. 启用中则还原 hosts —— 受限模式（非管理员）下无法还原，必须明确告警；
      4. 保存窗口位置 -> 停托盘 -> 经 post_ui 在主线程结束主循环。
    """
    if not op_lock.acquire(blocking=False):
        service.notify_user('正在执行其他操作，请稍候后再退出', popup=False)
        return
    try:
        logger.info('收到退出请求')
        stop_event.set()  # 唤醒并结束定时刷新线程
        if state.enabled:
            if not state.is_admin:
                logger.warning('受限模式无法还原 hosts（需要管理员权限）')
                ctypes.windll.user32.MessageBoxW(
                    0,
                    '当前以受限模式运行（没有管理员权限），无法自动还原 hosts。\n'
                    '加速仍处于启用状态；请以管理员身份重新运行后再退出，或手动还原。',
                    APP_TITLE, 0x30)
            else:
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


def _schedule_pending_action(root, name):
    """提权重启后，在主循环启动后自动执行旧实例托付的动作。

    入参：root —— tkinter 根窗口；name —— 动作名（已由命令行解析）。
    返回值：无。
    """
    action = PENDING_ACTIONS.get(name)
    if action is None:
        logger.warning('未知的待执行动作，已忽略：%s', name)
        return
    logger.info('提权重启后自动执行动作：%s', name)
    root.after(800, lambda: actions.run_action(action))


def main():
    """程序入口：日志 -> 权限探测 -> 单实例 -> 配置 -> 状态检测 -> 窗口 + 托盘 -> 主循环。

    入参：无。返回值：无（主循环结束后正常返回）。
    """
    if sys.platform != 'win32':
        sys.stderr.write('本工具仅支持 Windows 平台。\n')
        sys.exit(1)

    setup_logging()  # 日志最先初始化：之后任何步骤失败都有迹可循

    # --- 权限：默认不起管理员，只有需要改 hosts 时才按需提权重启 ---
    pending = parse_pending_action(sys.argv[1:])
    if pending is not None and not admin.is_admin():
        # 说明是"提权重启"后的实例仍然拿不到权限（组策略等），直接报错退出，
        # 绝不再次弹 UAC 形成死循环。
        logger.error('提权重启后仍未获得管理员权限（动作：%s），程序退出', pending)
        fatal_popup('未能获得管理员权限，无法执行该操作。')
        sys.exit(1)
    state.is_admin = admin.is_admin()
    logger.info('===== 程序启动（%s）=====',
                '管理员权限' if state.is_admin else '受限模式（普通用户）')
    ensure_single_instance()

    # --- 配置：只读一次，注入 service（各模块不重复 IO） ---
    config = config_manager.load_config()
    service.init(config)

    # --- 启动状态检测：以 hosts 磁盘内容为准（程序可能上次没正常退出）---
    # 此时还没有任何订阅者，直接赋值即可；窗口 / 托盘构造时会读它作为初始状态
    state.enabled = service.detect_enabled_on_startup()
    service.reload_candidates_state()  # 候选列表装载到状态（窗口表格的数据源）
    if state.enabled and not state.is_admin:
        logger.warning('hosts 处于加速状态但当前为受限模式：停用/还原需要提权')
        state.set_message('加速处于启用状态，停用需要管理员权限')
    proxy_info = proxy_detector.detect_proxy()
    state.proxy = proxy_info
    if proxy_info['enabled']:
        description = proxy_detector.describe_proxy(proxy_info)
        logger.warning('检测到系统代理（%s）：hosts 加速可能不适用，'
                       '必要时可改用「下载加速链接」的镜像方案', description)
        state.set_message(f'检测到系统代理：{description}，hosts 加速可能不适用')

    # --- 应用图标：任务栏分组 ID + 生成 icon.png / icon.ico（缺失时） ---
    app_icon.set_app_user_model_id()
    app_icon.ensure_icon_files()

    # --- 高 DPI：必须在创建任何窗口之前声明感知级别（否则高分屏下界面发虚）---
    dpi.enable_dpi_awareness()

    # --- 主窗口（主线程创建，启动即显示）+ 调度器注入 ---
    root = tk.Tk()
    dpi.tune_tk_scaling(root)
    install_exception_hooks(root)
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

    # --- 提权重启携带的动作：主循环起来后自动执行 ---
    if pending is not None:
        _schedule_pending_action(root, pending)

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
