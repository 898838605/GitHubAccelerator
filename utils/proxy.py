# -*- coding: utf-8 -*-
r"""系统代理检测（Windows：环境变量 + WinINET 注册表设置）。

为什么需要？
  hosts 加速只改「域名解析」，不做流量转发。如果用户所在网络必须经过代理
  才能上网（公司网络、校园网、本地代理软件的系统代理模式），直连 GitHub 的
  IP 可能完全不通：此时 hosts 加速不适用，强行写入 hosts 也没有效果。
  检测到代理时，程序会在连通性结果与状态栏给出提示，引导用户改用代理或
  「下载加速链接」的镜像方案。

检测顺序（只读，不修改任何系统设置）：
  1. 环境变量 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY（大小写都查）；
  2. 注册表 HKCU\...\Internet Settings 的 ProxyEnable + ProxyServer；
  3. 同位置的 AutoConfigURL（PAC 自动配置脚本）。
"""

import logging
import os

try:  # 仅 Windows 有 winreg；导入失败时视为「无法检测」，不影响程序运行
    import winreg
except ImportError:  # pragma: no cover - 非 Windows 环境
    winreg = None

logger = logging.getLogger(__name__)

# 需要读取的注册表位置（WinINET 系统代理设置）
_INTERNET_SETTINGS_KEY = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'

# 环境变量候选（按优先级排列；Windows 环境变量通常不区分大小写，这里都查一遍）
_ENV_PROXY_KEYS = ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                   'ALL_PROXY', 'all_proxy')


def detect_proxy():
    """检测当前系统代理设置。

    入参：无。
    返回值：dict ——
      {
        'enabled': bool,        # 是否检测到代理
        'server': str | None,   # 代理地址（host:port 或完整 URL）
        'source': str,          # '环境变量' / '系统设置' / '自动配置脚本(PAC)' / ''
        'pac': str | None,      # PAC 脚本地址（如有）
        'bypass': str | None,   # 代理例外列表（如有）
      }
    任何异常都降级为「未检测到」，检测失败绝不能影响程序主流程。
    """
    try:
        env_server = _env_proxy()
        if env_server:
            return {'enabled': True, 'server': env_server, 'source': '环境变量',
                    'pac': None, 'bypass': None}

        info = _read_registry()
        server = _primary_server(info.get('server'))
        if int(info.get('enable') or 0) and server:
            return {'enabled': True, 'server': server, 'source': '系统设置',
                    'pac': info.get('pac'), 'bypass': info.get('bypass')}
        if info.get('pac'):
            return {'enabled': True, 'server': None, 'source': '自动配置脚本(PAC)',
                    'pac': info.get('pac'), 'bypass': info.get('bypass')}
        return {'enabled': False, 'server': None, 'source': '',
                'pac': info.get('pac'), 'bypass': info.get('bypass')}
    except Exception:
        logger.warning('检测系统代理失败（按未启用处理）', exc_info=True)
        return {'enabled': False, 'server': None, 'source': '', 'pac': None,
                'bypass': None}


def describe_proxy(info):
    """把 detect_proxy() 的结果转成界面可展示的中文描述。

    入参：info —— detect_proxy() 返回值。返回值：str。
    """
    if not info or not info.get('enabled'):
        return '未检测到'
    target = info.get('server') or info.get('pac') or '未知地址'
    source = info.get('source') or '未知来源'
    return f'已启用（{target}，来源：{source}）'


def _env_proxy():
    """读取环境变量里的代理地址；没有则返回 None。"""
    for name in _ENV_PROXY_KEYS:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _read_registry():
    """读取 WinINET 注册表代理设置。

    返回值：dict —— {'enable': int, 'server': str|None, 'pac': str|None,
                     'bypass': str|None}；读取失败返回全空值。
    """
    result = {'enable': 0, 'server': None, 'pac': None, 'bypass': None}
    if winreg is None:
        return result
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INTERNET_SETTINGS_KEY) as key:
            result['enable'] = int(_query_value(key, 'ProxyEnable') or 0)
            result['server'] = _query_value(key, 'ProxyServer')
            result['pac'] = _query_value(key, 'AutoConfigURL')
            result['bypass'] = _query_value(key, 'ProxyOverride')
    except OSError:
        logger.debug('读取系统代理注册表失败（按未设置处理）', exc_info=True)
    return result


def _query_value(key, name):
    """安全读取一个注册表值；不存在时返回 None。"""
    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _primary_server(raw):
    """从 ProxyServer 原始值里取出最合适的一个代理地址。

    Windows 的 ProxyServer 可能是 "127.0.0.1:7890"，
    也可能是 "http=127.0.0.1:7890;https=127.0.0.1:7891" 这种按协议分开的写法。
    这里优先取 https，其次 http，最后任意一个。
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    if '=' in raw:
        entries = {}
        for part in raw.split(';'):
            if '=' in part:
                name, value = part.split('=', 1)
                entries[name.strip().lower()] = value.strip()
        return entries.get('https') or entries.get('http') or next(iter(entries.values()), None)
    return raw.split(';')[0].strip() or None
