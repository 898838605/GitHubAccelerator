# GitHub Hosts 加速器（GitHubAccelerator）

Windows 桌面小工具：一键加速 GitHub 访问，常驻系统托盘 + 图形主窗口。

**当前版本：** 5.2.1（更新记录见 [CHANGELOG.md](CHANGELOG.md)）

## 1. 项目简介

访问 GitHub 网页慢、`git clone` 卡住、头像 / 附件加载不出来——多数情况不是网络断了，而是**域名解析到的 IP 不可达或质量差**。本工具把 GitHub 的常用域名直接指向"实测可达且延迟最低"的 IP，写进系统 hosts 文件，绕过 DNS 环节，从而加速访问。

> v5.2 起提供三种加速方式：**Git 镜像加速**（clone/pull，跨系统、无需代理）、
> **下载镜像加速**（Release/raw，自动选最快节点）、**hosts 直连加速**（只解决 DNS 层）。
> 网页浏览/API 全站加速必须使用代理——hosts 和镜像都无法绕过网络层限速。

**主要功能：**

| 功能 | 说明 |
|---|---|
| 启用加速 | 在线更新 IP（可选）→ 备份 hosts → 并发测速 → 每个域名优选最低延迟 IP → 写入 hosts → 刷新 DNS |
| 停用加速 | 用备份还原 hosts，恢复原状 |
| 强制还原 | 不依赖内部状态，只要备份存在就把原始 hosts 盖回去（hosts 被弄乱时的"救命"按钮） |
| 更新 IP 列表 | 从 GitHub520 稳定地址（`https://raw.hellogithub.com/hosts`）拉取最新 IP，与本地合并去重 |
| 全部 / 选中测速 | TCP 443 并发测速，延迟显示在表格中，可按列排序 |
| 连通性测试 | 检测 `github.com` / `raw.githubusercontent.com` 当前生效 IP 是否可达 |
| 代理检测 | 只读识别系统代理（环境变量 / WinINET / PAC），提示 hosts 加速在代理网络下可能不适用 |
| IPv6 支持 | 候选清单与 hosts 加速段支持公网 IPv6 地址（`IP 域名` 行），标题排序 v4/v6 混排正确 |
| 定时自动刷新 | 每 N 小时（可配置）静默执行一次完整刷新（仅加速启用时） |
| 托盘常驻 | 关闭窗口只是隐藏到托盘；托盘菜单可完成全部操作；退出前自动还原 hosts |
| 下载加速链接生成 | 用镜像代理前缀拼接 Release 文件 / 源码包的加速下载链接，一键复制或直接打开（不改 hosts、不改系统配置） |
| **Git 镜像加速（v5.2）** | 自动测速并写入 git insteadOf 规则，让 clone/pull 走公共镜像；备份原配置、一键还原；无需管理员 |
| **下载节点测速（v5.2）** | 并发测试镜像节点延迟并自动选最快，下载链接优先使用 |
| **Git 代理（v5.2）** | 检测系统代理并一键写入 git 的 http/https.proxy；清除时恢复原配置 |

### 三种加速方式（v5.2 起）

| 方式 | 加速对象 | 需要管理员 | 需要外部代理 | 说明 |
|---|---|---|---|---|
| **Git 镜像加速** | git clone / pull | 否 | 否 | 自动测速公共镜像，通过 git `insteadOf` 把 `https://github.com/` 透明重写到最快节点；启用前备份原 Git 配置，可一键还原 |
| **下载镜像加速** | Release / raw / 源码包 | 否 | 否 | 镜像前缀 + 节点测速，自动选最快节点；生成可复制的加速下载链接 |
| **hosts 直连** | 网页 / API | 是 | 否 | 只改 DNS 解析；如果网络对 GitHub IP 做了限速/阻断，hosts 无效 |
| **代理（辅助）** | 网页 / 全站 | 否 | 是 | 检测系统代理并一键配置 Git 代理；浏览器加速需要系统代理本身可用 |

> **为什么"可用"的 IP 不一定写进 hosts？** 表格里的"可用"是 TCP 443 连通；
> 写 hosts 前还会对核心域名做 TLS 校验。现在同一域名有多个候选 IP，
> TLS 首选被重置/超时会自动换备选；若全部都是网络层干扰（不是证书错误），
> 仍会按 TCP 可用 IP 写入并在结果里提示，证书错误则跳过。
> hosts 每个域名只能生效一条映射，写入多个 IP 没有意义（系统按第一条匹配）。

> **适用边界：** hosts 与镜像都无法绕过网络层限速。若 `github.com` 直连大流量受限，
> 网页浏览必须使用代理；程序会在 hosts 启用后实际探测 HTTPS 传输，
> 效果有限时会明确提示，而不再只显示"已启用"。

**运行要求：** Windows 10/11，全系统单实例运行。启动不强制管理员：浏览、管理候选、生成下载链接都能直接使用，只有启用 / 停用 / 还原（修改系统 hosts）时才按需请求管理员权限。

## 2. 实现原理

### 2.1 为什么改 hosts 能加速 GitHub

访问一个域名的流程是：**DNS 解析（域名 → IP）→ 与该 IP 建立连接 → 传输数据**。GitHub 慢的瓶颈通常在第一步：DNS 得到的 IP 被污染、或 CDN 把用户调度到了绕路的远端节点。

hosts 文件是操作系统级的手工 DNS 映射表，**优先级高于 DNS 查询**。把域名直接指向一个实测可达、延迟低的 IP，就跳过了"解析"这个不可控环节——连接质量完全由你指定的 IP 决定。

本工具写入的加速段长这样（v5.0 起写在文件最前面，保证在已有同名条目时仍然生效，原条目不动）：

```
# === GitHub Accelerate Start ===
140.82.121.3 github.com
140.82.121.6 api.github.com
...
# === GitHub Accelerate End ===
```

### 2.2 备份与还原机制（安全网）

设计原则：**用户随时能一键回到最初状态**。

- 首次启用前，把原始 hosts 复制为 `hosts.github_backup`，**之后绝不覆盖**——备份是"回到初始状态"的唯一凭据，加速启用状态下再覆盖备份，还原就失去意义了；
- 还原 = 用备份整体覆盖 hosts + 执行 `ipconfig /flushdns` 刷新 DNS 缓存（不刷缓存，系统可能继续用旧映射）；
- 三个还原入口：停用加速、强制还原、退出程序（启用状态下退出自动还原）；
- 定时刷新在备份缺失时**拒绝执行**——防止把"已加速的 hosts"备份成"原始 hosts"。

### 2.3 为什么用 TCP 443 测速而不是 ping

| 方式 | 问题 |
|---|---|
| ping（ICMP） | 很多机房直接禁掉 ICMP，不通 ≠ 不可用，结果不可信 |
| HTTP(S) 探测 | 要完成 DNS + TLS 握手 + 证书校验，慢，且把"证书问题"误报为"网络不通" |
| **TCP 443 connect**（本工具） | 只验证"这条路径能否到达 GitHub 入口"，毫秒级完成，与最终使用方式（HTTPS）路径一致 |

测速参数：默认 2 秒超时（`config.json` 的 `test_timeout_seconds` 可调，弱网可调到 3 秒）、16 线程并发（几十个 IP 的总耗时 ≈ 最慢的一个）。每次连通成功后会**重复采样一次取最小值**，降低单次 RTT 抖动导致的误判；失败不重试，避免拖长整体耗时。

优选结果还会做一次 **TLS 严格校验**（见 2.7）：对核心域名用优选 IP 真正完成一次 TLS 握手并校验证书与主机名，把「能建连但被劫持/代理接管」的 IP 剔除。每个域名取延迟最低且通过校验的 IP 写入 hosts；全部失败则**不改动 hosts**。

如果远端列表缺少核心域名（或该域名所有候选都不可达），程序会为 `github.com` 等核心域名补充少量**内置兜底候选**参与测速（仍要连通且通过 TLS 校验才会写入）；启用结果会明确列出哪些核心域名没有写入 hosts，避免「显示已启用却没加速」的困惑。

### 2.4 线程模型：后台计算，主线程绘制

tkinter 不是线程安全的——子线程直接操作控件会崩溃或产生竞态。本项目的线程规则：

```
操作发起（窗口按钮 / 托盘菜单 / 定时线程）
        │
        ▼
ui.actions.run_action ── 抢 op_lock（操作互斥）── 按钮进入"处理中…"
        │
        ▼
daemon 后台线程执行 core.service 的业务函数
        │  （只写状态 state + 发布事件，绝不碰控件）
        ▼
finally：释放锁 + 广播 op_done
        │
        ▼
root.after(0, …) 调度回 tkinter 主线程 → 全量刷新界面
```

- `op_lock`：全局操作互斥锁，保证同一时刻只有一个业务操作在跑（窗口、托盘、定时线程三方共用）；
- `root.after(0, callback)`：跨线程更新 UI 的**唯一通道**，把任务投递到主线程事件队列；
- `op_done` 事件：任意操作结束后主窗口做一次全量刷新——这是"托盘改了状态、窗口没跟上"这类不同步 bug 的兜底机制；
- 定时刷新线程用 `stop_event.wait(秒数)` 而不是 `sleep`：退出时能立刻被唤醒，不用傻等几小时。

### 2.5 发布 / 订阅状态同步

托盘和主窗口展示同一份数据（启用状态、候选列表、测速结果…）。如果两侧各存一份，任何一侧操作后都要"记得"刷新另一侧，漏掉就是 bug。本项目用单一数据源：

- 所有状态存在 `core/app_state.py` 的 `state` 对象里；
- 谁改了数据谁就 `publish()` 广播事件（`enabled` / `candidates_changed` / `connectivity` / `op_done` …）；
- 托盘订阅 `enabled` 直接换图标（pystray 自身线程安全）；主窗口订阅所有事件，经 `root.after` 回主线程刷新。

两侧互不认识，只认识 `state`——低耦合，且想漏发事件都难（写状态必须走 `set_xxx` 方法，赋值和广播绑死）。

### 2.6 镜像加速与 Hosts 加速的区别（下载加速链接生成的原理）

hosts 加速解决的是「域名解析环节」的问题，但对 Release 附件、源码包这类大文件下载帮助有限——这些文件走的是对象存储 / CDN 的另一条链路。「下载加速链接生成」用的是另一种机制：**镜像代理**。

| | Hosts 加速 | 镜像加速（本功能） |
|---|---|---|
| 原理 | 把域名指向更快的 IP，仍然直连 GitHub | 整条链接交给代理服务器，由它代下载后回传 |
| 改动范围 | 写系统 hosts 文件 | 不改任何系统配置，只对单条链接生效 |
| 生效对象 | 网页、clone、API 等所有 GitHub 域名 | 你拼接的那一条下载链接 |
| 需要管理员 | 是 | 否 |

拼接规则：**镜像前缀 + 完整原始链接**。例如原始链接是

```
https://github.com/owner/repo/releases/download/v1.0/setup.zip
```

选择节点 `https://gh-proxy.com/` 后生成的加速链接为

```
https://gh-proxy.com/https://github.com/owner/repo/releases/download/v1.0/setup.zip
```

（追加的原始链接自带的 `https://` 是镜像站的固定调用格式，不是笔误；程序会去掉前缀末尾多余的 `/`，避免出现双斜杠。）

镜像代理不支持裸仓库地址——直接拼接 `https://github.com/user/repo.git` 这类克隆链接会得到 `Invalid input`。因此本功能会先识别链接类型：**克隆链接**（`user/repo.git`）与**仓库主页**（`user/repo`）自动转为源码包直链 `https://github.com/{user}/{repo}/archive/refs/heads/{分支}.zip`（分支默认 `main`，可在窗口中切换 `master`）；含 `/archive/`、`/releases/`、`/raw/`、`/blob/` 路径的**文件直链**原样拼接；其他页面（如 issues）给出提示而不生成无效链接。识别与转换行为均记录在日志中。

镜像站由社区维护，可用性可能随时间变化，节点列表可在 `core/download_helper.py` 的 `MIRROR_PREFIXES` 中自行增删（所有 URL 校验与拼接规则也只在这里定义一处）。

### 2.7 写入安全与候选过滤（v5.0）

系统 hosts 是「改错就会影响全系统解析」的关键文件，v5.0 在写入链路上加了几道保护：

| 机制 | 做法 | 解决的问题 |
|---|---|---|
| 原子写入 | 同目录临时文件 + fsync + `os.replace` | 断电 / 崩溃 / 杀软中断不会写坏 hosts（不会出现空文件或半截内容） |
| 加速段置顶 | 新段写到文件最前面，写后逐域名自检 | Windows 按「先匹配先赢」，避免原 hosts 已有同名条目时加速静默失效 |
| 候选白名单 | 只有 GitHub 相关域名 + 公网 IPv4 才能写入 | 远端数据被投毒或手工误加时，任意域名 / 内网地址无法进入 hosts |
| 备份一致性 | 备份前剔除旧加速段；备份缺失时按标记移除加速段 | 换机残留、备份被清理软件删除后仍能正确停用 |
| TLS 严格校验 | 核心域名用优选 IP 做真实 TLS 握手 + 证书/主机名校验 | 剔除「TCP 能连但 TLS 被劫持或代理接管」的假可用 IP |

被过滤的候选会在主窗口状态栏提示，并记录到 app.log；手动添加候选时对话框会直接给出原因。

### 2.8 数据目录与按需提权（v5.0）

**数据目录**：配置、日志、候选清单、运行时图标统一放在数据目录，与安装位置解耦：

| 场景 | 数据目录 |
|---|---|
| 默认（安装版 / 绿色版） | `%LOCALAPPDATA%\GitHubAccelerator`（当前用户可写，无需管理员） |
| 便携模式 | 程序目录（在 exe / 项目根放一个空的 `portable.flag` 即启用） |

旧版放在程序目录的 `config.json` / `github_hosts.txt` 会在首次运行时自动复制到新目录（只复制、不删除，方便回退旧版本）。把程序放进 Program Files、只读共享盘或 U 盘也能正常工作。

**按需提权**：启动不再要求管理员权限，普通用户即可打开界面、查看状态、管理候选、生成下载链接；只有点「启用加速 / 停用加速 / 强制还原」时才弹出 UAC 提示，并携带 `--do <动作>` 重启自身、自动继续刚才的操作。受限模式（非管理员）下状态栏会显示提示；若加速仍处于启用状态而用户以受限模式退出，程序会明确告警（无法还原 hosts），而不是静默失败。

### 2.9 界面刷新性能（v5.0）

- **事件合并**：一次操作会连续发布 `candidates_changed` / `enabled` / `message` / `op_done` 等多个事件；现在 30ms 窗口内的事件合并成一次最小刷新集合（实测 5 个事件只触发 1 次表格 / 日志刷新）。
- **表格增量刷新**：按 `(IP, 域名)` 做稳定主键，只插入 / 删除 / 更新变化的行，刷新不再丢失选中项与滚动位置，候选上百条时也不卡。
- **日志预览增量**：按文件大小 + mtime 判断日志是否变化，没变化就不重读 512KB 的 app.log。

### 2.10 代理检测与 IPv6（v5.1）

**代理检测**（只读，不修改任何系统设置）：hosts 加速只改域名解析、不转发流量。如果用户所在网络必须经过代理上网，直连 GitHub 的 IP 往往不可达，hosts 加速也就无从生效。程序启动与「测试连通性」时会检测：

- 环境变量 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`（大小写都查）；
- 系统代理（WinINET 注册表 `ProxyEnable` + `ProxyServer`，支持 `http=...;https=...` 写法）；
- 自动配置脚本（PAC，`AutoConfigURL`）。

检测到代理时：启动日志与状态栏给出提示；连通性结果区新增「代理：」一行；若「启用加速」全部候选测速失败，错误信息会说明可能是代理网络导致，并建议改用「下载加速链接」的镜像方案。

**IPv6 支持**：候选清单的每一行仍是 `IP 域名`，第一列同时接受 IPv4 与标准写法的 IPv6（不需要方括号），例如：

```
140.82.113.3 github.com
2606:50c0:8000::153 github.com
```

写入 hosts 时按原样输出 IPv6 地址；测速对 IPv6 目标使用 `AF_INET6` socket；TLS 严格校验同样适用；IP 列排序在 v4 / v6 混排时也正确（IPv4 在前、数值升序）。只接受公网地址，链路本地 / ULA / 回环等地址会被过滤。

## 3. 模块架构说明

```
GitHubAccelerator/
├── main.py                  入口：权限探测 / 单实例 / 按需提权 / 装配 / 退出收尾
├── core/                    业务核心层：不 import 任何界面库，返回结果不弹窗
│   ├── app_state.py         全局状态 + 发布/订阅事件 + 操作互斥锁 + 取消信号
│   ├── config_manager.py    config.json 读取/校验/合并写回（永不抛异常）
│   ├── hosts_manager.py     系统 hosts 读写/备份/还原 + 候选清单文件管理
│   ├── network_tester.py    TCP 443 测速 / TLS 严格校验 / 拉取 IP / 连通性诊断（纯函数）
│   ├── download_helper.py   下载加速链接：镜像前缀、URL 校验、链接识别（纯函数）
│   └── service.py           业务编排：把上面几个模块串成完整操作流程
├── ui/                      界面层：可以 import core/utils，只做"调 core + 展示"
│   ├── actions.py           统一操作调度器：op_lock + 后台线程 + 忙状态 + op_done
│   ├── dialogs.py           通用模态表单对话框（手动添加 / 编辑 IP）
│   ├── download_dialog.py   下载加速链接生成器：镜像前缀拼接 / 复制 / 打开（非模态）
│   ├── fonts.py             界面字体探测与回退（非中文 Windows 不再出现方框）
│   ├── main_window.py       主窗口：状态栏 / 按钮 / 排序表格 / 连通性 / 日志预览
│   └── tray_icon.py         托盘：右键菜单 / 状态换图标 / 气泡通知
├── utils/                   基础设施层：仅标准库（app_icon 另需 PIL）
│   ├── paths.py             程序目录 / 数据目录定位（便携模式 + 旧数据迁移）
│   ├── logger.py            日志初始化（根记录器统一配置，各模块 getLogger 即用）
│   ├── admin.py             管理员检测 / UAC 提权重启（可携带动作）/ 单实例互斥量
│   ├── proxy.py             系统代理检测（只读，不修改系统设置）
│   ├── fileio.py            原子文件写入（临时文件 + fsync + os.replace）
│   ├── dpi.py               高 DPI 感知声明与 Tk 缩放调整
│   └── app_icon.py          代码绘制应用图标 / 生成 icon.png+ico / 任务栏 AppUserModelID
├── pyproject.toml           依赖与 ruff 配置
└── README.md                本文档
```

> 打包配置（`GitHubHostsAccelerator.spec`、`version_info.txt`）、图标文件与 CI 属于本地构建文件，
> 不随仓库发布（已在 .gitignore 中排除）。

> 运行时数据（`config.json` / `github_hosts.txt` / `app.log` / `icon.png`+`icon.ico`）不在仓库里，
> 而是放在 **数据目录**（见 2.8）：默认 `%LOCALAPPDATA%\GitHubAccelerator`；便携模式下为程序目录。

**分层依赖（严格单向，禁止反向）：**

```
main.py  →  ui  →  core  →  utils
```

- `core` 绝不 import tkinter/pystray——所以 core 可以单独做单元测试（不启动界面）；
- core 的函数统一返回结果（`(bool, 消息)`、列表、dict），如何提示用户由 ui 决定；
- 两处循环导入用**依赖注入**解开：`main_window ↔ actions` 用 `actions.init(root, set_busy)`；`core ↔ 托盘` 用 `service.set_notifier(tray.notify)`。

**一次"启用加速"的完整数据流：**

```
按钮/托盘 → run_action → 后台线程 → service.enable:
  update_ip_list_online（可选）→ ensure_backup → load_candidate_pairs
  → network_tester.test_ips_concurrent（并发测速）
  → network_tester.pick_best_from_results（按域名选最低延迟）
  → hosts_manager.write_accel_section（先删旧段再追加）
  → hosts_manager.flush_dns → state.set_enabled(True)
  → publish 事件 → 主线程刷新窗口 / 托盘换绿图标
```

## 4. 使用指南

### 4.1 环境要求与安装依赖

- Windows 10 / 11（修改系统 hosts 需要管理员权限，程序在需要时按需请求提权）；
- Python 3.9+（Pillow 11 的最低要求）；
- 第三方依赖只有两个：`pillow`、`pystray`（版本范围见 `pyproject.toml`）
- 数据目录：默认 `%LOCALAPPDATA%\GitHubAccelerator`（便携模式见 2.8）

### 4.2 运行

```bat
cd GitHubAccelerator
python main.py
```

启动默认以普通用户运行：浏览状态、管理候选、生成下载链接都不需要提权；点「启用 / 停用 / 强制还原」时才会弹出 UAC 并以管理员身份重启自身（脚本模式用 `pythonw.exe` 重启，不弹黑窗），重启后自动继续刚才的操作。

首次运行会在数据目录生成 `config.json` / `github_hosts.txt` / `app.log` 与图标；在程序目录放一个空的 `portable.flag` 可切换为便携模式（数据跟着程序走）。

### 4.3 打包为 exe

```bat
pyinstaller -F -w --name GitHubHostsAccelerator --icon=icon.ico main.py
```

仓库只发布源码与文档，不含打包配置。需要正式构建时可在本地准备
`GitHubHostsAccelerator.spec` / `version_info.txt` / `icon.ico`（均已在 .gitignore 中排除），
再用 `pyinstaller --noconfirm GitHubHostsAccelerator.spec` 生成带版本资源、关闭 UPX 的 exe。
正式分发建议给 exe 做代码签名。

### 4.4 日常操作

- **主窗口**：顶部徽标显示启用状态（绿=已启用）与 hosts 生效条目数；连通性结果区会显示「代理：」检测结果；按钮区完成启停 / 更新 / 测速；长任务执行时按钮区下方出现**进度条**，「取消」按钮可随时中断（测速 / TLS 校验 / 在线更新）；表格支持表头点击排序（再点一次切换升降序）、双击编辑 IP、右键菜单（仅测速此项 / 复制 IP / 删除此项）、多选删除；底部是连通性结果与日志预览；
- **下载加速链接生成**（按钮区右上角「🔗 下载加速链接生成」）：粘贴 GitHub 文件链接（如 `https://github.com/owner/repo/releases/download/v1.0/setup.zip`），选一个加速节点后点「生成链接」，再「一键复制」到下载工具或「在浏览器中打开」直接下载；窗口为**非模态**，打开期间主窗口操作不受影响；切换节点 / 分支会自动重新生成（原理见 2.6 节）；粘贴 `.git` 克隆地址或仓库主页链接时自动转为源码包直链（分支默认 main，可切换 master）；窗口底部「项目主页：GitHubAccelerator」可直接打开本项目仓库；
- **关闭窗口** = 隐藏到托盘（进程不退出）；**双击托盘图标**重新打开；
- **托盘右键菜单**：全部操作均可从托盘完成；
- **退出**：托盘菜单「退出」。加速启用中退出会**自动还原 hosts**；正在执行其他操作时拒绝退出，稍候再试。

## 5. 配置说明

### 5.1 config.json（自动生成，可手动编辑）

> 文件位于**数据目录**：默认 `%LOCALAPPDATA%\GitHubAccelerator\config.json`（便携模式下在程序目录）。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `auto_update_on_enable` | bool | `true` | 启用加速前是否先在线更新 IP 列表（失败自动降级用本地列表） |
| `refresh_interval_hours` | int | `6` | 定时自动刷新间隔（小时，最小 1；仅在加速启用时执行） |
| `test_timeout_seconds` | float | `2.0` | 单次 TCP 测速超时（秒，0.5~10；弱网建议 3.0） |
| `strict_tls_verify` | bool | `true` | 是否对核心域名做严格 TLS 证书校验；代理/特殊网络下可设为 `false` |
| `window_width` / `window_height` / `window_x` / `window_y` | int | 退出时自动保存 | 主窗口尺寸与位置（下次启动恢复） |
| `ui_scale` | float | 退出时自动保存 | 保存窗口几何时的 DPI 缩放系数（内部用于高 DPI 换算，一般无需手改） |

配置文件损坏时程序不会崩溃：整文件非法 → 用默认值且**不覆盖坏文件**（方便发给别人看）；单个字段非法 → 只有该字段回退默认值。

### 5.2 github_hosts.txt（IP 候选清单）

- 每行一条 `IP 域名`，支持 `#` 注释行和空行；第一列可以是 IPv4 **或** IPv6（如 `2606:50c0:8000::153 github.com`）；
- **同一域名写多个 IP 是有意义的**：启用加速时全部参与测速，只保留延迟最低的一个写入 hosts——想换 IP 就多写几行候选；
- 「更新 IP 列表」会把在线数据合并进来（完全相同的条目去重，你手写的注释和不同 IP 候选都保留）；
- 文件缺失时程序自动生成带说明的模板。

### 5.3 app.log（运行日志）

记录所有操作的详细过程（测速逐 IP 结果、写入 hosts 条目、备份还原……）。超过 512KB 自动轮转，保留 2 份历史。主窗口日志预览区按级别着色（INFO 深灰 / WARNING 橙 / ERROR 红）。 逐 IP 测速明细为 DEBUG 级别，INFO 只保留阶段汇总，避免日志被刷满。

## 6. 学习指南（写给第一次读这个项目的人）

建议按下面的顺序读，每个阶段都带着问题去读代码。

### 第一阶段：跑起来，看骨架（半天）

1. **读 `main.py` 的 `main()` 函数**，跟着注释走一遍启动顺序，回答：
   - 为什么日志必须最先初始化？为什么提权在单实例之前？
   - `service.set_notifier(tray.notify)` 这行在干什么？（提示：依赖倒置）
2. 读 `utils/paths.py`（很短）：程序怎么同时支持"脚本运行"和"打包 exe 运行"两种目录定位。

### 第二阶段：数据与文件（1~2 天）

3. **`core/config_manager.py`**——全项目最简单的模块，重点看"永不抛异常"的容错策略（三种坏情况各有什么出路）。
4. **`core/hosts_manager.py`**——核心文件操作，重点看：
   - 模块 docstring：为什么读写 hosts 要用 `surrogateescape` 编码？
   - `ensure_backup`：为什么备份绝不覆盖？
   - `remove_accel_sections`：为什么需要两个正则（完整段 + 未闭合残段）？
5. **`core/network_tester.py`**：为什么选 TCP 443 而不是 ping？`pick_best_from_results` 如何做到"纯函数"（不碰状态、不碰界面）？

### 第三阶段：编排与状态（1~2 天）

6. **`core/app_state.py`**——先通读模块 docstring 的事件表和线程模型，这是全项目的"神经系统"。思考：为什么写状态必须走 `set_xxx` 方法？
7. **`core/service.py`**——业务编排。重点读 `enable()`：fail-fast 结构（写 hosts 为什么必须是最后一步？）；`refresh_scheduler_loop` 的三个跳过条件各防什么事故？

### 第四阶段：界面与线程（2~3 天）

8. **`utils/admin.py`**：UAC 提权的 `--elevated` 防死循环设计；命名互斥量为什么比"遍历进程列表"可靠（WAIT_ABANDONED 接管）。
9. **`ui/actions.py`**——`run_action` 四件事（互斥 / 后台执行 / 忙状态 / op_done 广播），想清楚 `finally` 里为什么先释放锁再广播。
10. **`ui/main_window.py`**——界面最大的一份。先看 `_handle_event`（事件如何变成刷新），再看 `_sort_key`（方向感知排序的技巧），最后看线程注释：`refresh_candidates` 里为什么要 `post_ui`？
11. **`ui/tray_icon.py`** + **`ui/dialogs.py`**：pystray 的线程模型（模块 docstring）；FormDialog 怎么做到"不认识 IP 也能校验"。

### 自测问题（读完应该都能回答）

1. 为什么托盘改了状态，主窗口能自动跟上而不用手动刷新？
2. 启用加速时停电了，hosts 会不会出现"半新半旧"的内容？（提示：先删后加 + 未闭合段清理）
3. 为什么定时刷新前要检查备份存在？不检查会发生什么？
4. 子线程想更新表格，正确路径是什么？为什么不能直接调 `tree.insert`？
5. 用户在操作进行中点了「退出」，程序会怎么表现？

## License

本项目基于 [MIT License](LICENSE) 开源。
