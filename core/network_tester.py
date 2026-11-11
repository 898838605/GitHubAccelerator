# -*- coding: utf-8 -*-
"""网络测试：TCP 443 测速、TLS 证书校验、GitHub 连通性测试、在线拉取 IP 列表。

四个能力（全部是纯函数式：给输入、返回结果，不碰界面、不碰全局状态）：
  1. test_ips_concurrent()   并发对一批 IP 做 TCP 443 连接测试（支持进度/取消）；
  2. verify_tls_batch()      对"优选结果"做严格 TLS 握手校验（证书 + 域名）；
  3. run_connectivity_test() 按"当前生效配置"做一次端到端连通性诊断；
  4. fetch_remote_hosts()    从 GitHub520 稳定地址拉取最新 hosts 数据。

为什么用"TCP 连接测试"而不是 ping / HTTP？
  - ping（ICMP）很多机房直接禁掉，结果不可信；
  - HTTP 会触发 TLS 握手和证书校验，慢且依赖证书链；
  - TCP 443 connect 只验证"这条网络路径能否到达 GitHub 的入口"，
    速度快（毫秒级）、开销小，和最终使用方式（HTTPS）路径一致。

v4.2 的准确性改进（P1-4）：
  - 同一 IP 连接成功后**重复采样取最小值**，降低单次 RTT 抖动带来的误判
    （失败不重试，避免弱网下把总耗时翻倍）；
  - 增加**严格模式**：优选结果再对核心域名做一次真正的 TLS 握手
    （校验证书链 + 主机名），把"能建连但 TLS 被劫持/代理"的 IP 剔除；
  - 失败原因归一化（超时 / 拒绝连接 / 不可达 / 解析失败 / TLS 校验失败），
    界面直接展示中文原因，日志保留底层异常细节。

core 层规则：本模块不 import 界面库、不写全局状态；测速结果原样返回，
记录到状态、广播事件是 service 层的事。
"""

import errno
import logging
import socket
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import hosts_manager
from utils.version import __version__

# 测速参数：TCP 端口 / 单次超时（秒）/ 成功后重复采样次数 / 并发线程上限
TEST_PORT = 443
TEST_TIMEOUT = 2.0
TEST_ATTEMPTS = 2
MAX_WORKERS = 16

# TLS 严格校验：并发数 / 超时（秒）
VERIFY_WORKERS = 8
TLS_VERIFY_TIMEOUT = 3.0

# 连通性测试：TCP 连接超时（秒）—— 比测速更宽容，弱网也能测出结果
TEST_TIMEOUT_CONNECT = 3.0

# 在线 IP 列表来源（GitHub520 稳定地址，无需访问 GitHub 即可获取）及拉取超时
REMOTE_HOSTS_URL = 'https://raw.hellogithub.com/hosts'

# 请求标识（带版本号，方便数据源统计与排查）
USER_AGENT = f'GitHubHostsAccelerator/{__version__}'
FETCH_TIMEOUT = 10

# 失败原因标签（归一化后给界面直接展示；底层异常写 DEBUG 日志）
REASON_TIMEOUT = '超时'
REASON_REFUSED = '拒绝连接'
REASON_UNREACHABLE = '不可达'
REASON_DNS = '解析失败'
REASON_TLS = 'TLS 校验失败'
REASON_RESET = '连接被重置'
REASON_FAILED = '失败'

# 需要做严格 TLS 校验的"核心域名"：这些域名有稳定的证书，校验失败说明
# 该 IP 大概率被劫持 / 被代理接管，不应作为优选结果。
# （github.io / fastly 等共享证书域名不强制校验，避免误杀合法节点。）
TLS_VERIFY_DOMAINS = frozenset({
    'github.com',
    'api.github.com',
    'codeload.github.com',
    'gist.github.com',
    'raw.githubusercontent.com',
    'objects.githubusercontent.com',
    'avatars.githubusercontent.com',
    'user-images.githubusercontent.com',
    'media.githubusercontent.com',
    'private-user-images.githubusercontent.com',
    'camo.githubusercontent.com',
    'github.githubassets.com',
})

logger = logging.getLogger(__name__)


def _address_family(host):
    """根据目标地址选择 socket 地址族。

    入参：host —— IP 字符串（域名或 "" 时按 IPv4 处理）。
    返回值：socket.AF_INET 或 socket.AF_INET6。
    为什么需要？IPv6 字面量必须用 AF_INET6 的 socket 才能 connect；
    用 AF_INET 会直接报 address family not supported。
    """
    return socket.AF_INET6 if isinstance(host, str) and ':' in host else socket.AF_INET


def _classify_error(error):
    """把底层网络异常归一化成界面可直接展示的中文原因。

    入参：error —— 任意异常对象。返回值：str（REASON_* 之一）。
    """
    if isinstance(error, TimeoutError):
        return REASON_TIMEOUT
    if isinstance(error, ConnectionRefusedError):
        return REASON_REFUSED
    if isinstance(error, socket.gaierror):
        return REASON_DNS
    if isinstance(error, ssl.SSLError):
        return REASON_TLS
    if isinstance(error, OSError):
        if getattr(error, 'errno', None) == errno.ETIMEDOUT:
            return REASON_TIMEOUT
        if getattr(error, 'errno', None) in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            return REASON_UNREACHABLE
        # Windows 的 WSA 错误码：10060 超时 / 10061 拒绝 / 10051、10065 不可达
        winerror = getattr(error, 'winerror', None)
        if winerror == 10060:
            return REASON_TIMEOUT
        if winerror == 10061:
            return REASON_REFUSED
        if winerror in (10053, 10054):
            return REASON_RESET
        if winerror in (10051, 10065):
            return REASON_UNREACHABLE
    return REASON_FAILED


def _tcp_connect(host, port, timeout):
    """对 host:port 做一次 TCP 连接测试。

    入参：host —— IP 或域名（域名会走系统解析，含 hosts 映射）；
          port —— 目标端口；timeout —— 超时秒数。
    返回值：(ms, reason) —— 成功 (延迟毫秒整数, None)；失败 (None, 失败原因)。

    实现细节：time.perf_counter 是单调高精度时钟，适合测耗时；
    with 语句确保 socket 无论成败都关闭，不泄漏句柄。
    """
    with socket.socket(_address_family(host), socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        start = time.perf_counter()
        try:
            sock.connect((host, port))
            return round((time.perf_counter() - start) * 1000.0), None
        except Exception as error:  # noqa: BLE001 - 网络异常种类多，统一归一化
            logger.debug('TCP 连接失败 %s:%s（%.1fs）：%s', host, port, timeout, error)
            return None, _classify_error(error)


def _probe_ip(ip, timeout, attempts):
    """对单个候选 IP 测速：成功后重复采样取最小值，失败直接返回原因。

    入参：ip —— IP 字符串；timeout —— 单次超时；attempts —— 采样次数（>=1）。
    返回值：(ms, reason) —— 同 _tcp_connect。
    为什么只在成功后重复？失败路径每次都要等满超时，弱网下会成倍拖长
    整个启用流程；而 RTT 抖动只影响"成功但偏慢"的判定，所以重复采样只针对成功。
    """
    ms, reason = _tcp_connect(ip, TEST_PORT, timeout)
    if ms is None:
        return None, reason
    best = ms
    for _ in range(max(0, attempts - 1)):
        extra_ms, extra_reason = _tcp_connect(ip, TEST_PORT, timeout)
        if extra_ms is None:
            logger.debug('重复采样失败 %s：%s', ip, extra_reason)
            continue
        best = min(best, extra_ms)
    return best, None


def test_ips_concurrent(ips, timeout=None, attempts=TEST_ATTEMPTS,
                        progress_cb=None, cancel_event=None):
    """并发测速一组唯一 IP（TCP 443，超时/采样次数可配置）。

    入参：ips —— IP 可迭代对象（内部自动去重排序）；
          timeout —— 单次连接超时（秒）；None 用默认 TEST_TIMEOUT；
          attempts —— 成功后最多采样次数（默认 2，取最小值降噪）；
          progress_cb —— 可选回调 fn(done, total)，每完成一个 IP 调用一次；
          cancel_event —— 可选 threading.Event，置位后尽快停止并返回已得结果。
    返回值：dict —— {ip: (ms 或 None, reason 或 None)}，包含每个已测 IP 的结果。

    为什么要并发？几十个 IP 串行测，每个最多等 2 秒，全超时要一分多钟；
    并发上限 16 线程，总耗时约等于"最慢的一个"。
    为什么限制 16？线程太多会挤占本机带宽与连接表，测出的延迟也失真。
    """
    unique_ips = sorted(set(ips))
    results = {}
    if not unique_ips:
        return results

    timeout = TEST_TIMEOUT if timeout is None else max(0.2, float(timeout))
    workers = min(MAX_WORKERS, len(unique_ips))
    started = time.perf_counter()
    logger.info('开始对 %d 个 IP 测速（TCP %d，超时 %.1f 秒，每 IP 最多 %d 次采样，并发 %d）',
                len(unique_ips), TEST_PORT, timeout, max(1, attempts), workers)

    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(_probe_ip, ip, timeout, attempts): ip
               for ip in unique_ips}
    cancelled = False
    try:
        for future in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            ip = futures[future]
            try:
                outcome = future.result()
            except Exception:
                logger.exception('测速任务异常：%s', ip)
                outcome = (None, REASON_FAILED)
            results[ip] = outcome
            ms, reason = outcome
            logger.debug('测速%s %s：%s', '成功' if ms is not None else '失败',
                         ip, ms if ms is not None else reason)
            if progress_cb is not None:
                try:
                    progress_cb(len(results), len(unique_ips))
                except Exception:
                    logger.exception('测速进度回调异常（已忽略）')
    finally:
        for future in futures:
            future.cancel()
        # 取消时不等剩下的超时任务，让 UI 立刻回到可用状态
        executor.shutdown(wait=not cancelled, cancel_futures=True)

    ok_count = sum(1 for ms, _reason in results.values() if ms is not None)
    logger.info('测速%s：%d/%d 个 IP 可用，耗时 %.1f 秒',
                '已取消' if cancelled else '完成',
                ok_count, len(unique_ips), time.perf_counter() - started)
    return results


def pick_best_from_results(pairs, results):
    """按域名从测速结果中优选每个域名延迟最低的可用 IP。

    入参：pairs —— [(IP, 域名), ...] 候选清单（同域名可对应多个 IP）；
          results —— test_ips_concurrent 的返回值 {ip: (ms, reason)}。
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


def verify_tls(domain, ip, timeout=TLS_VERIFY_TIMEOUT, attempts=1):
    """对 (域名, IP) 做严格 TLS 握手：校验证书链与主机名。

    入参：domain —— 用于 SNI 与证书主机名校验的域名；ip —— 目标 IP；
          timeout —— 单次握手超时（秒）；attempts —— 失败重试次数
          （网络抖动 / 限流会让单次握手偶发超时，重试可避免误杀）。
    返回值：(ok, reason) —— ok=True 表示证书有效且域名匹配；失败给出原因。
    """
    last_reason = REASON_TLS
    for attempt in range(max(1, int(attempts))):
        try:
            context = ssl.create_default_context()
            with socket.create_connection((ip, TEST_PORT), timeout=timeout) as raw:
                with context.wrap_socket(raw, server_hostname=domain):
                    return True, None
        except Exception as error:  # noqa: BLE001 - 统一归一化失败原因
            logger.debug('TLS 校验失败 %s -> %s（第 %d 次）：%s',
                         domain, ip, attempt + 1, error)
            last_reason = _classify_error(error)
            if attempt + 1 < attempts:
                time.sleep(0.3)
    return False, last_reason


def verify_tls_candidates(targets, timeout=TLS_VERIFY_TIMEOUT,
                           progress_cb=None, cancel_event=None, attempts=2):
    """并发校验同一域名下多个候选 IP 的 TLS，用于"首选 IP 失败时换备选"。

    入参：targets —— [(域名, IP), ...]（同一域名可有多行）；
          timeout —— 单次握手超时；progress_cb —— fn(done, total)；
          cancel_event —— 可选取消信号；attempts —— 每个 IP 的尝试次数。
    返回值：dict —— {(域名, IP): (ok, reason)}。
    """
    targets = list(targets)
    results = {}
    if not targets:
        return results

    workers = min(VERIFY_WORKERS, len(targets))
    logger.info('开始 TLS 严格校验：%d 个候选', len(targets))
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(verify_tls, domain, ip, timeout, attempts):
               (domain, ip) for domain, ip in targets}
    cancelled = False
    try:
        for future in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                logger.exception('TLS 校验任务异常：%s', (key,))
                results[key] = (False, REASON_FAILED)
            if progress_cb is not None:
                try:
                    progress_cb(len(results), len(targets))
                except Exception:
                    logger.exception('TLS 校验进度回调异常（已忽略）')
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=not cancelled, cancel_futures=True)

    failed = [f'{domain} -> {ip}' for (domain, ip), (ok, _reason) in results.items()
              if not ok]
    if failed:
        logger.warning('TLS 校验未通过的候选：%s', '；'.join(sorted(failed)))
    elif results:
        logger.info('TLS 严格校验通过：%d 个候选证书有效', len(results))
    return results


def verify_tls_batch(targets, timeout=TLS_VERIFY_TIMEOUT,
                     progress_cb=None, cancel_event=None):
    """并发校验一批 (域名, IP) 的 TLS 证书。

    入参：targets —— [(域名, IP), ...]；
          timeout —— 单个握手超时；progress_cb —— fn(done, total)；
          cancel_event —— 可选取消信号。
    返回值：dict —— {域名: (ok, reason)}。
    """
    targets = list(targets)
    results = {}
    if not targets:
        return results

    workers = min(VERIFY_WORKERS, len(targets))
    logger.info('开始 TLS 严格校验：%d 个域名', len(targets))
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {executor.submit(verify_tls, domain, ip, timeout): domain
               for domain, ip in targets}
    cancelled = False
    try:
        for future in as_completed(futures):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            domain = futures[future]
            try:
                results[domain] = future.result()
            except Exception:
                logger.exception('TLS 校验任务异常：%s', domain)
                results[domain] = (False, REASON_FAILED)
            if progress_cb is not None:
                try:
                    progress_cb(len(results), len(targets))
                except Exception:
                    logger.exception('TLS 校验进度回调异常（已忽略）')
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=not cancelled, cancel_futures=True)

    failed = [domain for domain, (ok, _reason) in results.items() if not ok]
    if failed:
        logger.warning('TLS 校验未通过：%s', ', '.join(sorted(failed)))
    elif results:
        logger.info('TLS 严格校验通过：%d 个域名证书有效', len(results))
    return results


def probe_https(domain, ip, path='/', max_bytes=131072, timeout=6):
    """对指定 IP 做 TLS + HTTP GET 小探测，验证真实 HTTPS 是否可用。

    入参：domain —— SNI/证书域名；ip —— 目标 IP；path —— 请求路径；
          max_bytes —— 最多读取字节数（防止真的下载整页）；timeout —— 超时秒数。
    返回值：dict —— {'ok','status','ttfb_ms','elapsed_ms','bytes','reason'}。
    用途：hosts 写入后判断"域名是否真的能用"，区分 DNS 层成功与 HTTPS 层受限。
    """
    started = time.perf_counter()
    try:
        context = ssl.create_default_context()
        with socket.create_connection((ip, TEST_PORT), timeout=timeout) as raw:
            raw.settimeout(timeout)
            with context.wrap_socket(raw, server_hostname=domain) as tls:
                tls.settimeout(timeout)
                request = (f'GET {path} HTTP/1.1\r\n'
                           f'Host: {domain}\r\n'
                           f'User-Agent: {USER_AGENT}\r\n'
                           f'Range: bytes=0-{max(0, max_bytes - 1)}\r\n'
                           'Accept-Encoding: identity\r\n'
                           'Connection: close\r\n\r\n')
                tls.sendall(request.encode('ascii', errors='ignore'))
                data = b''
                ttfb = None
                while len(data) < max_bytes:
                    chunk = tls.recv(min(8192, max_bytes - len(data)))
                    if not chunk:
                        break
                    if ttfb is None:
                        ttfb = round((time.perf_counter() - started) * 1000)
                    data += chunk
        elapsed = round((time.perf_counter() - started) * 1000)
        status = None
        if data.startswith(b'HTTP/'):
            try:
                status = int(data.split(b' ', 2)[1])
            except (IndexError, ValueError):
                status = None
        header_end = data.find(b'\r\n\r\n')
        body = data[header_end + 4:] if header_end >= 0 else b''
        ok = status in (200, 206, 301, 302, 304, 404) and len(body) > 0
        return {'ok': ok, 'status': status, 'ttfb_ms': ttfb,
                'elapsed_ms': elapsed, 'bytes': len(body),
                'reason': None if ok else 'HTTPS 传输受限或连接中断'}
    except Exception as error:
        return {'ok': False, 'status': None, 'ttfb_ms': None,
                'elapsed_ms': round((time.perf_counter() - started) * 1000),
                'bytes': 0, 'reason': _classify_error(error)}


def fetch_remote_hosts():
    """从 GitHub520 稳定地址拉取最新 hosts 数据。

    入参：无。
    返回值：(ok, data) —— 成功 (True, 响应文本)；失败 (False, 错误描述)。

    为什么不直接访问 GitHub 拿数据？—— 正是因为 GitHub 访问有问题才用本工具，
    所以数据源放在第三方稳定地址 raw.hellogithub.com。
    为什么失败返回元组而不是抛异常？网络失败是"正常业务分支"而非程序错误，
    用返回值表达可预期失败，用异常表达意外错误 —— 调用方不会漏处理。

    v5.1.4：先按系统代理设置请求；如果代理不可用（例如本机代理软件已退出，
    出现 WinError 10061 连接被拒绝），自动改用直连重试一次。
    hosts 加速本身走的是直连，所以代理挂掉时直连往往反而可用。
    """
    request = urllib.request.Request(
        REMOTE_HOSTS_URL, headers={'User-Agent': USER_AGENT})
    logger.info('开始在线拉取 IP 列表：%s', REMOTE_HOSTS_URL)

    # 1) 默认方式：urllib 会使用 Windows 系统代理设置
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return True, response.read().decode('utf-8', errors='replace')
    except Exception as error:
        logger.warning('在线拉取失败（系统代理方式）：%s', error)

    # 2) 兜底：绕过系统代理直连再试一次
    try:
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with direct.open(request, timeout=FETCH_TIMEOUT) as response:
            logger.info('系统代理不可用，已改用直连拉取成功')
            return True, response.read().decode('utf-8', errors='replace')
    except Exception as error:
        logger.warning('在线拉取失败（直连重试）：%s', error)
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
        'summary_line': '✅ 整体连通正常' / '❌ 部分节点超时' / '❌ 整体不可达',
      }

    测试路径设计：
      - 先查 hosts 中这两个域名当前生效的 IP（没配则回退系统 DNS），
        对"生效 IP"直连 —— 反映的是加速映射本身通不通；
      - 再对 github.com 域名做端到端连接 —— 解析会走系统 hosts，
        测的是用户实际访问走的完整路径。
    """
    details = {'time': time.strftime('%Y-%m-%d %H:%M:%S')}
    results = []
    hosts_missing = []
    for domain, label, key in (('github.com', 'github.com', 'github'),
                               ('raw.githubusercontent.com', 'raw', 'raw')):
        ip = hosts_manager.find_hosts_ip(domain)
        source = 'hosts'
        if ip is None:  # hosts 中未配置时回退系统 DNS 解析
            hosts_missing.append(domain)
            try:
                ip = socket.gethostbyname(domain)
                source = 'DNS'
            except Exception as err:
                logger.warning('连通性测试：%s 无法解析 IP：%s', domain, err)
                results.append(f'{label}: 解析失败 ✗')
                details[key] = '解析失败 ✗'
                continue
        ms, reason = _tcp_connect(ip, TEST_PORT, TEST_TIMEOUT_CONNECT)
        if ms is not None:
            value = f'{ms}ms ✓'
            logger.info('连通性测试 %s（IP=%s，来源=%s）：%d ms', domain, ip, source, ms)
        else:
            value = f'{reason} ✗'
            logger.warning('连通性测试 %s（IP=%s，来源=%s）失败：%s',
                           domain, ip, source, reason)
        results.append(f'{label}: {value}')
        details[key] = value

    # 端到端整体连通性（对域名连接，解析走系统 hosts，即真实生效路径）
    ms, reason = _tcp_connect('github.com', TEST_PORT, TEST_TIMEOUT_CONNECT)
    if ms is not None:
        details['overall'] = '可达'
        logger.info('连通性测试 github.com 端到端连接成功：%d ms', ms)
    else:
        details['overall'] = '不可达'
        logger.warning('连通性测试 github.com 端到端连接失败：%s', reason)
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
    if hosts_missing:
        # 说明加速段里根本没写这些域名（通常是本轮启用时无可用 IP），
        # 与"写了但 IP 不通"是两种不同的故障，界面要能区分。
        details['hosts_missing'] = hosts_missing
        details['summary'] += ' | hosts 未写入: ' + ', '.join(hosts_missing)
        details['summary_line'] += f"（hosts 未写入：{'、'.join(hosts_missing)}）"
        logger.warning('hosts 加速段中未找到这些域名：%s', '、'.join(hosts_missing))
    logger.info('连通性测试结果：%s', details['summary'])
    return details
