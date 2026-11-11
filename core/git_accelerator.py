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

# 仅拉取走镜像、push 仍走 github.com 的兜底规则（否则 push 会被重写到只读镜像）
PUSH_REWRITE_KEY = 'url.https://github.com/.pushInsteadOf'
MIRROR_TEST_TIMEOUT = 25
MIRROR_VERIFY_TIMEOUT = 30
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
        result = {'prefix': normalize_prefix(prefix), 'ok': ok, 'ms': elapsed,
                  'error': error}
    except Exception as error:
        result = {'prefix': normalize_prefix(prefix), 'ok': False,
                  'ms': round((time.perf_counter() - started) * 1000),
                  'error': str(error)[:120]}
    if result['ok']:
        logger.info('Git 镜像探测：%s 可用（%d ms）', result['prefix'], result['ms'])
    else:
        logger.warning('Git 镜像探测：%s 不可用（%d ms）：%s',
                       result['prefix'], result['ms'], result['error'])
    return result


def probe_mirrors(mirrors=None, timeout=MIRROR_TEST_TIMEOUT):
    """按顺序逐个测试镜像，命中第一个可用镜像就停止。

    为什么不再并发？三个镜像各开一条 Git HTTPS 连接会互相抢带宽，
    镜像单次 ls-remote 本身就要 10~20 秒，并发后很容易全部超时；
    顺序探测虽然总耗时略长，但第一个常用节点成功就能立即返回，反而更快更稳。
    """
    targets = list(mirrors or MIRRORS)
    results = []
    for index, prefix in enumerate(targets, start=1):
        logger.info('Git 镜像探测（%d/%d）：%s', index, len(targets), prefix)
        result = test_mirror(prefix, timeout)
        results.append(result)
        if result['ok']:
            logger.info('Git 镜像探测命中：%s（%d ms）',
                        result['prefix'], result['ms'])
            break
    usable = [item for item in results if item['ok']]
    failed = [item for item in results if not item['ok']]
    if not usable:
        logger.warning('Git 镜像探测完成：已尝试 %d 个镜像，全部不可用',
                       len(results))
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


def _push_rule_exists(rules):
    """是否存在 pushInsteadOf 规则。"""
    return any(rule['key'].lower() == PUSH_REWRITE_KEY.lower() for rule in rules)


def _remove_push_rule():
    """移除本程序写入的 pushInsteadOf 规则。"""
    try:
        run_git(['config', '--global', '--unset-all', PUSH_REWRITE_KEY], timeout=5)
    except Exception:
        logger.debug('移除 pushInsteadOf 规则时未找到或失败（忽略）')


def ensure_safe_push():
    """已有镜像规则时补上 pushInsteadOf，保证 push 仍直连 github.com。

    旧版本只写了 insteadOf，导致 push 也被重写到只读镜像而失败；
    启动时发现旧配置会自动补一条 no-op 的 push 重写规则并记日志。
    返回值：bool —— 本次是否补写了规则。
    """
    rules = _read_url_rules()
    if not any(_is_our_rule(rule) for rule in rules):
        return False
    if _push_rule_exists(rules):
        return False
    try:
        run_git(['config', '--global', PUSH_REWRITE_KEY, GITHUB_URL], timeout=5)
        logger.info('已为已有 Git 镜像配置补充 pushInsteadOf：push 仍走 github.com')
        return True
    except Exception:
        logger.warning('补充 pushInsteadOf 失败（push 可能仍被镜像重写）', exc_info=True)
        return False


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


def _verify_rewrite(timeout=MIRROR_VERIFY_TIMEOUT, attempts=1):
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


def enable(preferred_mirror=None, probe=True):
    """启用 Git 镜像加速。

    入参：preferred_mirror —— 指定镜像前缀（None/空=自动选最快）；
          probe —— True 时先做 Git 协议探测；False 表示调用方已用其他方式
          （如下载节点测速）选定镜像，直接配置并验证。
    返回值：(ok, message, prefix)。
    步骤：测速选镜像 -> 备份原始规则 -> 清理旧规则 -> 写入 insteadOf -> 验证。
    """
    if not find_git():
        return False, '未检测到 Git，请先安装 Git for Windows', None

    logger.info('开始启用 Git 镜像加速（preferred=%s, probe=%s）',
                preferred_mirror or '自动', probe)
    best = None
    probe_verified = False
    if probe:
        results = probe_mirrors()
        if preferred_mirror:
            for item in results:
                if item['ok'] and item['prefix'] == normalize_prefix(preferred_mirror):
                    best = item
                    break
        if best is None:
            best = pick_best_mirror(results)
        if best is None:
            logger.warning('Git 镜像探测全部失败，无法自动选择节点')
            return False, '所有 Git 镜像都不可用，请检查网络后重试', None
        probe_verified = True
    else:
        if not preferred_mirror:
            return False, '未指定 Git 镜像节点', None
        best = {'prefix': normalize_prefix(preferred_mirror), 'ms': 0, 'ok': True}
        logger.info('跳过 Git 协议探测，直接使用指定镜像：%s', best['prefix'])
    logger.info('选定 Git 镜像：%s（探测 %d ms）', best['prefix'], best['ms'])

    rules = _read_url_rules()
    ours = [rule for rule in rules if _is_our_rule(rule)]
    user_rules = [rule for rule in rules
                  if rule['value'] == GITHUB_URL and not _is_our_rule(rule)]
    if user_rules:
        return (False,
                '检测到已有的自定义 Git 镜像规则，为避免破坏你的配置已取消。'
                '请先手动清理 url.* 规则后重试', None)
    if _push_rule_exists(rules):
        return (False,
                '检测到已有的 pushInsteadOf 规则，为避免破坏你的配置已取消。'
                '请先手动清理 url.* 规则后重试', None)

    if _load_backup() is None or not ours:
        _save_backup(rules)

    _unset_rules(ours)
    key = 'url.' + normalize_prefix(best['prefix']) + GITHUB_URL + '.insteadOf'
    try:
        run_git(['config', '--global', key, GITHUB_URL], timeout=5)
        run_git(['config', '--global', PUSH_REWRITE_KEY, GITHUB_URL], timeout=5)
        logger.info('Git 镜像规则已写入：%s（fetch 走镜像，push 保持 github.com）', key)
    except Exception as error:
        _restore_after_failure()
        return False, f'写入 Git 配置失败：{error}', None

    applied = [rule for rule in _read_url_rules() if _is_our_rule(rule)]
    if not applied:
        _restore_after_failure()
        return False, 'Git 配置写入后回读校验失败，已还原', None
    if probe_verified:
        # 探测阶段已经用 git ls-remote 走了一遍镜像的 Git 协议；配置只是把
        # github.com 重写到同一路径，再做一次 30 秒级验证只会让用户白等。
        logger.info('Git 协议探测已通过，跳过重复验证：%s', best['prefix'])
        logger.info('Git 镜像加速已启用：%s（探测 %d ms）', best['prefix'], best['ms'])
        return True, f'Git 镜像已启用：{best["prefix"]}（探测 {best["ms"]} ms）', best['prefix']

    logger.info('Git 镜像规则已写入，开始验证实际请求：%s', best['prefix'])
    if not _verify_rewrite(timeout=MIRROR_VERIFY_TIMEOUT):
        # 这里通常是从下载节点回退配置的情况；验证失败也保留配置，
        # 避免"点了没反应"，由用户决定是否关闭。
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
    _remove_push_rule()
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
    _remove_push_rule()
    remaining = [rule for rule in _read_url_rules() if _is_our_rule(rule)]
    if remaining:
        return False, '删除 Git 镜像规则失败，请检查 git 配置权限'
    _remove_backup()
    logger.info('Git 镜像加速已关闭，已删除本程序写入的 url.* 规则')
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
