# -*- coding: utf-8 -*-
"""系统代理检测与 Git 代理配置（一键可逆）。

定位：
  - 网页浏览需要代理（hosts 和镜像都解决不了），本模块负责检测系统代理、
    验证代理是否可用，并把代理一键配置到 Git（git config http.proxy）以便
    clone/pull 也能走代理；
  - 不默认修改 Windows 系统代理（影响整个系统），只提供检测与 Git 配置；
  - 所有 Git 配置修改都有备份文件，可一键还原。

只依赖标准库、utils/proxy 与 core/git_accelerator 的 run_git。
"""

import json
import logging
import os
import socket

from core import git_accelerator
from utils import fileio
from utils import paths
from utils import proxy

logger = logging.getLogger(__name__)

BACKUP_FILENAME = 'git_proxy_backup.json'
CONNECT_TIMEOUT = 3.0


def _backup_path():
    return os.path.join(paths.get_data_dir(), BACKUP_FILENAME)


def _load_backup():
    path = _backup_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as file:
            return json.load(file)
    except Exception:
        logger.warning('读取 Git 代理备份失败（忽略）', exc_info=True)
        return None


def _save_backup(data):
    try:
        fileio.atomic_write_text(
            _backup_path(), json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8')
    except Exception:
        logger.warning('保存 Git 代理备份失败', exc_info=True)


def _remove_backup():
    try:
        os.remove(_backup_path())
    except OSError:
        pass


def _parse_server(server):
    """把代理地址解析成 (scheme, host, port)。"""
    if not server:
        return None
    raw = str(server).strip()
    scheme = 'http'
    if '://' in raw:
        scheme, raw = raw.split('://', 1)
        scheme = scheme.lower()
    raw = raw.split('/', 1)[0]
    if raw.startswith('['):  # IPv6 字面量
        host, _, rest = raw[1:].partition(']')
        port = int(rest.lstrip(':') or 80)
        return scheme, host, port
    if ':' in raw:
        host, port_text = raw.rsplit(':', 1)
        try:
            port = int(port_text)
        except ValueError:
            port = 80
        return scheme, host, port
    return scheme, raw, (443 if scheme == 'https' else 80)


def verify_proxy(server, timeout=CONNECT_TIMEOUT):
    """验证代理是否真的可用。

    入参：server —— 代理地址（可以是 http://host:port 或 host:port）。
    返回值：dict —— {'reachable','verified','detail'}。
    先做 TCP 连通性检查，再对 HTTP 代理发 CONNECT，确认能连上 GitHub。
    """
    parsed = _parse_server(server)
    if not parsed:
        return {'reachable': False, 'verified': False, 'detail': '无法解析代理地址'}
    scheme, host, port = parsed
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if scheme not in ('http', 'https'):
                return {'reachable': True, 'verified': False,
                        'detail': f'{scheme} 代理端口可达（未做 CONNECT 探测）'}
            request = ('CONNECT github.com:443 HTTP/1.1\r\n'
                       'Host: github.com:443\r\n'
                       'Proxy-Connection: keep-alive\r\n\r\n')
            sock.sendall(request.encode('ascii', errors='ignore'))
            response = sock.recv(128).decode('latin-1', errors='replace')
            ok = ' 200 ' in response.split('\r\n', 1)[0]
            return {'reachable': True, 'verified': ok,
                    'detail': '代理可用（CONNECT 成功）' if ok else '代理端口可达，但 CONNECT 失败'}
    except Exception as error:
        return {'reachable': False, 'verified': False,
                'detail': f'代理不可用：{type(error).__name__}'}


def detect_and_verify():
    """检测系统代理并验证可用性，返回给界面的状态 dict。"""
    info = proxy.detect_proxy()
    status = {
        'detected': bool(info.get('enabled')),
        'server': info.get('server') or info.get('pac'),
        'verified': False,
        'detail': proxy.describe_proxy(info),
        'info': info,
    }
    if info.get('enabled') and info.get('server'):
        verification = verify_proxy(info['server'])
        status['verified'] = verification['verified']
        status['detail'] = f"{status['detail']}；{verification['detail']}"
    return status


def get_git_proxy():
    """读取当前 Git 全局代理配置。"""
    values = {}
    for key in ('http.proxy', 'https.proxy'):
        try:
            completed = git_accelerator.run_git(['config', '--global', '--get', key],
                                                timeout=5)
            values[key] = (completed.stdout or '').strip() or None
        except Exception:
            values[key] = None
    return values


def set_git_proxy(server):
    """把代理写入 Git 全局配置（http.proxy / https.proxy），并备份原值。

    入参：server —— 代理地址。
    返回值：(ok, message)。
    """
    if not git_accelerator.find_git():
        return False, '未检测到 Git，请先安装 Git for Windows'
    if not server:
        return False, '未检测到可用的代理地址'
    verification = verify_proxy(server)
    if not verification['reachable']:
        return False, f'代理不可用：{verification["detail"]}'

    if _load_backup() is None:
        _save_backup({'created_at': __import__('time').strftime('%Y-%m-%d %H:%M:%S'),
                      **{key: value for key, value in get_git_proxy().items()}})
    try:
        git_accelerator.run_git(['config', '--global', 'http.proxy', server], timeout=5)
        git_accelerator.run_git(['config', '--global', 'https.proxy', server], timeout=5)
    except Exception as error:
        return False, f'写入 Git 代理失败：{error}'
    applied = get_git_proxy()
    if applied.get('http.proxy') != server:
        return False, 'Git 代理写入后回读校验失败'
    logger.info('Git 代理已设置：%s', server)
    return True, f'Git 代理已设置：{server}'


def clear_git_proxy():
    """清除 Git 代理配置，并按备份恢复原值。

    返回值：(ok, message)。
    """
    if not git_accelerator.find_git():
        return False, '未检测到 Git'
    backup = _load_backup()
    try:
        for key in ('http.proxy', 'https.proxy'):
            git_accelerator.run_git(['config', '--global', '--unset-all', key],
                                    timeout=5)
    except Exception:
        logger.debug('清除 Git 代理配置时出现异常（继续）', exc_info=True)
    if backup:
        for key in ('http.proxy', 'https.proxy'):
            value = backup.get(key)
            if value:
                try:
                    git_accelerator.run_git(
                        ['config', '--global', key, str(value)], timeout=5)
                except Exception:
                    logger.warning('恢复 Git 代理失败：%s', key, exc_info=True)
        _remove_backup()
    applied = get_git_proxy()
    expected = {'http.proxy': (backup or {}).get('http.proxy') or None,
                'https.proxy': (backup or {}).get('https.proxy') or None}
    if applied != expected:
        return False, 'Git 代理清除失败，请检查 git 配置权限'
    logger.info('Git 代理已清除')
    return True, 'Git 代理已清除，配置已恢复'
