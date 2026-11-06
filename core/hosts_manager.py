# -*- coding: utf-8 -*-
r"""hosts 文件管理：系统 hosts 的读写 / 备份 / 还原，以及候选列表文件维护。

职责范围（两类"hosts 格式"的文件）：
  1. 系统 hosts：C:\Windows\System32\drivers\etc\hosts
     - 加速段 = 带「开始 / 结束标记」的一段条目，只动这一段，其他内容不碰；
     - 备份 = 首次启用前把原始 hosts 复制一份，绝不覆盖（那是用户唯一的
       "回到初始状态"的凭据）。
  2. 候选列表：程序目录下 github_hosts.txt
     - 用户可手编的「IP 域名」清单，是测速的输入；同样是 hosts 行格式。

core 层规则：本模块绝不弹窗、绝不 import 界面库；出错时记日志并返回
(False, 错误消息) 之类的结果，如何提示用户由 ui 层决定。

编码说明（为什么用 surrogateescape）：
  系统 hosts 历史上可能是任何编码（ANSI/GBK/UTF-8）。读文件时用
  errors='surrogateescape' 解码：无法解码的字节被映射到保留区，写回时
  原样还原 —— 无论原文件是什么编码，"读进来再写出去"字节级无损。
"""

import ipaddress
import logging
import os
import re
import subprocess

from utils import fileio
from utils import paths

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 系统 hosts 路径与备份路径（备份与 hosts 同目录，权限一致）
HOSTS_PATH = r'C:\Windows\System32\drivers\etc\hosts'
BACKUP_PATH = r'C:\Windows\System32\drivers\etc\hosts.github_backup'

# 候选列表文件名（存放在程序目录）
CANDIDATE_FILENAME = 'github_hosts.txt'

# hosts 中加速段的开始 / 结束标记（还原时靠识别标记来"只删这一段"）
START_MARKER = '# === GitHub Accelerate Start ==='
END_MARKER = '# === GitHub Accelerate End ==='

# 加速段正则：
#   SECTION_RE          完整段（开始标记...结束标记）
#   UNCLOSED_SECTION_RE 未闭合段（只有开始标记直到文件尾，上次写入被打断的残骸）
#   ACCEL_SECTION_RE    提取段内内容（统计生效条目数用）
SECTION_RE = re.compile(
    re.escape(START_MARKER) + r'.*?' + re.escape(END_MARKER) + r'[ \t]*\r?\n?', re.S)
UNCLOSED_SECTION_RE = re.compile(re.escape(START_MARKER) + r'.*\Z', re.S)
ACCEL_SECTION_RE = re.compile(
    re.escape(START_MARKER) + '(.*?)' + re.escape(END_MARKER), re.S)

# IPv4 / 域名粗略校验
_IPV4_RE = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')
_DOMAIN_RE = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$')

# ipconfig 子进程不弹黑窗口的标志位
CREATE_NO_WINDOW = 0x08000000

# 允许写入系统 hosts 的域名后缀白名单（GitHub 主站及其静态资源 / 对象存储 / 附属服务）。
# 为什么需要白名单：远端数据源与本地候选文件都是「外部输入」，如果不过滤，
# 一条 '1.2.3.4 bank.example' 就能把任意域名解析到指定 IP（钓鱼 / 中间人）。
ALLOWED_DOMAIN_SUFFIXES = (
    'github.com',             # 主站 / API / codeload / gist / raw 等全部子域
    'github.io',              # GitHub Pages
    'github.blog',
    'github.community',
    'githubstatus.com',
    'githubcopilot.com',
    'githubusercontent.com',  # raw / avatars / user-images / objects 等
    'githubassets.com',
    'fastly.net',             # github.map.fastly.net / github.global.ssl.fastly.net
    'amazonaws.com',          # Release 附件所在的 S3 对象存储域名
    'vscode.dev',             # GitHub520 列表附带的 VS Code 在线版域名
)

# 远端合并进候选文件时打的来源标记（行尾注释，不影响「IP 域名」解析）。
# 只有带标记的行才会在后续更新中被自动淘汰，用户手工写的行永远不动。
REMOTE_LINE_MARK = '  # gha:remote'

# 每个域名最多保留多少条「远端来源」候选（超出时淘汰最早加入的）。
MAX_REMOTE_PER_DOMAIN = 6

# github_hosts.txt 缺失时自动生成的模板
CANDIDATE_TEMPLATE = """\
# ============================================================
# github_hosts.txt —— GitHub 加速 IP 候选列表（可手动编辑）
#
# 格式：每行一条「IP 域名」，支持 # 注释行和空行，例如：
#     140.82.114.3 github.com
# 也支持 IPv6（直接用标准写法，无需方括号）：
#     2606:50c0:8000::153 github.com
#
# 启用加速时程序会对每个 IP 做 TCP 443 测速（超时 2 秒，并发进行），
# 每个域名只保留延迟最低且连通的 IP 写入系统 hosts；
# 若全部失败则不改动 hosts。
#
# 下方 IP 为常见节点的占位示例，请按实际测速情况自行补充 / 替换，
# 也可在主窗口中手动添加 / 编辑 / 删除，或用「更新 IP 列表」在线获取。
# IP 可通过 https://www.ipaddress.com 等网站查询以下域名：
#   github.com / api.github.com / codeload.github.com /
#   gist.github.com / github.global.ssl.fastly.net /
#   raw.githubusercontent.com / objects.githubusercontent.com
# ============================================================

# ---- github.com ----
140.82.112.3 github.com
140.82.113.3 github.com
140.82.114.3 github.com
20.205.243.166 github.com

# ---- api.github.com ----
140.82.112.6 api.github.com
140.82.113.6 api.github.com

# ---- codeload.github.com ----
140.82.112.10 codeload.github.com
140.82.113.10 codeload.github.com

# ---- gist.github.com ----
140.82.112.9 gist.github.com

# ---- github.global.ssl.fastly.net ----
199.232.69.194 github.global.ssl.fastly.net
151.101.1.194 github.global.ssl.fastly.net

# ---- raw.githubusercontent.com ----
185.199.108.133 raw.githubusercontent.com
185.199.109.133 raw.githubusercontent.com

# ---- gist / objects.githubusercontent.com ----
185.199.108.133 gist.githubusercontent.com
185.199.109.133 gist.githubusercontent.com
185.199.108.133 objects.githubusercontent.com
185.199.109.133 objects.githubusercontent.com
"""


# ---------------------------------------------------------------------------
# 系统 hosts：读 / 写 / 备份 / 还原
# ---------------------------------------------------------------------------

def read_hosts_text():
    """读取系统 hosts 文本（字节级无损，见模块 docstring 的编码说明）。

    入参：无。
    返回值：str —— hosts 全文；读取失败时抛异常（由调用方决定如何处理）。
    """
    with open(HOSTS_PATH, 'rb') as file:
        return file.read().decode('utf-8', errors='surrogateescape')


def write_hosts_text(text):
    """写回系统 hosts 文本；与 read_hosts_text 配套，保证字节无损。

    入参：text —— 要写入的完整文本。
    返回值：无；写入失败抛异常（写 hosts 是关键动作，失败必须让调用方知道）。
    """
    # 原子写：先写同目录临时文件，fsync 后整体替换；失败时原 hosts 保持原样。
    fileio.atomic_write_bytes(
        HOSTS_PATH, text.encode('utf-8', errors='surrogateescape'))


def _read_text(path):
    """读取任意文本文件（与 hosts 相同的编码策略：UTF-8 + surrogateescape）。"""
    with open(path, 'rb') as file:
        return file.read().decode('utf-8', errors='surrogateescape')


def _detect_newline(text):
    """探测文本的主要换行风格，返回 '\r\n' 或 '\n'（默认 LF）。

    为什么：系统 hosts 通常是 CRLF，直接追加 LF 会造成文件里两种换行混排，
    部分编辑器 / 安全软件会因此提示"文件格式异常"。
    """
    if '\r\n' in text:
        return '\r\n'
    return '\n'


def remove_accel_sections(text):
    """移除文本中已存在的加速段（完整段 + 未闭合残段）。

    入参：text —— hosts 全文。
    返回值：str —— 去掉加速段后的文本。

    为什么要两个正则？正常情况只有完整段；但如果上次写入中途断电 / 崩溃，
    文件尾部可能残留一个没有结束标记的段 —— 不清理会越叠越多。
    """
    text = SECTION_RE.sub('', text)
    text = UNCLOSED_SECTION_RE.sub('', text)
    return text


def write_accel_section(best):
    """把测速优选结果写入系统 hosts（先删旧段，再把新段放到文件最前面）。

    入参：best —— {域名: IP} 优选结果映射。
    返回值：list[str] —— 写入后自检不一致的条目（空列表表示全部命中）。

    为什么"先删后加"？hosts 是纯文本，段落位置会漂移；整段删除再重写，
    逻辑最简单，也不会留下半新半旧的中间状态。

    为什么新段放文件最前面？Windows 的 DNS 客户端按文件顺序匹配 hosts、
    **首个命中生效**。如果用户 hosts 里已经存在同名条目（旧加速工具、手工
    配置、系统镜像预置），追加到文件末尾的段永远不会生效——程序显示"已启用"，
    实际什么都没改。置顶可以保证我们的映射优先；原条目仍保留在文件里
    （备份也在），停用时整体还原即可。
    """
    original = remove_accel_sections(read_hosts_text())
    newline = _detect_newline(original)
    # 保留 UTF-8 BOM（如果有）：它必须是文件的第一个字符
    header = ''
    if original.startswith('\ufeff'):
        header, original = '\ufeff', original[1:]
    entries = [f'{ip} {domain}' for domain, ip in best.items()]
    section = (START_MARKER + newline + newline.join(entries)
               + newline + END_MARKER + newline)
    body = original.rstrip('\r\n')
    new_text = header + section + ((newline + body + newline) if body else '')
    write_hosts_text(new_text)
    for domain, ip in best.items():
        logger.info('写入 hosts 条目：%s %s', ip, domain)
    return verify_accel_section(best)


def verify_accel_section(best):
    """写入后自检：逐个域名确认 hosts 实际生效的 IP 就是刚写入的优选 IP。

    入参：best —— {域名: IP} 优选结果映射。
    返回值：list[str] —— 不一致条目的描述（空列表表示全部命中）。

    为什么需要自检？hosts 的生效规则是"先匹配先赢"，任何意外（权限、
    编码、其他软件并发改写）都可能让写入"看起来成功、实际没生效"。
    """
    mismatched = []
    for domain, ip in best.items():
        actual = find_hosts_ip(domain)
        if actual != ip:
            mismatched.append(f'{domain}（期望 {ip}，实际 {actual}）')
    if mismatched:
        logger.error('hosts 写入后自检不一致：%s', '；'.join(mismatched))
    else:
        logger.info('hosts 写入后自检通过：%d 个域名均命中加速段', len(best))
    return mismatched


def count_accel_entries():
    """统计系统 hosts 加速段内生效的条目数量。

    入参：无。
    返回值：int —— 生效条目数；无加速段或读取失败返回 0
             （统计失败不该让界面报错，0 是安全的降级值）。
    """
    try:
        match = ACCEL_SECTION_RE.search(read_hosts_text())
        if not match:
            return 0
        return len([line for line in match.group(1).splitlines()
                    if line.strip() and not line.strip().startswith('#')])
    except Exception as error:
        logger.warning('统计 hosts 加速段条目失败：%s', error)
        return 0


def ensure_backup():
    """首次启用前备份原始 hosts（备份已存在则绝不覆盖）。

    入参：无。
    返回值：(bool, str) —— (是否成功, 失败原因 / '已备份' 描述)。

    为什么绝不覆盖备份？
      备份是"用户原始 hosts"的唯一凭据。如果加速启用状态下再覆盖备份，
      备份里存的就是加速后的内容，还原就失去意义了。
    """
    if os.path.exists(BACKUP_PATH):
        logger.info('已存在原始 hosts 备份，跳过备份：%s', BACKUP_PATH)
        return True, '备份已存在'
    try:
        text = read_hosts_text()
        cleaned = False
        if START_MARKER in text:
            # 首次运行时 hosts 已含加速段（换机 / 旧版残留 / 备份被清理软件删掉）：
            # 若原样备份，"还原"就会还原成加速状态。先剔除加速段再备份。
            text = remove_accel_sections(text)
            cleaned = True
            logger.warning('首次备份前发现 hosts 已含加速段，已剔除后再保存备份')
        fileio.atomic_write_text(BACKUP_PATH, text,
                                 encoding='utf-8', errors='surrogateescape')
        logger.info('已备份原始 hosts -> %s', BACKUP_PATH)
        return True, ('已备份（已剔除旧的加速段）' if cleaned else '已备份')
    except Exception as error:
        logger.exception('备份 hosts 失败')
        return False, f'备份 hosts 失败：{error}'


def restore_from_backup():
    """用备份覆盖系统 hosts 并刷新 DNS（停用 / 强制还原共用）。

    入参：无。
    返回值：(bool, str) —— (是否成功, 消息)。备份不存在返回
             (False, '未找到备份文件，无法还原')。
    """
    if os.path.exists(BACKUP_PATH):
        try:
            backup_text = _read_text(BACKUP_PATH)
            if START_MARKER in backup_text:
                # 防御历史遗留的"被加速内容污染"的备份：还原时再剔除一次，
                # 保证还原后的 hosts 一定不含加速段。
                logger.warning('备份文件中含加速段（历史遗留），还原时已剔除')
                backup_text = remove_accel_sections(backup_text)
            write_hosts_text(backup_text)
            logger.info('已用备份还原 hosts：%s -> %s', BACKUP_PATH, HOSTS_PATH)
            flush_dns()
            return True, 'hosts 已还原'
        except Exception as error:
            logger.exception('还原 hosts 失败')
            return False, f'还原 hosts 失败：{error}'

    # 兜底：备份缺失时不能"无路可走"。既然 hosts 里有我们写的标记，
    # 就按标记移除加速段——这正是"恢复原状"想要的结果。
    logger.warning('hosts 备份不存在，尝试按标记移除加速段：%s', BACKUP_PATH)
    try:
        text = read_hosts_text()
    except Exception as error:
        logger.exception('读取 hosts 失败')
        return False, f'未找到备份文件，且读取 hosts 失败：{error}'
    if START_MARKER not in text:
        return False, '未找到备份文件，且 hosts 中没有加速段（无需还原）'
    try:
        write_hosts_text(remove_accel_sections(text))
        flush_dns()
        logger.info('备份缺失，已按标记移除加速段')
        return True, '备份缺失，已按标记移除加速段'
    except Exception as error:
        logger.exception('移除加速段失败')
        return False, f'移除加速段失败：{error}'


def flush_dns():
    """执行 ipconfig /flushdns 刷新 DNS 缓存（写入 hosts 后必须做）。

    入参：无。
    返回值：bool —— 是否成功。失败只记日志，由调用方决定是否提示用户；
             DNS 刷新失败不影响 hosts 已写入的事实。
    """
    try:
        result = subprocess.run(['ipconfig', '/flushdns'], capture_output=True,
                                creationflags=CREATE_NO_WINDOW, timeout=15)
        if result.returncode == 0:
            logger.info('DNS 缓存刷新成功')
            return True
        output = (result.stderr or result.stdout or b'').decode(
            'gbk', errors='ignore').strip()
        logger.warning('DNS 缓存刷新失败（返回码 %s）：%s', result.returncode, output)
        return False
    except Exception as error:
        logger.exception('DNS 缓存刷新异常：%s', error)
        return False


def find_hosts_ip(domain):
    """查找系统 hosts 中某域名当前生效的 IP（文件顺序首个匹配）。

    入参：domain —— 域名，如 'github.com'。
    返回值：str 或 None —— 生效 IP；未找到 / 读取失败返回 None。
    """
    try:
        text = read_hosts_text()
    except Exception as error:
        logger.warning('读取 hosts 以查找 %s 失败：%s', domain, error)
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1].lower() == domain.lower() \
                and looks_like_ip(parts[0]):
            return parts[0]
    return None


# ---------------------------------------------------------------------------
# 候选列表文件（github_hosts.txt）
# ---------------------------------------------------------------------------

def get_candidate_path():
    """返回候选列表文件完整路径。入参：无。返回值：str 路径。"""
    return os.path.join(paths.get_data_dir(), CANDIDATE_FILENAME)


def parse_pairs(text, source):
    """解析「IP 域名」格式文本为 [(IP, 域名), ...]。

    入参：text —— 文件 / 远端返回的全文；
          source —— 来源描述（仅用于日志定位是哪个文件解析出错）。
    返回值：list[(ip, domain)]。

    容错：无法识别的行记警告并跳过，不中断整体解析 ——
    用户手编的文件里混进一行错误格式很正常，不能因此整个文件作废。
    """
    pairs = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue  # 空行 / 注释行
        parts = line.split()
        if len(parts) >= 2 and looks_like_ip(parts[0]):
            pairs.append((parts[0], parts[1]))
        else:
            logger.warning('解析 %s 时跳过无法识别的行：%s', source, line[:100])
    return pairs


def load_candidate_pairs():
    """读取候选列表文件为 [(IP, 域名), ...]；文件缺失时先生成模板。

    入参：无。
    返回值：list[(ip, domain)]；读取失败抛异常（调用方捕获后提示用户）。
    """
    path = get_candidate_path()
    if not os.path.exists(path):
        logger.warning('未找到 %s，自动生成模板文件', path)
        with open(path, 'w', encoding='utf-8', newline='\n') as file:
            file.write(CANDIDATE_TEMPLATE)
        logger.info('已生成候选模板：%s', path)
    # utf-8-sig：兼容用户用记事本编辑后带上的 BOM 头
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as file:
        return parse_pairs(file.read(), '本地候选文件')


def add_candidate_pair(ip, domain):
    """把一条新候选追加到 github_hosts.txt 末尾（格式校验由调用方完成）。

    入参：ip / domain —— 已校验合法的字符串。返回值：无。
    """
    if not os.path.exists(get_candidate_path()):
        load_candidate_pairs()  # 先生成模板，保证追加有落点
    with open(get_candidate_path(), 'a', encoding='utf-8', newline='\n') as file:
        file.write(f'{ip} {domain}\n')


def remove_candidate_pairs(remove_set):
    """从候选文件删除给定 (IP, 域名) 对应的行。

    入参：remove_set —— {(IP, 域名), ...} 要删除的条目集合。
    返回值：int —— 实际删除的行数。

    实现要点：逐行读入、命中删除集合的行丢弃、其余原样写回 ——
    注释和空行全部保留（用户的文件，注释也是内容）。
    """
    with open(get_candidate_path(), 'r', encoding='utf-8-sig', errors='replace') as file:
        lines = file.readlines()
    kept, removed = [], 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            parts = stripped.split()
            if len(parts) >= 2 and (parts[0], parts[1]) in remove_set:
                removed += 1
                continue  # 命中删除集合，丢弃该行
        kept.append(line)
    with open(get_candidate_path(), 'w', encoding='utf-8', newline='') as file:
        file.writelines(kept)
    return removed


def replace_candidate_pair(old_ip, old_domain, new_ip):
    """把文件中第一条 (old_ip, old_domain) 行的 IP 改为 new_ip。

    入参：old_ip / old_domain —— 定位要改的行；new_ip —— 新 IP。
    返回值：bool —— 是否修改成功（False = 没找到匹配行）。
    """
    with open(get_candidate_path(), 'r', encoding='utf-8-sig', errors='replace') as file:
        lines = file.readlines()
    changed, kept = False, []
    for line in lines:
        stripped = line.strip()
        if not changed and stripped and not stripped.startswith('#'):
            parts = stripped.split()
            if len(parts) >= 2 and parts[0] == old_ip and parts[1] == old_domain:
                ending = '\n' if line.endswith('\n') else ''
                kept.append(f'{new_ip} {old_domain}{ending}')
                changed = True
                continue
        kept.append(line)
    if changed:
        with open(get_candidate_path(), 'w', encoding='utf-8', newline='') as file:
            file.writelines(kept)
    return changed


def merge_remote_into_local(remote_pairs):
    """把远端 (IP, 域名) 对合并进本地候选文件。

    入参：remote_pairs —— [(IP, 域名), ...] 远端解析出的条目。
    返回值：(added, total) —— 新增条数、合并后总条数。

    合并规则（保持"测速选优"能力不被破坏）：
      - 保留本地文件原有全部行（含注释），用户手工维护的内容不丢失；
      - 完全相同的 (IP, 域名) 去重，绝不重复追加；
      - 同一域名的不同 IP 全部保留为候选 —— 选优发生在测速环节；
      - 远端加入的行带 REMOTE_LINE_MARK 标记，每个域名最多保留
        MAX_REMOTE_PER_DOMAIN 条，超出时淘汰最早加入的。这解决了
        "远端 IP 漂移 -> 旧 IP 永久堆积 -> 每次启用测速越来越慢"的问题，
        同时绝不删除用户手工写的行。
    """
    path = get_candidate_path()
    if not os.path.exists(path):
        load_candidate_pairs()  # 本地文件缺失时先生成模板
    with open(path, 'r', encoding='utf-8-sig', errors='replace') as file:
        text = file.read()
    original_pairs = set(parse_pairs(text, '本地候选文件'))
    seen_pairs = set(original_pairs)
    new_lines = []
    for pair in remote_pairs:
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            new_lines.append(f'{pair[0]} {pair[1]}{REMOTE_LINE_MARK}')
    if not new_lines:
        return 0, len(original_pairs)

    lines = text.splitlines()
    lines.extend(new_lines)
    lines = _prune_remote_lines(lines)
    merged_text = '\n'.join(lines) + '\n'
    fileio.atomic_write_text(path, merged_text, encoding='utf-8')
    merged_pairs = set(parse_pairs(merged_text, '本地候选文件'))
    added = len(merged_pairs - original_pairs)
    return added, len(merged_pairs)


def _prune_remote_lines(lines):
    """限制每个域名保留的「远端来源」行数，淘汰最早加入的（保持文件顺序）。

    入参：lines —— 候选文件的所有行。
    返回值：list[str] —— 淘汰后的行列表。
    （只处理带 REMOTE_LINE_MARK 的行；用户手写的行原样保留。）
    """
    indexes_by_domain = {}
    drop_indexes = set()
    for index, line in enumerate(lines):
        if REMOTE_LINE_MARK not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        indexes = indexes_by_domain.setdefault(parts[1].lower(), [])
        indexes.append(index)
        if len(indexes) > MAX_REMOTE_PER_DOMAIN:
            drop_indexes.add(indexes.pop(0))
    if not drop_indexes:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop_indexes]


# ---------------------------------------------------------------------------
# 格式校验（供 ui 层输入框校验复用）
# ---------------------------------------------------------------------------

def is_allowed_domain(domain):
    """域名是否属于允许写入 hosts 的 GitHub 相关域名（后缀白名单）。

    入参：domain —— 域名字符串。返回值：bool。
    （子域一律放行，例如 api.github.com / raw.githubusercontent.com。）
    """
    if not isinstance(domain, str):
        return False
    domain = domain.strip().lower().rstrip('.')
    if not domain:
        return False
    return any(domain == suffix or domain.endswith('.' + suffix)
               for suffix in ALLOWED_DOMAIN_SUFFIXES)


def is_public_ipv4(text):
    """是否为可安全写入 hosts 的「公网」IPv4 地址。

    入参：text —— IP 字符串。返回值：bool。

    排除私网 / 回环 / 链路本地 / 组播 / 保留 / 未指定地址：
    这些地址指向本机或内网，写进 hosts 只会造成解析异常，也可能是
    被污染的候选数据（例如手工误加的 '192.168.1.1 example.com'）。
    ipaddress.IPv4Address.is_global 已经覆盖上述全部情况。
    """
    if not looks_like_ipv4(text):
        return False
    try:
        return ipaddress.IPv4Address(text).is_global
    except ValueError:
        return False


def filter_allowed_entries(pairs):
    """按白名单与公网 IP 规则筛选候选条目。

    入参：pairs —— [(IP, 域名), ...]。
    返回值：(allowed, rejected) —— 两个列表，元素均为 (IP, 域名)。
    用途：远端数据合并前、写入 hosts 前各调用一次，形成双保险。
    """
    allowed, rejected = [], []
    for ip, domain in pairs:
        if is_public_address(ip) and is_allowed_domain(domain):
            allowed.append((ip, domain))
        else:
            rejected.append((ip, domain))
    return allowed, rejected


def looks_like_ipv4(text):
    """粗略校验是否为合法 IPv4（每段 0-255）。

    入参：text —— 待校验字符串。返回值：bool。
    （公开函数：ui 层的输入框校验也要用，格式校验归本模块管。）
    """
    match = _IPV4_RE.match(text)
    if not match:
        return False
    return all(0 <= int(group) <= 255 for group in match.groups())


def looks_like_ipv6(text):
    """校验是否为合法 IPv6 地址（用标准库 ipaddress 解析）。

    入参：text —— 待校验字符串。返回值：bool。
    """
    if not isinstance(text, str) or ':' not in text:
        return False
    try:
        return isinstance(ipaddress.ip_address(text.strip()), ipaddress.IPv6Address)
    except ValueError:
        return False


def looks_like_ip(text):
    """校验是否为合法 IP（IPv4 或 IPv6）。

    入参：text —— 待校验字符串。返回值：bool。
    """
    return looks_like_ipv4(text) or looks_like_ipv6(text)


def is_public_address(text):
    """是否为可安全写入 hosts 的「公网」IP（IPv4 / IPv6 通用）。

    入参：text —— IP 字符串。返回值：bool。
    ipaddress 的 is_global 会排除私网 / 回环 / 链路本地 / 组播 / 保留 / 未指定地址。
    """
    if not looks_like_ip(text):
        return False
    try:
        return ipaddress.ip_address(text.strip()).is_global
    except ValueError:
        return False


def looks_like_domain(text):
    """粗略校验域名格式（字母/数字/点/连字符，不允许空格与连续点）。

    入参：text —— 待校验字符串。返回值：bool。
    （公开函数：ui 层的输入框校验也要用。）
    """
    return bool(_DOMAIN_RE.match(text)) and '..' not in text


def is_valid_ipv4(text):
    """IPv4 校验的公开入口（ui 层输入框校验用）。

    入参：text —— 待校验字符串。返回值：bool。
    """
    return looks_like_ipv4(text)


def is_valid_ip(text):
    """IP 校验的公开入口（ui 层输入框校验用，支持 IPv4 / IPv6）。

    入参：text —— 待校验字符串。返回值：bool。
    """
    return looks_like_ip(text)


def is_valid_domain(text):
    """域名校验的公开入口（ui 层输入框校验用）。

    入参：text —— 待校验字符串。返回值：bool。
    """
    return looks_like_domain(text)
