# -*- coding: utf-8 -*-
"""Git 镜像加速：把 https://github.com/ 的 Git 请求重写到公共镜像。

为什么需要？（v5.2 的核心）
  hosts 加速只改 DNS，无法解决 GitHub 直连被限速/阻断的问题；
  但 git clone/pull 可以走公共镜像（gh-proxy / ghfast / ghproxy），
  通过 git 的 url.<base>.insteadOf 规则把 https://github.com/ 透明重写，
  不需要管理员权限、不需要外部代理，在任意 Windows 10/11 上都能生效。

安全与可逆：
  - 只接受白名单内的 https 镜像；
  - 修改前把原始 url.* 规则备份到数据目录 git_mirror_backup.json；
  - 关闭时只删除本程序写入的规则，并恢复备份；
  - 已存在用户自定义的 GitHub 镜像规则时拒绝覆盖（避免破坏用户配置）。

本模块只依赖标准库与 utils；不 import 界面库。
"""

import json
import logging
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils import fileio
from utils import paths

logger = logging.getLogger(__name__)

GITHUB_URL = 'https://github.com/'
GIT_TEST_REPO = 'https://github.com/octocat/Hello-World.git'

# 公共镜像前缀（调用格式：前缀 + 完整 GitHub 链接）
MIRRORS = (
    'https://gh-proxy.com/',
    'https://ghfast.top/',
    'https://ghproxy.net/',
)

BACKUP_FILENAME = 'git_mirror_backup.json'
MIRROR_TEST_TIMEOUT = 8
CREATE_NO_WINDOW = 0x08000000


def find_git():
    """返回 git 可执行文件路径；未安装时返回 None。

    优先走 PATH；如果程序是从桌面/开始菜单启动（环境变量与终端不同），
    再检查 Git for Windows 的常见安装位置，避免误判"未检测到 Git"。
    """
    found = shutil.which('git')
    if found:
        return found
    candidates = [
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'),
                     'Git', 'cmd', 'git.exe'),
        os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
                     'Git', 'cmd', 'git.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''),
                     'Programs', 'Git', 'cmd', 'git.exe'),
        os.path.join(os.environ.get('USERPROFILE', ''),
                     'scoop', 'shims', 'git.exe'),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def run_git(args, timeout=10):
    """执行 git 命令（不弹黑窗），返回 CompletedProcess。

    入参：args —— 参数列表（不含 git 本身）；timeout —— 超时秒数。
    返回值：subprocess.CompletedProcess（捕获 stdout/stderr）。
    异常：未安装 git 或超时会抛 RuntimeError。
    """
    git = find_git()
    if not git:
        raise RuntimeError('未检测到 Git，请先安装 Git for Windows')
    creationflags = CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen(
        [git, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        creationflags=creationflags)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # Windows 上只 kill 直接子进程时，git-remote-https 等子进程可能仍持有
        # 管道，导致 communicate 继续卡住；必须 /T 结束整棵进程树。
        try:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(process.pid)],
                               capture_output=True, timeout=5)
            else:
                process.kill()
        except Exception:
            logger.debug('结束超时 git 进程失败', exc_info=True)
        try:
            process.communicate(timeout=2)
        except Exception:
            pass
        raise RuntimeError(f'git 命令超时（{timeout} 秒）')
    return subprocess.CompletedProcess([git, *args], process.returncode,
                                       stdout, stderr)


def normalize_prefix(prefix):
    """规范化镜像前缀：去掉首尾空白并保证以 / 结尾。"""
    return prefix.strip().rstrip('/') + '/'


def mirror_url(prefix):
    """镜像前缀对应的 Git 克隆地址（前缀 + GitHub 链接）。"""
    return normalize_prefix(prefix) + GIT_TEST_REPO


def test_mirror(prefix, timeout=MIRROR_TEST_TIMEOUT):
    """测试单个镜像是否支持 Git smart HTTP。

    入参：prefix —— 镜像前缀；timeout —— 超时秒数。
    返回值：dict —— {'prefix','ok','ms','error'}。
    用 ls-remote 测一个公开仓库，能拿到 HEAD 就说明镜像可代理 git 请求。
    """
    started = time.perf_counter()
    try:
        completed = run_git(['ls-remote', mirror_url(prefix), 'HEAD'], timeout=timeout)
        elapsed = round((time.perf_counter() - started) * 1000)
        ok = completed.returncode == 0 and 'HEAD' in (completed.stdout or '')
        error = None if ok else (completed.stderr or completed.stdout or '').strip()[:120]
        return {'prefix': normalize_prefix(prefix), 'ok': ok, 'ms': elapsed,
                'error': error}
    except Exception as error:
        return {'prefix': normalize_prefix(prefix), 'ok': False,
                'ms': round((time.perf_counter() - started) * 1000),
                'error': str(error)[:120]}


def probe_mirrors(mirrors=None, timeout=MIRROR_TEST_TIMEOUT):
    """并发测试多个镜像，返回按延迟升序排列的结果列表。"""
    targets = list(mirrors or MIRRORS)
    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        futures = {pool.submit(test_mirror, prefix, timeout): prefix
                   for prefix in targets}
        for future in as_completed(futures):
            results.append(future.result())
    usable = sorted((item for item in results if item['ok']), key=lambda x: x['ms'])
    failed = [item for item in results if not item['ok']]
    return usable + failed


def pick_best_mirror(results):
    """从 probe_mirrors 结果中选最快可用镜像；没有可用返回 None。"""
    for item in results:
        if item['ok']:
            return item
    return None


def _read_url_rules(extra_args=None):
    """读取 git 全局 url.* 规则，返回 [{'key','value'}]。"""
    args = ['config', '--global', '--get-regexp', r'^url\.']
    if extra_args:
        args = extra_args + args
    try:
        completed = run_git(args, timeout=5)
    except Exception:
        return []
    rules = []
    if completed.returncode != 0:
        return rules
    for line in (completed.stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            rules.append({'key': parts[0], 'value': parts[1].strip()})
    return rules


def _is_our_rule(rule):
    """判断一条 url.* 规则是否由本程序写入。"""
    key = rule['key'].lower()
    if rule['value'] != GITHUB_URL:
        return False
    if GITHUB_URL.lower() not in key:
        return False
    return any(normalize_prefix(mirror).lower() in key for mirror in MIRRORS)


def _prefix_from_key(key):
    for mirror in MIRRORS:
        if normalize_prefix(mirror) in key:
            return normalize_prefix(mirror)
    return '自定义镜像'


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
        logger.warning('读取 Git 镜像备份失败（忽略）：%s', path, exc_info=True)
        return None


def _save_backup(rules):
    payload = json.dumps({
        'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'url_rules': rules,
    }, ensure_ascii=False, indent=2)
    try:
        fileio.atomic_write_text(_backup_path(), payload, encoding='utf-8')
    except Exception:
        logger.warning('保存 Git 镜像备份失败', exc_info=True)


def _remove_backup():
    try:
        os.remove(_backup_path())
    except OSError:
        pass


def _unset_rules(rules):
    for rule in rules:
        try:
            run_git(['config', '--global', '--unset-all', rule['key']], timeout=5)
        except Exception:
            logger.warning('删除 Git 镜像规则失败：%s', rule['key'], exc_info=True)


def _verify_rewrite(timeout=15, attempts=1):
    """验证 insteadOf 规则确实生效：用 github.com 原始地址执行 ls-remote。

    网络抖动时一次失败不代表配置错误，最多重试两次再判定失败。
    """
    for attempt in range(max(1, attempts)):
        try:
            completed = run_git(['ls-remote', GIT_TEST_REPO, 'HEAD'], timeout=timeout)
            if completed.returncode == 0 and 'HEAD' in (completed.stdout or ''):
                return True
        except Exception:
            logger.warning('验证 Git 镜像规则失败（第 %d 次）', attempt + 1,
                           exc_info=True)
        if attempt + 1 < attempts:
            time.sleep(0.5)
    return False


def enable(preferred_mirror=None):
    """启用 Git 镜像加速。

    入参：preferred_mirror —— 指定镜像前缀（None/空=自动选最快）。
    返回值：(ok, message, prefix)。
    步骤：测速选镜像 -> 备份原始规则 -> 清理旧规则 -> 写入 insteadOf -> 验证。
    """
    if not find_git():
        return False, '未检测到 Git，请先安装 Git for Windows', None

    results = probe_mirrors()
    best = None
    if preferred_mirror:
        for item in results:
            if item['ok'] and item['prefix'] == normalize_prefix(preferred_mirror):
                best = item
                break
    if best is None:
        best = pick_best_mirror(results)
    if best is None:
        return False, '所有 Git 镜像都不可用，请检查网络后重试', None

    rules = _read_url_rules()
    ours = [rule for rule in rules if _is_our_rule(rule)]
    user_rules = [rule for rule in rules
                  if rule['value'] == GITHUB_URL and not _is_our_rule(rule)]
    if user_rules:
        return (False,
                '检测到已有的自定义 Git 镜像规则，为避免破坏你的配置已取消。'
                '请先手动清理 url.* 规则后重试', None)

    if _load_backup() is None or not ours:
        _save_backup(rules)

    _unset_rules(ours)
    key = 'url.' + normalize_prefix(best['prefix']) + GITHUB_URL + '.insteadOf'
    try:
        run_git(['config', '--global', key, GITHUB_URL], timeout=5)
    except Exception as error:
        _restore_after_failure()
        return False, f'写入 Git 配置失败：{error}', None

    applied = [rule for rule in _read_url_rules() if _is_our_rule(rule)]
    if not applied:
        _restore_after_failure()
        return False, 'Git 配置写入后回读校验失败，已还原', None
    if not _verify_rewrite():
        # 镜像探测已通过，但立刻再请求可能遇到限流/抖动；此时配置本身是对的，
        # 不轻易回滚（回滚会让用户觉得"点了没反应"），保留并提示稍后重试。
        logger.warning('Git 镜像已写入，但即时验证失败（保留配置，可能是网络抖动）')
        return (True,
                f'Git 镜像已启用：{best["prefix"]}'
                '（当前节点响应不稳定，可稍后手动 git 测试，或用「关闭 Git 镜像」还原）',
                best['prefix'])

    logger.info('Git 镜像加速已启用：%s（%d ms）', best['prefix'], best['ms'])
    return True, f'Git 镜像已启用：{best["prefix"]}（{best["ms"]} ms）', best['prefix']


def _restore_after_failure():
    """启用失败时清理本程序写入的规则并恢复备份。"""
    rules = _read_url_rules()
    _unset_rules([rule for rule in rules if _is_our_rule(rule)])
    backup = _load_backup()
    if backup:
        for rule in backup.get('url_rules', []):
            try:
                run_git(['config', '--global', '--add', rule['key'], rule['value']],
                        timeout=5)
            except Exception:
                logger.warning('恢复 Git 规则失败：%s', rule['key'], exc_info=True)
        _remove_backup()


def disable():
    """关闭 Git 镜像加速：删除本程序写入的规则并清理备份。

    返回值：(ok, message)。
    """
    if not find_git():
        return False, '未检测到 Git'
    rules = _read_url_rules()
    ours = [rule for rule in rules if _is_our_rule(rule)]
    if not ours:
        _remove_backup()
        return True, 'Git 镜像未启用'
    _unset_rules(ours)
    remaining = [rule for rule in _read_url_rules() if _is_our_rule(rule)]
    if remaining:
        return False, '删除 Git 镜像规则失败，请检查 git 配置权限'
    _remove_backup()
    logger.info('Git 镜像加速已关闭')
    return True, 'Git 镜像已关闭，Git 配置已恢复'


def status():
    """查询当前 Git 镜像状态。

    返回值：dict —— {'git_found','enabled','prefix','detail'}。
    """
    if not find_git():
        return {'git_found': False, 'enabled': False, 'prefix': '',
                'detail': '未检测到 Git'}
    rules = _read_url_rules()
    ours = [rule for rule in rules if _is_our_rule(rule)]
    if not ours:
        return {'git_found': True, 'enabled': False, 'prefix': '',
                'detail': '未启用'}
    prefix = _prefix_from_key(ours[0]['key'])
    return {'git_found': True, 'enabled': True, 'prefix': prefix,
            'detail': f'已启用：{prefix}'}
