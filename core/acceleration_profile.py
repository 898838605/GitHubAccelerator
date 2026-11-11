# -*- coding: utf-8 -*-
"""可配置的加速 profile：按域名声明多上游，供 hosts 引擎做兜底与选路。

设计来源（借鉴 SteamTools/FastGithub，但不引入代理/MITM）：
  - SteamTools 用 AccelerateProjectDTO 描述 match/listen/upstream/order；
  - 这里只保留 hosts 模式需要的部分：域名 -> 多个直连 IP；
  - 不增加根证书、系统代理、驱动或第三方反代节点。

加载策略：
  - profiles.json 不存在 -> 使用内置 DEFAULT_PROFILES；
  - 文件损坏/全部非法 -> 记录 WARNING，回退 DEFAULT_PROFILES；
  - 文件合法且非空 -> DEFAULT_PROFILES + 自定义 profiles（合并去重）；
  - 文件合法且为空 -> 只使用空配置（用户显式关闭兜底）。

本模块只做配置解析/合并，不直接写 hosts。
"""

import json
import logging
import os
from dataclasses import dataclass

from core import hosts_manager
from utils import paths

logger = logging.getLogger(__name__)

PROFILE_FILENAME = 'profiles.json'
SUPPORTED_MODES = frozenset({'hosts'})
SUPPORTED_UPSTREAM_TYPES = frozenset({'ip'})


class ProfileValidationError(ValueError):
    """单条 profile 配置非法。"""


@dataclass(frozen=True)
class Upstream:
    """一个上游候选。Phase 1 只支持 host 模式下的直连 IP。"""

    type: str
    value: str
    port: int = 443
    sni: str = ''

    def to_dict(self):
        return {
            'type': self.type,
            'value': self.value,
            'port': self.port,
            'sni': self.sni,
        }


@dataclass(frozen=True)
class AccelerationProfile:
    """一组域名的上游配置。"""

    id: str
    name: str
    mode: str
    match_domains: tuple
    listen_domains: tuple
    upstreams: tuple
    enabled: bool = True
    order: int = 0
    notes: str = ''

    def summary(self):
        return {
            'id': self.id,
            'name': self.name,
            'mode': self.mode,
            'enabled': bool(self.enabled),
            'order': self.order,
            'match_domains': list(self.match_domains),
            'listen_domains': list(self.listen_domains),
            'upstream_count': len(self.upstreams),
            'notes': self.notes,
        }


def _default_profiles():
    """把现有内置兜底 IP 迁移成默认 profiles，保持行为等价。"""
    result = []
    for order, (domain, ips) in enumerate(
            hosts_manager.BUILTIN_FALLBACK_CANDIDATES.items()):
        result.append(AccelerationProfile(
            id='builtin-' + domain.replace('.', '-'),
            name=domain,
            mode='hosts',
            match_domains=(domain,),
            listen_domains=(domain,),
            upstreams=tuple(Upstream('ip', ip) for ip in ips),
            enabled=True,
            order=order,
            notes='内置兜底',
        ))
    return tuple(result)


DEFAULT_PROFILES = _default_profiles()


def default_path():
    return os.path.join(paths.get_data_dir(), PROFILE_FILENAME)


def _as_domain_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(',', ';').split(';')
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        raise ProfileValidationError('域名列表必须是字符串或数组')
    result = []
    for item in items:
        if not isinstance(item, str):
            raise ProfileValidationError('域名必须是字符串')
        domain = item.strip().lower()
        if domain and domain not in result:
            result.append(domain)
    return result


def _validate_upstream(raw):
    if not isinstance(raw, dict):
        raise ProfileValidationError('upstream 必须是对象')
    type_ = str(raw.get('type') or 'ip').strip().lower()
    if type_ not in SUPPORTED_UPSTREAM_TYPES:
        raise ProfileValidationError(f'不支持的 upstream 类型：{type_}')
    value = str(raw.get('value') or '').strip()
    if not value:
        raise ProfileValidationError('upstream 缺少 value')
    if not hosts_manager.is_public_address(value):
        raise ProfileValidationError(f'upstream 不是公网 IP：{value}')
    port = raw.get('port', 443)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ProfileValidationError('upstream 端口非法')
    if not 1 <= port <= 65535:
        raise ProfileValidationError('upstream 端口越界')
    sni = raw.get('sni') or ''
    if not isinstance(sni, str):
        raise ProfileValidationError('sni 必须是字符串')
    return Upstream(type_, value, port, sni.strip())


def validate_profile(raw):
    """校验单条 profile；非法时抛出 ProfileValidationError。"""
    if not isinstance(raw, dict):
        raise ProfileValidationError('profile 必须是对象')

    profile_id = raw.get('id')
    name = raw.get('name') or profile_id
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ProfileValidationError('profile 缺少 id')
    if not isinstance(name, str) or not name.strip():
        raise ProfileValidationError('profile 缺少 name')

    mode = str(raw.get('mode') or 'hosts').strip().lower()
    if mode not in SUPPORTED_MODES:
        raise ProfileValidationError(f'当前版本不支持 mode：{mode}')

    match_domains = _as_domain_list(raw.get('match_domains'))
    if not match_domains:
        raise ProfileValidationError('profile 缺少 match_domains')
    for domain in match_domains:
        if not hosts_manager.is_allowed_domain(domain):
            raise ProfileValidationError(f'不在 GitHub 白名单的域名：{domain}')

    listen_domains = _as_domain_list(raw.get('listen_domains')) or list(match_domains)
    for domain in listen_domains:
        if not hosts_manager.is_allowed_domain(domain):
            raise ProfileValidationError(f'listen_domains 含非法域名：{domain}')

    upstreams_raw = raw.get('upstreams')
    if not isinstance(upstreams_raw, list) or not upstreams_raw:
        raise ProfileValidationError('profile 缺少 upstreams')
    upstreams = []
    seen = set()
    for item in upstreams_raw:
        upstream = _validate_upstream(item)
        key = (upstream.type, upstream.value, upstream.port)
        if key not in seen:
            seen.add(key)
            upstreams.append(upstream)

    enabled = raw.get('enabled', True)
    enabled = bool(enabled) if isinstance(enabled, bool) else True
    order = raw.get('order', 0)
    try:
        order = int(order)
    except (TypeError, ValueError):
        order = 0

    notes = raw.get('notes') or ''
    if not isinstance(notes, str):
        notes = ''

    return AccelerationProfile(
        id=profile_id.strip(),
        name=name.strip(),
        mode=mode,
        match_domains=tuple(match_domains),
        listen_domains=tuple(listen_domains),
        upstreams=tuple(upstreams),
        enabled=enabled,
        order=order,
        notes=notes.strip(),
    )


def load_profiles(path=None):
    """加载 profiles，返回 list[AccelerationProfile]。永不抛异常。"""
    path = path or default_path()
    if not os.path.exists(path):
        return list(DEFAULT_PROFILES)

    try:
        with open(path, 'r', encoding='utf-8') as file:
            data = json.load(file)
    except Exception:
        logger.warning('读取 profiles.json 失败，使用内置兜底配置：%s', path,
                       exc_info=True)
        return list(DEFAULT_PROFILES)

    if not isinstance(data, dict):
        logger.warning('profiles.json 根节点不是对象，使用内置兜底配置：%s', path)
        return list(DEFAULT_PROFILES)

    raw_profiles = data.get('profiles')
    if not isinstance(raw_profiles, list):
        logger.warning('profiles.json 缺少 profiles 数组，使用内置兜底配置：%s', path)
        return list(DEFAULT_PROFILES)
    if not raw_profiles:
        logger.info('profiles.json 为空，本次不使用额外加速配置')
        return []

    custom = []
    for index, raw in enumerate(raw_profiles):
        try:
            custom.append(validate_profile(raw))
        except ProfileValidationError as error:
            logger.warning('忽略非法 profile（第 %d 条）：%s', index + 1, error)

    if not custom:
        logger.warning('profiles.json 中没有合法配置，使用内置兜底配置：%s', path)
        return list(DEFAULT_PROFILES)

    result = list(DEFAULT_PROFILES) + custom
    logger.info('已加载 %d 个自定义加速配置（另有 %d 个内置配置）',
                len(custom), len(DEFAULT_PROFILES))
    return result


def merge_profile_upstreams(pairs, profiles=None, only_existing_domains=True):
    """把 enabled profile 的直连 IP 合并进候选 pairs。

    入参：pairs —— [(IP, 域名), ...]；profiles —— 可选，None 时加载默认文件。
    返回值：list[(IP, 域名)] —— 原条目 + 新增条目（去重，保持顺序）。
    """
    if profiles is None:
        profiles = load_profiles()
    known = set(pairs)
    existing_domains = {domain for _ip, domain in pairs}
    result = list(pairs)
    for profile in sorted(profiles, key=lambda item: item.order):
        if not profile.enabled or profile.mode != 'hosts':
            continue
        for domain in profile.match_domains:
            if only_existing_domains and domain not in existing_domains:
                continue
            if not hosts_manager.is_allowed_domain(domain):
                continue
            for upstream in profile.upstreams:
                if upstream.type != 'ip':
                    continue
                if not hosts_manager.is_public_address(upstream.value):
                    continue
                pair = (upstream.value, domain)
                if pair in known:
                    continue
                known.add(pair)
                result.append(pair)
    return result


def summaries(profiles=None):
    """返回供状态/UI 展示的 profile 摘要列表。"""
    if profiles is None:
        profiles = load_profiles()
    return [profile.summary() for profile in profiles]
