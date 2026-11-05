# -*- coding: utf-8 -*-
"""网络测试：TCP 443 测速、GitHub 连通性测试、在线拉取 IP 列表。

三个能力（全部是纯函数式：给输入、返回结果，不碰界面、不碰全局状态）：
  1. test_ips_concurrent()   并发对一批 IP 做 TCP 443 连接测试；
  2. run_connectivity_test() 按"当前生效配置"做一次端到端连通性诊断；
  3. fetch_remote_hosts()    从 GitHub520 稳定地址拉取最新 hosts 数据。

为什么用"TCP 连接测试"而不是 ping / HTTP？
  - ping（ICMP）很多机房直接禁掉，结果不可信；
  - HTTP 会触发 TLS 握手和证书校验，慢且依赖证书链；
  - TCP 443 connect 只验证"这条网络路径能否到达 GitHub 的入口"，
    速度快（毫秒级）、开销小，和最终使用方式（HTTPS）路径一致。

core 层规则：本模块不 import 界面库、不写全局状态；测速结果原样返回，
记录到状态、广播事件是 service 层的事。
"""

import logging
import socket
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from core import hosts_manager

# 测速参数：TCP 端口 / 单次超时（秒）/ 并发线程上限
TEST_PORT = 443
TEST_TIMEOUT = 2.0
MAX_WORKERS = 16

# 连通性测试：TCP 连接超时（秒）—— 比测速更宽容，弱网也能测出结果
TEST_TIMEOUT_CONNECT = 3.0

# 在线 IP 列表来源（GitHub520 稳定地址，无需访问 GitHub 即可获取）及拉取超时
REMOTE_HOSTS_URL = 'https://raw.hellogithub.com/hosts'
FETCH_TIMEOUT = 10

logger = logging.getLogger(__name__)


def _tcp_connect(host, port, timeout):
    """对 host:port 做一次 TCP 连接测试。

    入参：host —— IP 或域名（域名会走系统解析，含 hosts 映射）；
          port —— 目标端口；timeout —— 超时秒数。
    返回值：(ms, err) —— 成功时 (延迟毫秒整数, None)；失败时 (None, 原因字符串)。

    实现细节：time.perf_counter 是单调高精度时钟，适合测耗时；
    with 语句确保 socket 无论成败都关闭，不泄漏句柄。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        start = time.perf_counter()
        try:
            sock.connect((host, port))
            return round((time.perf_counter() - start) * 1000.0), None
        except Exception as err:
            return None, f'{type(err).__name__}: {err}'


def _test_one_ip(ip):
    """对单个候选 IP 的 443 端口做一次 TCP 测速。

    入参：ip —— IP 字符串。
    返回值：(ms, err) —— 同 _tcp_connect。
    （独立成函数是因为 ThreadPoolExecutor.map 需要一元函数。）
    """
    return _tcp_connect(ip, TEST_PORT, TEST_TIMEOUT)


def test_ips_concurrent(ips):
    """并发测速一组唯一 IP（TCP 443，2 秒超时）。

    入参：ips —— IP 可迭代对象（内部自动去重排序）。
    返回值：dict —— {ip: (ms 或 None, err 或 None)}，包含每个 IP 的完整结果。

    为什么要并发？几十个 IP 串行测，每个最多等 2 秒，全超时要一分多钟；
    并发上限 16 线程，总耗时约等于"最慢的那一个"。
    为什么限制 16？线程太多会挤占本机带宽与连接表，测出的延迟也失真。
    """
    unique_ips = sorted(set(ips))
    results = {}
    logger.info('开始对 %d 个 IP 测速（TCP %d，超时 %.1f 秒，并发）',
                len(unique_ips), TEST_PORT, TEST_TIMEOUT)
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(unique_ips)))) as pool:
        for ip, outcome in zip(unique_ips, pool.map(_test_one_ip, unique_ips)):
            results[ip] = outcome
            ms, _ = outcome
            if ms is None:
                logger.info('测速失败 %s：%s', ip, _)
            else:
                logger.info('测速成功 %s：%d ms', ip, ms)
    return results


def pick_best_from_results(pairs, results):
    """按域名从测速结果中优选每个域名延迟最低的可用 IP。

    入参：pairs —— [(IP, 域名), ...] 候选清单（同域名可对应多个 IP）；
          results —— test_ips_concurrent 的返回值 {ip: (ms, err)}。
    返回值：dict —— {域名: 最优IP}；没有任何可用 IP 时为空 dict。
    """
    # 只保留连通的 IP 的延迟，用于比较大小
    latencies = {ip: ms for ip, (ms, _err) in results.items() if ms is not None}
    best = {}
    for ip, domain in pairs:
        if ip in latencies and (domain not in best
                                or latencies[ip] < latencies[best[domain]]):
            best[domain] = ip
    for domain, ip in best.items():
        logger.info('优选结果：%s -> %s（%d ms）', domain, ip, latencies[ip])
    skipped = sorted({d for _, d in pairs} - set(best))
    if skipped:
        logger.warning('以下域名无可用 IP，已跳过：%s', ', '.join(skipped))
    return best


def fetch_remote_hosts():
    """从 GitHub520 稳定地址拉取最新 hosts 数据。

    入参：无。
    返回值：(ok, data) —— 成功 (True, 响应文本)；失败 (False, 错误描述)。

    为什么不直接访问 GitHub 拿数据？—— 正是因为 GitHub 访问有问题才用本工具，
    所以数据源放在第三方稳定地址 raw.hellogithub.com。
    为什么失败返回元组而不是抛异常？网络失败是"正常业务分支"而非程序错误，
    用返回值表达可预期失败，用异常表达意外错误 —— 调用方不会漏处理。
    """
    logger.info('开始在线拉取 IP 列表：%s', REMOTE_HOSTS_URL)
    try:
        request = urllib.request.Request(
            REMOTE_HOSTS_URL, headers={'User-Agent': 'GitHubHostsAccelerator/4.0'})
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return True, response.read().decode('utf-8', errors='replace')
    except Exception as error:
        logger.warning('在线拉取失败：%s', error)
        return False, f'拉取失败：{error}'


def resolve_system_ip(domain):
    """用系统解析（DNS，含 hosts 映射）解析域名。

    入参：domain —— 域名字符串。
    返回值：(ok, data) —— 成功 (True, IP 字符串)；失败 (False, 错误描述)。
    用途：主窗口「手动添加」弹窗的「从系统获取」按钮 ——
    若加速已启用，解析会命中 hosts 里的优选 IP，方便快速补录条目。
    """
    try:
        return True, socket.gethostbyname(domain)
    except Exception as error:
        logger.warning('系统解析 %s 失败：%s', domain, error)
        return False, f'解析失败：{error}'


def run_connectivity_test():
    """做一次 GitHub 连通性诊断（不修改 hosts）。

    入参：无。
    返回值：dict —— 测试详情：
      {
        'time':         'YYYY-MM-DD HH:MM:SS',
        'github':       '45ms ✓' / '超时 ✗' / '解析失败 ✗',
        'raw':          同上（对应 raw.githubusercontent.com），
        'overall':      '可达' / '不可达',
        'summary':      'github.com: ... | raw: ... | 整体: ...'（气泡用），
        'summary_line': '✅ 整体连通正常' / '❌ 部分节点超时' / '❌ 整体不可达'，
      }

    测试路径设计：
      - 先查 hosts 中这两个域名当前生效的 IP（没配则回退系统 DNS），
        对"生效 IP"直连 —— 反映的是加速映射本身通不通；
      - 再对 github.com 域名做端到端连接 —— 解析会走系统 hosts，
        测的是用户实际访问走的完整路径。
    """
    details = {'time': time.strftime('%Y-%m-%d %H:%M:%S')}
    results = []
    for domain, label, key in (('github.com', 'github.com', 'github'),
                               ('raw.githubusercontent.com', 'raw', 'raw')):
        ip = hosts_manager.find_hosts_ip(domain)
        source = 'hosts'
        if ip is None:  # hosts 中未配置时回退系统 DNS 解析
            try:
                ip = socket.gethostbyname(domain)
                source = 'DNS'
            except Exception as err:
                logger.warning('连通性测试：%s 无法解析 IP：%s', domain, err)
                results.append(f'{label}: 解析失败 ✗')
                details[key] = '解析失败 ✗'
                continue
        ms, err = _tcp_connect(ip, TEST_PORT, TEST_TIMEOUT_CONNECT)
        if ms is not None:
            value = f'{ms}ms ✓'
            logger.info('连通性测试 %s（IP=%s，来源=%s）：%d ms', domain, ip, source, ms)
        else:
            value = ('超时' if 'timed out' in str(err).lower() else '失败') + ' ✗'
            logger.warning('连通性测试 %s（IP=%s，来源=%s）失败：%s',
                           domain, ip, source, err)
        results.append(f'{label}: {value}')
        details[key] = value

    # 端到端整体连通性（对域名连接，解析走系统 hosts，即真实生效路径）
    ms, err = _tcp_connect('github.com', TEST_PORT, TEST_TIMEOUT_CONNECT)
    if ms is not None:
        details['overall'] = '可达'
        logger.info('连通性测试 github.com 端到端连接成功：%d ms', ms)
    else:
        details['overall'] = '不可达'
        logger.warning('连通性测试 github.com 端到端连接失败：%s', err)
    results.append(f"整体: {details['overall']}")

    # 大字状态汇总：节点全部连通且端到端可达 -> 正常；部分节点失败 -> 部分超时
    nodes_ok = all(str(details.get(key, '')).endswith('✓')
                   for key in ('github', 'raw'))
    if details.get('overall') == '可达' and nodes_ok:
        details['summary_line'] = '✅ 整体连通正常'
    elif details.get('overall') == '可达':
        details['summary_line'] = '❌ 部分节点超时'
    else:
        details['summary_line'] = '❌ 整体不可达'

    details['summary'] = ' | '.join(results)
    logger.info('连通性测试结果：%s', details['summary'])
    return details
