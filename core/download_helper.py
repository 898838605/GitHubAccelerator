# -*- coding: utf-8 -*-
"""下载加速链接：镜像前缀 + GitHub 直链的识别、校验与拼接（纯函数模块）。

与 hosts 加速的区别（为什么需要这个功能）：
  hosts 加速解决的是"域名解析到坏 IP"——让网页 / API 恢复可达；
  但 Release 大文件下载慢经常是"源站 / CDN 出口本身就慢"，换 IP 也快
  不起来。镜像代理（gh-proxy 等公益镜像站）通过中转服务器代为下载
  （通常还带缓存），把下载流量引导到对用户更友好的线路上，才能真正
  提速大文件。两种方式互补，互不替代。

为什么这些规则放在 core 而不是 ui：
  镜像节点列表、URL 校验标准、克隆/仓库链接的自动转换规则都是"业务规则"，
  必须只有一处定义。以前 ui/download_dialog.py 里也有一份 MIRROR_NODES 和
  一套只 startswith 的校验，与本模块重复且标准不一致——现在统一收敛到这里，
  UI 只负责展示与调用。

core 层规则：纯函数——输入前缀与链接、返回结果，不碰界面、不写全局状态、
不开线程；如何展示结果由 ui 层决定。
"""

import logging
import urllib.parse

logger = logging.getLogger(__name__)

# 常用镜像代理前缀（界面下拉框的候选，按需自行增删）。
# 调用格式是「前缀 + 完整原始链接」：
#   https://gh-proxy.com/https://github.com/owner/repo/releases/download/v1/a.zip
MIRROR_PREFIXES = (
    'https://gh-proxy.com/',
    'https://ghfast.top/',
    'https://ghproxy.net/',
)
DEFAULT_NODE = MIRROR_PREFIXES[0]

# 判定为"文件直链"的路径标记（release 附件 / 源码包 / raw / blob 文件页）：
# 路径命中任意一个就原样拼接代理前缀。
FILE_PATH_MARKERS = ('/archive/', '/releases/', '/raw/', '/blob/')

# 克隆 / 仓库链接自动转换的目标分支（本地无法确定仓库默认分支，默认 main）
BRANCH_CHOICES = ('main', 'master')
DEFAULT_BRANCH = 'main'

# 校验失败时的提示消息（ui 层直接展示，避免两侧文案不一致）
MSG_EMPTY = '请先粘贴链接'
MSG_INVALID = '请输入有效的 GitHub 链接'


def is_valid_github_url(url):
    """校验链接是否为 github.com 下的 http(s) 链接。

    入参：url —— 待校验的链接字符串（首尾空白会被忽略）。
    返回值：bool —— True 表示 scheme 是 http/https 且主机名恰为 github.com。

    用 urlsplit 而不是 startswith：大小写、端口、userinfo、路径中的
    "@github.com" 等写法都不会被误判，也不会放过 http://github.com.evil.com。
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme not in ('http', 'https'):
        return False
    return parts.netloc.lower() == 'github.com'


def normalize_prefix(prefix):
    """规范化代理前缀：确保以且仅以一个 / 结尾（避免拼接出现双斜杠）。

    入参：prefix —— 代理前缀，有无结尾斜杠均可（'https://gh-proxy.com'）。
    返回值：str，形如 'https://gh-proxy.com/'。
    """
    return prefix.strip().rstrip('/') + '/'


def build_accel_url(prefix, url):
    """把代理前缀与原始链接拼接为加速链接。

    入参：prefix —— 代理前缀（有无结尾斜杠均可）；
          url —— 原始链接（允许带首尾空白，内部去除）。
    返回值：(ok, result) ——
          成功 (True, 加速链接字符串)；
          失败 (False, 提示消息：MSG_EMPTY / MSG_INVALID)。

    拼接示例：
      build_accel_url('https://gh-proxy.com',
                      'https://github.com/owner/repo/releases/download/v1/a.zip')
      -> 'https://gh-proxy.com/https://github.com/owner/repo/releases/download/v1/a.zip'
    """
    url = (url or '').strip()
    if not url:
        logger.info('下载加速：输入为空，取消生成')
        return False, MSG_EMPTY
    if not is_valid_github_url(url):
        logger.info('下载加速：链接不是 GitHub 直链，取消生成：%s', url[:100])
        return False, MSG_INVALID
    accel_url = normalize_prefix(prefix) + url
    logger.info('下载加速：已生成加速链接：%s', accel_url)
    return True, accel_url


def resolve_github_url(url, branch=DEFAULT_BRANCH):
    """识别 GitHub 链接类型；克隆 / 仓库主页自动转为源码包直链。

    入参：url —— 已去空白的合法 GitHub 链接；branch —— 源码包分支（默认 main）。
    返回值：(kind, resolved_url)
      kind         —— 'clone'（.git 克隆）/ 'repo'（仓库主页）/
                      'file'（文件直链）/ 'unknown'（其他页面）
      resolved_url —— 待拼接代理前缀的 GitHub 地址
                      （clone / repo 已转换为 archive 源码包直链）

    识别顺序：.git 克隆链接 -> 文件直链路径标记 -> 仓库主页（两段路径）。
    镜像代理不支持裸仓库地址（直接拼接会得到 Invalid input），所以自动转换。
    """
    if branch not in BRANCH_CHOICES:
        branch = DEFAULT_BRANCH
    segments = [part for part in urllib.parse.urlsplit(url).path.split('/') if part]
    # 1) 克隆链接：https://github.com/{user}/{repo}.git（容忍大小写与尾斜杠）
    if len(segments) == 2 and segments[1].lower().endswith('.git'):
        user, repo = segments[0], segments[1][:-4]
        return 'clone', f'https://github.com/{user}/{repo}/archive/refs/heads/{branch}.zip'
    # 2) 文件直链：release 附件 / 源码包 / raw / blob 文件页，原样拼接
    path = '/' + '/'.join(segments)
    if any(marker in path for marker in FILE_PATH_MARKERS):
        return 'file', url
    # 3) 仓库主页：https://github.com/{user}/{repo}
    if len(segments) == 2:
        user, repo = segments
        return 'repo', f'https://github.com/{user}/{repo}/archive/refs/heads/{branch}.zip'
    return 'unknown', url
