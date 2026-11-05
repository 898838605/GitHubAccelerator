# -*- coding: utf-8 -*-
"""下载加速链接生成：把 GitHub 文件直链拼上镜像代理前缀。

与 hosts 加速的区别（为什么需要这个功能）：
  hosts 加速解决的是"域名解析到坏 IP"——让网页 / API 恢复可达；
  但 Release 大文件下载慢经常是"源站 / CDN 出口本身就慢"，换 IP 也快
  不起来。镜像代理（gh-proxy 等公益镜像站）通过中转服务器代为下载
  （通常还带缓存），把下载流量引导到对用户更友好的线路上，才能真正
  提速大文件。两种方式互补，互不替代。

core 层规则：纯函数模块——输入前缀与链接、返回结果，不碰界面、
不写全局状态、不开线程；如何展示结果由 ui 层决定。
"""

import logging

logger = logging.getLogger(__name__)

# 常用代理镜像前缀（界面下拉框的候选，按需自行增删）
PROXY_PREFIXES = (
    'https://gh-proxy.com/',
    'https://ghfast.top/',
    'https://ghproxy.net/',
)

# 合法原始链接的前缀：仅接受 github.com 的 http/https 直链
# （release 资产、源码包的原始下载链接都在 github.com 域下）
VALID_URL_PREFIXES = ('https://github.com/', 'http://github.com/')

# 校验失败时的提示消息（ui 层直接展示，避免两侧文案不一致）
MSG_EMPTY = '请先粘贴链接'
MSG_INVALID = '请输入有效的 GitHub 链接'


def is_valid_github_url(url):
    """校验原始链接是否为可加速的 GitHub 直链。

    入参：url —— 已去首尾空白的链接字符串。
    返回值：bool —— True 表示以 http(s)://github.com/ 开头。
    """
    return url.startswith(VALID_URL_PREFIXES)


def normalize_prefix(prefix):
    """规范化代理前缀：确保以且仅以一个 / 结尾（避免拼接出现双斜杠）。

    入参：prefix —— 代理前缀，有无结尾斜杠均可（'https://gh-proxy.com'）。
    返回值：str，形如 'https://gh-proxy.com/'。
    """
    return prefix.rstrip('/') + '/'


def build_accel_url(prefix, url):
    """把代理前缀与原始链接拼接为加速链接。

    入参：prefix —— 代理前缀（有无结尾斜杠均可）；
          url —— 原始链接（允许带首尾空白，内部去除）。
    返回值：(ok, result) ——
          成功  (True,  加速链接字符串)；
          失败  (False, 提示消息：MSG_EMPTY / MSG_INVALID)。

    拼接示例：
      build_accel_url('https://gh-proxy.com',
                      'https://github.com/owner/repo/releases/download/v1/a.zip')
      -> 'https://gh-proxy.com/https://github.com/owner/repo/releases/download/v1/a.zip'
    """
    url = url.strip()
    if not url:
        logger.info('下载加速：输入为空，取消生成')
        return False, MSG_EMPTY
    if not is_valid_github_url(url):
        logger.info('下载加速：链接不是 GitHub 直链，取消生成：%s', url[:100])
        return False, MSG_INVALID
    accel_url = normalize_prefix(prefix) + url
    logger.info('下载加速：已生成加速链接：%s', accel_url)
    return True, accel_url
