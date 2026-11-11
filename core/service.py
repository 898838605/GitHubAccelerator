# -*- coding: utf-8 -*-
"""业务编排层：把 hosts_manager / network_tester / config_manager 串成完整流程。

这一层回答的问题是："点一下「启用加速」，程序到底做了哪几步？"
  在线更新(可选) -> 备份 -> 读候选 -> 并发测速 -> 按域名优选 -> 写 hosts -> 刷 DNS。

与旧版单文件的关键区别（拆分时的职责重划）：
  - 旧版业务函数里混着 notify_tray(...)（托盘气泡）—— 那是界面动作；
  - 现在 core 不认识托盘。本模块通过"通知器注入"（见 set_notifier）拿到一个
    普通函数来发气泡：main.py 启动时把托盘的通知函数注入进来。
    这叫依赖倒置 —— core 定义"我需要一个发通知的能力"，main 负责供给；
  - 对外每个操作函数统一返回 (ok, message)，ui 层据此刷新界面。

线程约定：本模块的函数都设计为"在后台线程中执行"（由 ui/actions 调度器
或 main 的定时线程调用）；它们只允许通过 state.publish 广播事件，
绝不允许直接操作 tkinter 控件。
"""

import logging
import os
import time

from core import config_manager
from core import download_helper
from core import git_accelerator
from core import hosts_manager
from core import network_tester
from core import proxy_helper
from core.app_state import op_cancel, op_lock, state
from utils import proxy as proxy_detector

logger = logging.getLogger(__name__)

# 运行期配置（main.py 启动时通过 init() 注入；不注入则用默认值）
_config = dict(config_manager.DEFAULT_CONFIG)


def init(config):
    """注入运行期配置（由 main.py 在启动时调用一次）。

    入参：config —— config_manager.load_config() 的返回值。
    返回值：无。
    为什么用注入而不是本模块自己去读文件？配置只该读一次，
    由入口统一读、统一分发，各模块不重复 IO。
    """
    global _config
    _config = config


# ---------------------------------------------------------------------------
# 通知器注入（依赖倒置：core 不认识托盘，只认识"一个能发通知的函数"）
# ---------------------------------------------------------------------------

_notifier = None    # main.py 注入的托盘气泡函数，签名 fn(message, popup=True)


def set_notifier(fn):
    """注入通知函数（托盘气泡）；传 None 表示禁用（如单元测试）。

    入参：fn —— fn(message, popup=True)。返回值：无。
    """
    global _notifier
    _notifier = fn


def notify_user(message, popup=True):
    """用注入的通知函数发气泡；未注入 / 失败时静默降级为日志。

    入参：message —— 气泡文字；popup —— 托盘不可用时是否允许降级为系统弹窗。
    返回值：无。
    供本模块内部与 ui 层（如操作调度器的"请稍候"提示）共同使用。
    """
    if message:
        # 同步到状态栏「最近提示」：托盘气泡可能被 Windows 专注助手静默丢弃，
        # 主窗口信息行是用户更可靠的反馈通道。
        try:
            state.set_message(message)
        except Exception:
            logger.exception('写入状态栏提示失败：%s', message)
    if _notifier is None:
        logger.info('（通知器未注入，仅记录）%s', message)
        return
    try:
        _notifier(message, popup=popup)
    except Exception:
        logger.exception('发送通知失败：%s', message)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _save_config_values(values):
    """把少量配置变更合并写回 config.json，并同步内存配置。"""
    global _config
    _config.update(values)
    try:
        config_manager.merge_config_values(values)
    except Exception:
        logger.warning('保存配置失败（本次运行仍生效）', exc_info=True)


# ---------------------------------------------------------------------------
# Git 镜像加速（v5.2 核心：不依赖管理员/代理，跨系统可用）
# ---------------------------------------------------------------------------

def _publish_git_mirror(status):
    """写入全局状态并广播 git_mirror 事件。"""
    state.git_mirror = {
        'enabled': bool(status.get('enabled')),
        'prefix': status.get('prefix') or '',
        'detail': status.get('detail') or '未启用',
    }
    state.publish('git_mirror')


def refresh_git_mirror_status():
    """重新读取 Git 镜像状态（启动时调用）。返回值：status dict。"""
    try:
        status = git_accelerator.status()
    except Exception:
        logger.exception('读取 Git 镜像状态失败')
        status = {'git_found': False, 'enabled': False, 'prefix': '',
                  'detail': '读取失败'}
    _publish_git_mirror(status)
    return status


def git_mirror_toggle():
    """Git 镜像加速开关（供按钮/托盘调用）。

    入参：无。返回值：(ok, message)。
    """
    status = git_accelerator.status()
    if not status.get('git_found'):
        message = '未检测到 Git，请先安装 Git for Windows'
        notify_user(message)
        return False, message
    if status.get('enabled'):
        return git_mirror_disable()
    return git_mirror_enable()


def git_mirror_enable(preferred=None):
    """启用 Git 镜像加速。返回值：(ok, message)。"""
    preferred = preferred or _config.get('preferred_git_mirror') or None
    state.set_progress('正在测速 Git 镜像…', 0, 0)
    try:
        ok, message, prefix = git_accelerator.enable(preferred)
    except Exception as error:
        logger.exception('启用 Git 镜像失败')
        ok, message, prefix = False, f'启用 Git 镜像失败：{error}', None
    state.set_progress('')
    if ok:
        _save_config_values({'git_mirror_enabled': True,
                             'preferred_git_mirror': prefix or ''})
    notify_user(message)
    _publish_git_mirror(git_accelerator.status())
    return ok, message


def git_mirror_disable():
    """关闭 Git 镜像加速。返回值：(ok, message)。"""
    state.set_progress('正在还原 Git 配置…', 0, 0)
    try:
        ok, message = git_accelerator.disable()
    except Exception as error:
        logger.exception('关闭 Git 镜像失败')
        ok, message = False, f'关闭 Git 镜像失败：{error}'
    state.set_progress('')
    if ok:
        _save_config_values({'git_mirror_enabled': False})
    notify_user(message)
    _publish_git_mirror(git_accelerator.status())
    return ok, message


def test_download_mirrors():
    """测试下载镜像节点并记录最快节点。返回值：(ok, message)。"""
    state.set_progress('正在测试下载镜像节点…', 0, 0)
    try:
        results = download_helper.probe_mirrors()
    except Exception as error:
        logger.exception('测试下载镜像失败')
        state.set_progress('')
        message = f'测试下载镜像失败：{error}'
        notify_user(message)
        return False, message
    state.set_progress('')
    best = download_helper.pick_fastest_mirror(results)
    detail = '、'.join(
        f"{item['prefix'].rstrip('/').replace('https://', '')}={item['ms']}ms"
        for item in results[:3
        ] if item['ok'])
    if best is None:
        message = '所有下载镜像都不可用，请检查网络'
        notify_user(message)
        return False, message
    _save_config_values({'preferred_mirror': best['prefix']})
    message = f"下载镜像测速完成，已选最快节点：{best['prefix']}（{best['ms']} ms）"
    if detail:
        message += f'；{detail}'
    notify_user(message)
    return True, message


# ---------------------------------------------------------------------------
# 代理检测 / Git 代理（网页加速必须依赖代理）
# ---------------------------------------------------------------------------

def refresh_proxy_status():
    """检测并验证系统代理，写入状态并广播。返回值：status dict。"""
    try:
        status = proxy_helper.detect_and_verify()
    except Exception:
        logger.exception('检测代理失败')
        status = {'detected': False, 'server': None, 'verified': False,
                  'detail': '检测失败'}
    state.proxy_status = status
    state.proxy = status.get('info')
    state.publish('proxy_status')
    return status


def git_proxy_toggle():
    """Git 代理开关：已有则清除，没有则使用检测到的系统代理。"""
    applied = proxy_helper.get_git_proxy()
    if applied.get('http.proxy') or applied.get('https.proxy'):
        ok, message = proxy_helper.clear_git_proxy()
        notify_user(message)
        return ok, message
    status = refresh_proxy_status()
    if not status.get('detected'):
        message = '未检测到系统代理；网页/浏览器加速需要先开启代理软件'
        notify_user(message)
        return False, message
    server = status.get('server')
    if not server:
        message = '检测到的是 PAC 自动配置脚本，暂不支持自动写入 Git 代理'
        notify_user(message)
        return False, message
    ok, message = proxy_helper.set_git_proxy(server)
    notify_user(message)
    return ok, message


def _detect_hosts_limitation(best):
    """hosts 写入后检查 github.com 的真实 HTTPS 是否可用。

    入参：best —— {域名: IP} 映射。返回值：str|None —— 需要提示的说明。
    """
    ip = best.get('github.com')
    if not ip:
        return None
    try:
        result = network_tester.probe_https(
            'github.com', ip, path='/', max_bytes=196608, timeout=6)
    except Exception:
        logger.debug('hosts 有效性探测失败（忽略）', exc_info=True)
        return None
    if result.get('ok'):
        return None
    logger.warning('hosts 已写入，但 github.com HTTPS 传输受限：%s', result)
    return ('github.com HTTPS 传输受限（hosts 只能改 DNS，无法绕过网络限速）；'
            '建议开启「Git 镜像加速」或使用代理')


def _record_test_results(results):
    """把并发测速结果写入全局状态并广播（表格的延迟 / 状态列数据源）。

    入参：results —— network_tester.test_ips_concurrent 的返回值
           {ip: (ms, err)}。返回值：无。
    """
    for ip, (ms, err) in results.items():
        state.set_ip_result(ip, ms, err)
    state.publish('candidates_changed')  # 一次性广播，表格只刷新一次


def _filter_pairs_with_notice(pairs, source):
    """按白名单 / 公网 IP 规则过滤候选，并把被忽略的条目告知用户。

    入参：pairs —— [(IP, 域名), ...]；source —— 来源描述（日志定位用）。
    返回值：list[(ip, domain)] —— 可安全写入 hosts 的条目。
    """
    allowed, rejected = hosts_manager.filter_allowed_entries(pairs)
    if rejected:
        sample = '、'.join(f'{ip} {domain}' for ip, domain in rejected[:5])
        more = f' 等 {len(rejected)} 条' if len(rejected) > 5 else ''
        logger.warning('%s中有 %d 条不在白名单或非公网 IP，已忽略：%s%s',
                       source, len(rejected), sample, more)
        state.set_message(f'已忽略 {len(rejected)} 条不合规候选：{sample}{more}')
    return allowed


# ---------------------------------------------------------------------------
# 取消 / 进度小工具（长流程各阶段共用）
# ---------------------------------------------------------------------------

CANCEL_MESSAGE = '操作已取消'


def _cancelled():
    """当前操作是否已被用户取消。返回值：bool。"""
    return op_cancel.is_set()


def _cancelled_result():
    """已取消时统一收尾：清进度、记日志、提示用户，返回 (False, 取消消息)。"""
    logger.info('用户取消，终止当前操作')
    state.set_progress('')
    notify_user(CANCEL_MESSAGE)
    return False, CANCEL_MESSAGE


def _test_progress_text(prefix, done, total):
    """生成"正在测速（3/75）…"这类进度文案。"""
    return f'{prefix}（{done}/{total}）…' if total else f'{prefix}…'


def _apply_tls_results(best, verified_results):
    """根据 TLS 校验结果剔除未通过的域名。

    入参：best —— {域名: IP} 优选结果；verified_results —— {域名: (ok, reason)}。
    返回值：(filtered_best, dropped_domains) —— 保留的映射与被剔除的域名列表。
    """
    dropped = sorted(domain for domain, (ok, _reason) in verified_results.items()
                     if not ok)
    if dropped:
        logger.warning('TLS 校验未通过，已跳过 %d 个域名：%s',
                       len(dropped), '、'.join(dropped))
    return {d: ip for d, ip in best.items() if d not in dropped}, dropped


def reload_candidates_state():
    """重新读取候选文件并同步到全局状态（ui「刷新列表」/启动时调用）。

    入参：无。返回值：bool —— 是否成功（失败已记日志）。
    """
    try:
        state.set_candidates(hosts_manager.load_candidate_pairs())
        return True
    except Exception as error:
        logger.exception('读取候选列表失败：%s', error)
        return False


def detect_enabled_on_startup():
    """启动时检测系统 hosts 是否已写入加速段。

    入参：无。
    返回值：bool —— True 表示加速段已存在（视为"启用中"）。
    为什么以 hosts 内容为准而不是内存状态？程序可能上次没正常退出，
    磁盘上的 hosts 才是事实。
    """
    try:
        text = hosts_manager.read_hosts_text()
        enabled = hosts_manager.START_MARKER in text
        if enabled and hosts_manager.END_MARKER not in text:
            # 上次写入被打断（只有开始标记）：界面仍按"启用中"处理，
            # 让用户能通过「停用/强制还原」把它清掉（还原支持无备份兜底）。
            logger.warning('hosts 中存在未闭合的加速段，上次写入可能被中断')
        if enabled and not os.path.exists(hosts_manager.BACKUP_PATH):
            logger.warning('加速段存在但备份缺失：停用时会改用"按标记移除"兜底')
        logger.info('启动检测：加速段%s',
                    '已写入 hosts（启用中）' if enabled else '未写入（未启用）')
        return enabled
    except Exception as error:
        logger.exception('启动时读取 hosts 失败：%s', error)
        return False


# ---------------------------------------------------------------------------
# 核心业务操作（托盘菜单与主窗口按钮共用）
# ---------------------------------------------------------------------------

def enable(silent=False):
    """启用加速：在线更新(可选) -> 备份 -> 测速优选 -> TLS 校验 -> 写 hosts -> 刷 DNS。

    入参：silent —— True 时全程不弹气泡、只写日志（定时刷新复用同一流程）。
    返回值：(ok, message) —— ok 表示是否成功启用。

    每一步失败都提前返回（fail-fast），并且绝不留下"半启用"状态：
    写 hosts 是最后一步，它之前的失败都不碰系统文件。
    长流程在每个关键阶段都检查全局取消信号（op_cancel），用户点「取消」后
    最多再等当前这一小批网络请求结束就会退出。
    """
    logger.info('收到启用加速请求，开始处理')
    if _cancelled():
        return _cancelled_result()
    if not silent:
        notify_user('正在测速并启用加速，请稍候…', popup=False)
    state.set_progress('正在准备…', 0, 0)

    # 0. 按配置先在线更新 IP 列表；失败静默降级（本地列表照常能用）
    if _config.get('auto_update_on_enable'):
        state.set_message('正在在线更新 IP 列表…')
        state.set_progress('正在在线更新 IP 列表…', 0, 0)
        update_ip_list_online(silent=True)
        if _cancelled():
            return _cancelled_result()

    # 1. 备份原始 hosts（仅第一次创建，绝不覆盖已有备份）
    state.set_progress('正在备份 hosts…', 0, 0)
    ok, message = hosts_manager.ensure_backup()
    if not ok:
        notify_user(message)
        return False, message
    if _cancelled():
        return _cancelled_result()

    # 2. 读取并解析 IP 候选列表
    try:
        pairs = hosts_manager.load_candidate_pairs()
    except Exception as error:
        logger.exception('读取候选列表失败：%s', error)
        message = f'读取 github_hosts.txt 失败：{error}'
        notify_user(message)
        return False, message
    if not pairs:
        message = 'github_hosts.txt 中没有有效条目，请编辑后重试'
        notify_user(message)
        return False, message
    state.set_candidates(pairs)  # 同步主窗口表格（含被过滤条目，便于用户查看/清理）

    # 2.5 白名单 / 公网 IP 过滤：只有 GitHub 相关域名才能进入系统 hosts
    usable_pairs = _filter_pairs_with_notice(pairs, '候选列表')
    if usable_pairs:
        # 远端列表可能缺少核心域名或只剩不可达 IP：补充少量内置兜底候选，
        # 仍然要经过测速 / TLS 校验才会写入 hosts。
        supplemented = hosts_manager.with_builtin_fallbacks(usable_pairs)
        if len(supplemented) > len(usable_pairs):
            logger.info('已补充 %d 条内置兜底候选参与本次测速',
                        len(supplemented) - len(usable_pairs))
            usable_pairs = supplemented
    if not usable_pairs:
        message = '候选列表中没有可用的 GitHub 域名（已过滤不合规条目），请更新后重试'
        logger.error(message)
        notify_user(message)
        return False, message

    # 3. 并发测速 + 记录结果 + 按域名优选（带进度/取消/可配置超时）
    timeout = _config.get('test_timeout_seconds') or network_tester.TEST_TIMEOUT
    unique_ips = sorted({ip for ip, _ in usable_pairs})
    state.set_message(f'正在测速 {len(unique_ips)} 个 IP…')
    state.set_progress(_test_progress_text('正在测速', 0, len(unique_ips)), 0, len(unique_ips))

    def _on_test_progress(done, total):
        state.set_progress(_test_progress_text('正在测速', done, total), done, total)

    results = network_tester.test_ips_concurrent(
        unique_ips, timeout=timeout,
        progress_cb=_on_test_progress, cancel_event=op_cancel)
    _record_test_results(results)
    if _cancelled():
        return _cancelled_result()

    best = network_tester.pick_best_from_results(usable_pairs, results)
    if not best:
        message = '所有候选 IP 均不可用，请更新 github_hosts.txt'
        proxy_info = proxy_detector.detect_proxy()
        if proxy_info['enabled']:
            message += (f'（检测到系统代理：{proxy_detector.describe_proxy(proxy_info)}，'
                        'hosts 加速在代理网络下可能不适用，可改用「下载加速链接」）')
        logger.error(message)
        notify_user(message)
        return False, message

    # 核心域名缺失提示（例如本轮 github.com 全部候选不可达）：
    # 不阻断启用，但要让用户知道 hosts 里没有写入它，避免"显示已启用却没加速"的困惑。
    present_domains = {domain for _ip, domain in usable_pairs}
    missing_core = [domain for domain in ('github.com', 'api.github.com',
                                          'raw.githubusercontent.com')
                    if domain in present_domains and domain not in best]
    if missing_core:
        logger.warning('核心域名没有可用 IP，未写入 hosts：%s', '、'.join(missing_core))

    # 4. 严格 TLS 校验：同一域名有多个候选时，首选 IP 握手失败自动换备选，
    #    避免"TCP 可用但单个 IP 的 TLS 偶发超时"导致整个域名被跳过。
    tls_dropped = {}
    tls_fallback = {}
    if _config.get('strict_tls_verify', True):
        try:
            candidate_limit = max(1, int(_config.get('tls_candidate_limit', 3) or 3))
        except (TypeError, ValueError):
            candidate_limit = 3

        candidates_by_domain = {}
        for ip, domain in usable_pairs:
            if domain not in network_tester.TLS_VERIFY_DOMAINS:
                continue
            ms = results.get(ip, (None, None))[0]
            if ms is not None:
                candidates_by_domain.setdefault(domain, []).append((ms, ip))

        domain_candidates = {}
        checks = []
        for domain, best_ip in best.items():
            if domain not in network_tester.TLS_VERIFY_DOMAINS:
                continue
            ordered = [(results.get(best_ip, (None, None))[0] or 0, best_ip)]
            for ms, ip in sorted(candidates_by_domain.get(domain, [])):
                if ip != best_ip:
                    ordered.append((ms, ip))
            chosen = []
            for _ms, ip in ordered:
                if ip not in chosen:
                    chosen.append(ip)
            chosen = chosen[:candidate_limit]
            domain_candidates[domain] = chosen
            checks.extend((domain, ip) for ip in chosen)

        if checks:
            state.set_progress(_test_progress_text('正在校验 TLS 证书', 0, len(checks)),
                               0, len(checks))

            def _on_tls_progress(done, total):
                state.set_progress(
                    _test_progress_text('正在校验 TLS 证书', done, total), done, total)

            verified = network_tester.verify_tls_candidates(
                checks, progress_cb=_on_tls_progress, cancel_event=op_cancel,
                attempts=2)
            if _cancelled():
                return _cancelled_result()

            selected = {domain: ip for domain, ip in best.items()
                        if domain not in domain_candidates}
            fallback_enabled = _config.get('tls_failure_fallback', True)
            for domain, ips in domain_candidates.items():
                chosen = None
                reasons = []
                for ip in ips:
                    ok, reason = verified.get((domain, ip),
                                              (False, network_tester.REASON_TLS))
                    if ok:
                        chosen = ip
                        break
                    reasons.append(reason or network_tester.REASON_TLS)
                if chosen:
                    selected[domain] = chosen
                    if chosen != best[domain]:
                        logger.info('TLS 首选失败，改用备选 IP：%s -> %s（原 %s）',
                                    domain, chosen, best[domain])
                elif fallback_enabled and network_tester.REASON_TLS not in reasons:
                    # 握手被重置 / 超时属于网络干扰，不是证书错误：
                    # 仍按 TCP 可用写入，避免因为一次握手被 reset 就整个域名不加速。
                    selected[domain] = best[domain]
                    tls_fallback[domain] = best[domain]
                    logger.warning(
                        'TLS 校验受网络干扰（%s），仍按 TCP 可用写入：%s -> %s',
                        '、'.join(str(item) for item in reasons),
                        domain, best[domain])
                else:
                    tls_dropped[domain] = reasons[-1] if reasons else network_tester.REASON_TLS
                    logger.warning('TLS 校验全部失败，未写入：%s（候选：%s，原因：%s）',
                                   domain, '、'.join(ips),
                                   '、'.join(str(item) for item in reasons))
            best = selected
            if not best:
                message = ('核心域名的 TLS 校验全部未通过，已取消写入 hosts。'
                           '若你处在代理 / 特殊网络环境，可在 config.json 中把 '
                           'strict_tls_verify 设为 false 后重试。')
                logger.error(message)
                notify_user(message)
                return False, message
            if tls_dropped and not silent:
                state.set_message(
                    f'已跳过 TLS 校验未通过的域名：{"、".join(sorted(tls_dropped))}')

    # TLS 之后重新计算核心域名缺失：区分"无可用 IP"与"TLS 未通过"，
    # 让日志/气泡/连通性区都能给出准确原因。
    missing_core = [domain for domain in ('github.com', 'api.github.com',
                                          'raw.githubusercontent.com')
                    if domain in present_domains and domain not in best]

    # 5. 写入 hosts 加速段（先移除旧段，再把新段置顶，保证"先匹配先赢"命中）
    if _cancelled():
        return _cancelled_result()
    state.set_progress('正在写入 hosts…', 0, 0)
    state.set_message('正在写入 hosts…')
    try:
        mismatched = hosts_manager.write_accel_section(best)
    except Exception as error:
        logger.exception('写入 hosts 失败')
        message = f'写入 hosts 失败：{error}'
        notify_user(message)
        return False, message
    if mismatched:
        # 写入"成功"但实际没生效：典型原因是 hosts 被其他软件并发改写/占用
        logger.error('hosts 自检未通过：%s', '；'.join(mismatched))
        notify_user('hosts 已写入，但部分域名未生效，请检查是否有其他软件在修改 hosts')

    # 6. 刷新 DNS（失败不致命，只提示）并更新状态 / 图标
    if not hosts_manager.flush_dns() and not silent:
        notify_user('DNS 刷新失败，可手动执行 ipconfig /flushdns')
    state.set_enabled(True)
    state.publish('accel_changed')  # 生效条目数刷新
    state.set_progress('')
    message = f'加速已启用，优选写入 {len(best)} 个域名'
    if missing_core:
        no_ip = [domain for domain in missing_core if domain not in tls_dropped]
        reasons = []
        if no_ip:
            reasons.append('无可用 IP')
        if len(no_ip) < len(missing_core):
            reasons.append('TLS 校验未通过')
        message += f'；未写入：{"、".join(missing_core)}（{"，".join(reasons)}）'
    if tls_fallback:
        message += f'；TLS 被网络干扰，已按 TCP 可用写入：{"、".join(sorted(tls_fallback))}'
    if not silent:
        limitation = _detect_hosts_limitation(best)
        if limitation:
            message += f'；注意：{limitation}'
    logging.info('启用加速完成，共写入 %d 个域名的优选映射', len(best))
    if not silent:
        notify_user(message)
    return True, message


def disable():
    """停用加速：用备份覆盖系统 hosts 并刷新 DNS。

    入参：无。返回值：(ok, message)。
    """
    logger.info('收到停用加速请求')
    ok, message = hosts_manager.restore_from_backup()
    if ok:
        _set_disabled(f'加速已停用：{message}')
        return True, message
    logger.warning('停用加速未执行：%s', message)
    notify_user(message)
    return False, message


def force_restore():
    """强制还原原始 hosts（不依赖内部状态，只要备份存在就覆盖）。

    入参：无。返回值：(ok, message)。
    与 disable 的区别：disable 是"回到启用前的状态"，force_restore 是
    "无论如何把备份盖回去"（比如 hosts 被弄乱了用来救命）。
    """
    logger.info('收到强制还原请求')
    ok, message = hosts_manager.restore_from_backup()
    if ok:
        _set_disabled('已强制还原原始 hosts')
        return True, message
    logger.warning('强制还原失败：%s', message)
    notify_user(message)
    return False, message


def _set_disabled(ok_message):
    """停用 / 还原成功后的公共收尾：发气泡 + 更新状态。"""
    notify_user(ok_message)
    state.set_enabled(False)
    state.publish('accel_changed')  # 生效条目数清零刷新


# ---------------------------------------------------------------------------
# 在线更新 / 测速 / 连通性
# ---------------------------------------------------------------------------

def update_ip_list_online(silent=False):
    """在线更新 IP 列表：拉取 -> 解析 -> 合并写回候选文件 -> 同步界面。

    入参：silent —— True 时不弹气泡（启用前自动更新 / 定时刷新场景）。
    返回值：(ok, message, added, total) —— added/total 供界面文案使用。
    """
    state.set_progress('正在在线更新 IP 列表…', 0, 0)
    ok, data = network_tester.fetch_remote_hosts()
    if _cancelled():
        return False, CANCEL_MESSAGE, 0, 0
    if not ok:
        logger.warning('在线更新失败（拉取阶段）：%s', data)
        if not silent:
            state.set_message('更新失败，继续使用本地列表')
            notify_user('更新失败，继续使用本地列表')
        return False, data, 0, 0

    if _cancelled():
        return False, CANCEL_MESSAGE, 0, 0
    remote_pairs = hosts_manager.parse_pairs(data, 'GitHub520 数据')
    if not remote_pairs:
        logger.warning('在线更新失败：远端数据没有可解析条目（返回格式可能已变化）')
        if not silent:
            state.set_message('更新失败，继续使用本地列表')
            notify_user('更新失败，继续使用本地列表')
        return False, '远端数据解析为空', 0, 0

    # 远端数据是不可信输入：先按域名白名单 + 公网 IP 过滤，再允许合并进本地文件。
    # 这条过滤同时保证了"被投毒的远端数据"无法把任意域名写进系统 hosts。
    remote_pairs, rejected = hosts_manager.filter_allowed_entries(remote_pairs)
    if rejected:
        sample = '、'.join(f'{ip} {domain}' for ip, domain in rejected[:5])
        logger.warning('在线更新：已过滤 %d 条不合规远端数据（示例：%s）',
                       len(rejected), sample)
    if not remote_pairs:
        logger.warning('在线更新失败：远端数据过滤后为空')
        if not silent:
            state.set_message('更新失败，继续使用本地列表')
            notify_user('更新失败，继续使用本地列表')
        return False, '远端数据过滤后为空', 0, 0

    if _cancelled():
        return False, CANCEL_MESSAGE, 0, 0
    try:
        added, total = hosts_manager.merge_remote_into_local(remote_pairs)
    except Exception as error:
        logger.exception('在线更新失败（合并阶段）：%s', error)
        if not silent:
            state.set_message('更新失败，继续使用本地列表')
            notify_user('更新失败，继续使用本地列表')
        return False, f'合并失败：{error}', 0, 0

    logger.info('在线更新成功：解析 %d 条，新增 %d 条，合并后共 %d 条候选',
                len(remote_pairs), added, total)
    state.set_progress('')
    state.last_refresh_time = time.time()
    state.publish('refresh_time')     # 状态栏"上次刷新时间"更新
    reload_candidates_state()         # 同步主窗口表格
    message = f'已更新 {added} 条 IP 记录（共 {total} 条候选）'
    if rejected:
        message += f'，已过滤 {len(rejected)} 条不合规数据'
    if not silent:
        notify_user(message)
    return True, message, added, total


def test_selected(pairs):
    """测速选中的候选条目（按 IP 去重），带进度与取消。

    入参：pairs —— [(IP, 域名), ...]。返回值：(ok, message)。
    """
    unique_ips = sorted({ip for ip, _ in pairs})
    logger.info('开始测速选中的 %d 个 IP', len(unique_ips))
    timeout = _config.get('test_timeout_seconds') or network_tester.TEST_TIMEOUT
    state.set_progress(_test_progress_text('正在测速选中项', 0, len(unique_ips)),
                       0, len(unique_ips))

    def _on_progress(done, total):
        state.set_progress(_test_progress_text('正在测速选中项', done, total),
                           done, total)

    results = network_tester.test_ips_concurrent(
        unique_ips, timeout=timeout, progress_cb=_on_progress, cancel_event=op_cancel)
    _record_test_results(results)
    if _cancelled():
        state.set_progress('')
        state.set_message(CANCEL_MESSAGE)
        return False, CANCEL_MESSAGE
    state.set_progress('')
    message = f'选中测速完成（{len(unique_ips)} 个 IP）'
    state.set_message(message)  # run_action 不消费返回值，反馈走信息行事件
    return True, message


def test_all():
    """「全部测速」：并发测速所有候选 IP，带进度与取消。

    入参：无。返回值：(ok, message)。
    """
    ips = [ip for ip, _ in state.candidates]
    if not ips:
        state.set_message('候选列表为空，无需测速')
        return False, '候选列表为空，无需测速'
    logger.info('开始测速全部 %d 条候选', len(ips))
    timeout = _config.get('test_timeout_seconds') or network_tester.TEST_TIMEOUT
    state.set_progress(_test_progress_text('正在全部测速', 0, len(ips)), 0, len(ips))

    def _on_progress(done, total):
        state.set_progress(_test_progress_text('正在全部测速', done, total), done, total)

    results = network_tester.test_ips_concurrent(
        ips, timeout=timeout, progress_cb=_on_progress, cancel_event=op_cancel)
    _record_test_results(results)
    if _cancelled():
        state.set_progress('')
        state.set_message(CANCEL_MESSAGE)
        return False, CANCEL_MESSAGE
    state.set_progress('')
    ok_count = sum(1 for ms, _reason in results.values() if ms is not None)
    message = f'全部测速完成：{ok_count}/{len(ips)} 个 IP 可用'
    state.set_message(message)  # run_action 不消费返回值，反馈走信息行事件
    return True, message


def test_connectivity():
    """测试 GitHub 连通性：结果写状态、广播事件、发气泡。

    入参：无。
    返回值：dict —— network_tester.run_connectivity_test() 的详情。
    """
    details = network_tester.run_connectivity_test()
    # 代理检测：hosts 加速不改变流量走向，代理网络下直连 IP 可能不可达，
    # 把结果一并展示，避免用户误以为"加速没生效"。
    proxy_info = proxy_detector.detect_proxy()
    details['proxy'] = proxy_detector.describe_proxy(proxy_info)
    details['proxy_enabled'] = proxy_info['enabled']
    if proxy_info['enabled']:
        logger.warning('检测到系统代理：%s',
                       proxy_detector.describe_proxy(proxy_info))
        details['summary'] += ' | 代理: 已启用'
    state.last_connectivity = details
    state.publish('connectivity', details)
    notify_user(details['summary'])
    return details


# ---------------------------------------------------------------------------
# 候选条目管理（主窗口「手动添加 / 删除 / 编辑」入口）
# ---------------------------------------------------------------------------

def add_candidate(ip, domain):
    """添加一条候选条目并同步全局状态。

    入参：ip / domain —— 已由 ui 层校验合法的字符串。返回值：(ok, message)。
    （服务层再校验一次：即使调用方漏校验，也不会让任意域名进入候选。）
    """
    if not hosts_manager.is_public_address(ip) or not hosts_manager.is_allowed_domain(domain):
        message = '仅支持公网 IPv4 / IPv6 地址 + GitHub 相关域名'
        logger.warning('拒绝添加不合规候选：%s %s', ip, domain)
        state.set_message(message)
        return False, message
    hosts_manager.add_candidate_pair(ip, domain)
    logger.info('已添加候选条目：%s %s', ip, domain)
    reload_candidates_state()
    return True, f'已添加 {domain} -> {ip}'


def remove_candidates(pairs):
    """删除候选条目并同步全局状态。

    入参：pairs —— [(IP, 域名), ...]。返回值：(ok, message)。
    """
    removed = hosts_manager.remove_candidate_pairs(set(pairs))
    logger.info('已删除候选条目 %d 条', removed)
    reload_candidates_state()
    return True, f'已删除 {removed} 条候选'


def edit_candidate_ip(old_ip, old_domain, new_ip):
    """修改候选条目的 IP 并同步全局状态。

    入参：old_ip / old_domain —— 定位旧行；new_ip —— 新 IP。
    返回值：(ok, message)。
    """
    if not hosts_manager.is_public_address(new_ip):
        message = f'IP 不合法（需要公网 IPv4 / IPv6）：{new_ip}'
        logger.warning('拒绝修改候选 IP：%s %s -> %s', old_ip, old_domain, new_ip)
        state.set_message(message)
        return False, message
    if hosts_manager.replace_candidate_pair(old_ip, old_domain, new_ip):
        logger.info('已修改候选条目：%s %s -> %s %s', old_ip, old_domain, new_ip, old_domain)
        message = f'已修改 {old_domain}：{old_ip} -> {new_ip}'
    else:
        logger.warning('未找到要修改的候选行：%s %s', old_ip, old_domain)
        message = f'未找到要修改的行：{old_ip} {old_domain}'
    reload_candidates_state()
    return True, message


def open_log_file():
    """用系统默认编辑器打开日志文件。

    入参：无。
    返回值：(ok, message)。
    """
    # 注意：必须用"模块属性"方式读取 LOG_PATH（调用时取最新值），
    # 如果写 from utils.logger import LOG_PATH 会在导入瞬间绑定旧值
    # （setup_logging 还没跑，拿到 None）。
    from utils import logger as log_utils
    path = log_utils.LOG_PATH
    logger.info('收到打开日志请求：%s', path)
    try:
        if path and os.path.exists(path):
            os.startfile(path)   # Windows shell 打开（用默认 .log 关联程序）
            return True, '已打开日志'
        logger.warning('日志文件不存在：%s', path)
        return False, '日志文件不存在'
    except Exception as error:
        logger.exception('打开日志失败')
        return False, f'打开日志失败：{error}'


# ---------------------------------------------------------------------------
# 定时自动刷新（线程体，由 main.py 启动守护线程执行）
# ---------------------------------------------------------------------------

def refresh_scheduler_loop(stop_event):
    """定时自动刷新守护线程的循环体：每隔 N 小时完整刷新一次。

    入参：stop_event —— threading.Event，退出时由 main.py 置位以结束线程。
    返回值：无（线程体）。

    完整流程 = 在线更新 -> 测速选优 -> 更新 hosts 段（复用 enable(silent=True)）。
    三个跳过条件（安全第一）：
      - 当前未启用加速 -> 不动 hosts（用户明确选择不用加速）；
      - 有其他操作在跑 -> 抢不到 op_lock 就跳过本次，避免争抢 hosts；
      - hosts 备份缺失 -> 绝不把"已加速的 hosts"备份成"原始 hosts"。
    """
    interval_hours = _config.get('refresh_interval_hours', 6)
    interval_seconds = max(1, interval_hours) * 3600
    logger.info('定时自动刷新线程已启动，间隔 %s 小时', interval_hours)
    while not stop_event.is_set():
        state.next_refresh_time = time.time() + interval_seconds
        state.publish('refresh_time')          # 状态栏倒计时数据源
        # 用 wait 代替 sleep：退出时能立刻被唤醒，而不是傻等几小时
        if stop_event.wait(interval_seconds):
            break
        try:
            logger.info('定时自动刷新触发')
            op_cancel.clear()  # 清掉上一次用户取消留下的信号
            if not state.enabled:
                logger.info('当前未启用加速，跳过本次定时刷新')
                if not state.is_admin:
                    logger.info('受限模式下跳过定时刷新（写 hosts 需要管理员权限）')
                    continue
                continue
            if not _acquire_op_lock():
                logger.info('有其他操作正在执行，跳过本次定时刷新')
                continue
            try:
                if not os.path.exists(hosts_manager.BACKUP_PATH):
                    logger.warning('hosts 备份缺失，跳过定时刷新（不修改 hosts）')
                    continue
                enable(silent=True)
                logger.info('定时自动刷新完成')
            finally:
                _release_op_lock()
        except Exception:
            # 定时任务挂了不能拖垮整个程序：记录后继续下一轮
            logging.exception('定时自动刷新异常（已忽略，程序继续运行）')


def _acquire_op_lock():
    """尝试获取全局操作锁（不阻塞）。返回值：bool 是否成功。"""
    return op_lock.acquire(blocking=False)


def _release_op_lock():
    """释放全局操作锁。返回值：无。"""
    try:
        op_lock.release()
    except RuntimeError:
        # 未持锁时释放（理论上不会发生）：记录而非静默吞掉
        logger.warning('释放操作锁失败：当前线程并未持有该锁', exc_info=True)


# 「需要管理员权限」标记：ui.actions 在受限模式（非管理员）下据此触发按需提权，
# 而不是等到写 hosts 时报 PermissionError。
enable.requires_admin = True
disable.requires_admin = True
force_restore.requires_admin = True
