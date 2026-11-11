# -*- coding: utf-8 -*-
"""配置管理：config.json 的读取、校验与写回。

设计原则（core 层规则的落地示范）：
  - 不弹任何界面；所有失败路径都是"记日志 + 回退默认值"，绝不抛出致命
    异常打断程序启动 —— 配置文件坏了不应该让工具没法用；
  - 对外以"完整的配置 dict"为边界：调用方拿到的永远是校验后的干净数据，
    不需要自己再判断"这个字段存不存在、类型对不对"。

config.json 支持的字段：
  auto_update_on_enable   启用加速前是否先在线更新 IP 列表（默认 true）
  refresh_interval_hours  定时自动刷新间隔（小时，默认 6，最小 1）
  test_timeout_seconds    单次 TCP 测速超时（秒，默认 2.0，范围 0.5~10）
  strict_tls_verify       是否对核心域名做严格 TLS 证书校验（默认 true）
  window_width / window_height / window_x / window_y
                          主窗口尺寸与位置（可选；退出时保存，缺失按默认处理）
  ui_scale                保存窗口几何时的 DPI 缩放系数（内部使用）
"""

import json
import logging
import os
import shutil

from utils import fileio
from utils import paths

logger = logging.getLogger(__name__)

# 配置文件名（存放在程序目录）
CONFIG_FILENAME = 'config.json'

# 默认配置：config.json 缺失 / 字段非法时使用
DEFAULT_CONFIG = {
    'auto_update_on_enable': True,   # 启用加速前自动在线更新 IP 列表
    'refresh_interval_hours': 6,     # 定时自动刷新间隔（小时）
    'test_timeout_seconds': 2.0,     # 单次 TCP 测速超时（秒）
    'strict_tls_verify': True,       # 是否对核心域名做严格 TLS 证书校验
    'tls_failure_fallback': True,    # TLS 被网络干扰（重置/超时）时是否仍按 TCP 可用写入
    'git_mirror_enabled': False,     # 是否已启用 Git 镜像加速
    'preferred_mirror': '',          # 首选下载镜像（空=自动测速选择）
    'preferred_git_mirror': '',      # 首选 Git 镜像（空=自动测速选择）
    'git_mirror_scope': 'global',    # Git 镜像作用域（预留 local）
    'proxy_auto_detect': True,       # 是否自动检测系统代理
    'diagnose_on_startup': False,    # 启动时是否自动做网络诊断
}

# 窗口相关的可选键（缺失或非法时记为 None，界面层按默认值处理）
_WINDOW_KEYS = ('window_width', 'window_height', 'window_x', 'window_y')


def get_config_path(app_dir=None):
    """返回 config.json 的完整路径。

    入参：app_dir —— 指定目录（单元测试注入临时目录用）；None 表示程序目录。
    返回值：str —— config.json 完整路径。
    """
    return os.path.join(app_dir or paths.get_data_dir(), CONFIG_FILENAME)


def load_config(config_path=None):
    """读取并校验配置，返回干净的配置 dict（本函数永不抛异常）。

    入参：config_path —— 指定配置文件路径；None 用默认路径。
    返回值：dict —— 一定包含 DEFAULT_CONFIG 的全部键 + 全部 window_* 键。

    容错策略（每一种坏情况都有出路）：
      1. 文件不存在              -> 生成默认配置文件并返回默认值；
      2. JSON 语法错误 / 根不是对象 -> 记警告并返回默认值；注意不覆盖坏文件，
         方便用户把文件发给别人帮忙看（对比"文件被程序偷偷重置"的体验）；
      3. 单个字段类型非法        -> 只有该字段回退默认值，其他字段照常生效。
    """
    path = config_path or get_config_path()
    config = dict(DEFAULT_CONFIG)   # 拷贝一份，绝不修改模块级默认值
    for key in _WINDOW_KEYS:
        config[key] = None          # 窗口项先统一填 None（表示"未配置"）

    # ---- 情况 1：文件不存在，生成默认配置 ----
    if not os.path.exists(path):
        try:
            with open(path, 'w', encoding='utf-8') as file:
                json.dump(DEFAULT_CONFIG, file, ensure_ascii=False, indent=2)
            logger.info('已生成默认配置文件：%s', path)
        except Exception as error:
            logger.exception('生成默认配置文件失败：%s', error)
        return config

    # ---- 读取 + 解析（utf-8-sig：兼容用户用记事本保存时加的 BOM 头）----
    try:
        with open(path, 'r', encoding='utf-8-sig') as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError('配置根节点必须是 JSON 对象')
    except Exception as error:
        # 情况 2：文件坏了，返回默认值但绝不覆盖原文件（方便用户发给别人排查），
        # 同时另存一份 .bad 副本，便于对比与恢复。
        logger.warning('读取 config.json 失败（%s），使用默认配置', error)
        _backup_broken_config(path)
        return config

    # ---- 情况 3：逐字段校验，坏一个字段只回退那一个 ----

    # auto_update_on_enable：兼容 bool / 字符串 / 数字三种写法
    # （用户手改配置时可能写成 "true"、1 等，宽松解析比报错友好）
    raw = data.get('auto_update_on_enable', True)
    if isinstance(raw, bool):
        config['auto_update_on_enable'] = raw
    elif isinstance(raw, str):
        config['auto_update_on_enable'] = raw.strip().lower() in ('1', 'true', 'yes', 'on')
    elif isinstance(raw, (int, float)):
        config['auto_update_on_enable'] = bool(raw)
    else:
        logger.warning('配置项 auto_update_on_enable 类型非法（%r），使用默认值 true', raw)

    # refresh_interval_hours：必须是 >= 1 的整数
    # （0 或负数会让定时器空转打死 CPU，直接判非法）
    raw = data.get('refresh_interval_hours', 6)
    try:
        hours = int(raw)
        if hours < 1:
            raise ValueError('必须为不小于 1 的整数')
        config['refresh_interval_hours'] = hours
    except Exception:
        logger.warning('配置项 refresh_interval_hours 非法（%r），使用默认值 6', raw)

    # test_timeout_seconds：0.5~10 秒的浮点数（弱网用户可适当调大）
    raw = data.get('test_timeout_seconds', 2.0)
    try:
        timeout = float(raw)
        if not 0.5 <= timeout <= 10.0:
            raise ValueError('必须在 0.5~10 秒之间')
        config['test_timeout_seconds'] = timeout
    except Exception:
        logger.warning('配置项 test_timeout_seconds 非法（%r），使用默认值 2.0', raw)

    # strict_tls_verify：宽松布尔解析（与 auto_update_on_enable 相同的规则）
    raw = data.get('strict_tls_verify', True)
    if isinstance(raw, bool):
        config['strict_tls_verify'] = raw
    elif isinstance(raw, str):
        config['strict_tls_verify'] = raw.strip().lower() in ('1', 'true', 'yes', 'on')
    elif isinstance(raw, (int, float)):
        config['strict_tls_verify'] = bool(raw)
    else:
        logger.warning('配置项 strict_tls_verify 类型非法（%r），使用默认值 true', raw)

    # ui_scale：保存窗口几何时的 DPI 缩放系数（可选，浮点）
    if 'ui_scale' in data:
        try:
            scale = float(data['ui_scale'])
            if 0.5 <= scale <= 4.0:
                config['ui_scale'] = scale
            else:
                logger.warning('配置项 ui_scale 超出合理范围（%r），忽略', data['ui_scale'])
        except Exception:
            logger.warning('配置项 ui_scale 非法（%r），忽略', data['ui_scale'])

    # Git 镜像 / 代理相关配置（宽松解析）
    raw = data.get('git_mirror_enabled', False)
    if isinstance(raw, bool):
        config['git_mirror_enabled'] = raw
    elif isinstance(raw, str):
        config['git_mirror_enabled'] = raw.strip().lower() in ('1', 'true', 'yes', 'on')
    elif isinstance(raw, (int, float)):
        config['git_mirror_enabled'] = bool(raw)

    for key in ('preferred_mirror', 'preferred_git_mirror'):
        raw = data.get(key, '')
        if isinstance(raw, str):
            config[key] = raw.strip()
        else:
            logger.warning('配置项 %s 类型非法（%r），使用自动选择', key, raw)

    raw = data.get('git_mirror_scope', 'global')
    config['git_mirror_scope'] = raw if raw in ('global', 'local') else 'global'

    raw = data.get('proxy_auto_detect', True)
    if isinstance(raw, bool):
        config['proxy_auto_detect'] = raw
    elif isinstance(raw, str):
        config['proxy_auto_detect'] = raw.strip().lower() in ('1', 'true', 'yes', 'on')
    else:
        config['proxy_auto_detect'] = bool(raw)

    raw = data.get('tls_failure_fallback', True)
    if isinstance(raw, bool):
        config['tls_failure_fallback'] = raw
    elif isinstance(raw, str):
        config['tls_failure_fallback'] = raw.strip().lower() in ('1', 'true', 'yes', 'on')
    else:
        config['tls_failure_fallback'] = bool(raw)

    raw = data.get('diagnose_on_startup', False)
    config['diagnose_on_startup'] = bool(raw) if isinstance(raw, bool) else False

    # window_*：可选，能转 int 就用，否则保持 None
    for key in _WINDOW_KEYS:
        if key in data:
            try:
                config[key] = int(data[key])
            except Exception:
                logger.warning('配置项 %s 非法（%r），忽略', key, data[key])

    logger.info('配置加载完成：%s', config)
    return config


def save_config(data, config_path=None):
    """把完整配置 dict 原样写入 config.json。

    入参：data —— 要写入的完整配置 dict；
          config_path —— 指定路径；None 用默认路径。
    返回值：bool —— 是否写入成功（失败只记日志，如何提示由调用方决定）。
    """
    path = config_path or get_config_path()
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        # 原子写：退出/掉电时不会把 config.json 写成空文件或半截 JSON
        fileio.atomic_write_text(path, payload, encoding='utf-8')
        return True
    except Exception:
        logger.exception('写入配置文件失败：%s', path)
        return False


def _backup_broken_config(path):
    """把损坏的配置文件另存为 .bad 副本；失败只记日志。"""
    bad_path = path + '.bad'
    try:
        shutil.copyfile(path, bad_path)
        logger.info('损坏的配置已另存：%s', bad_path)
    except Exception as error:
        logger.debug('另存损坏配置失败（忽略）：%s', error)


def merge_config_values(values, config_path=None):
    """合并写入：保留 config.json 已有的其他字段，只更新 values 里的键。

    功能：退出程序时保存窗口位置尺寸用。config.json 里可能有用户手写的
    其他字段，所以绝不能拿"只含 window_* 的新 dict"整文件覆盖。

    入参：values —— dict，要更新的键值对（如 {'window_width': 800}）；
          config_path —— 指定路径；None 用默认路径。
    返回值：bool —— 是否写入成功。
    """
    path = config_path or get_config_path()
    data = {}
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8-sig') as file:
                data = json.load(file)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            # 原文件已损坏：就以本次 values 为准重建（没有更好的选择了）
            data = {}
    data.update(values)
    return save_config(data, path)
