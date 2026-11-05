# GitHub Hosts 加速器（GitHubAccelerator）

Windows 桌面小工具：一键加速 GitHub 访问，常驻系统托盘 + 图形主窗口。

## 1. 项目简介

访问 GitHub 网页慢、`git clone` 卡住、头像 / 附件加载不出来——多数情况不是网络断了，而是**域名解析到的 IP 不可达或质量差**。本工具把 GitHub 的常用域名直接指向"实测可达且延迟最低"的 IP，写进系统 hosts 文件，绕过 DNS 环节，从而加速访问。

**主要功能：**

| 功能 | 说明 |
|---|---|
| 启用加速 | 在线更新 IP（可选）→ 备份 hosts → 并发测速 → 每个域名优选最低延迟 IP → 写入 hosts → 刷新 DNS |
| 停用加速 | 用备份还原 hosts，恢复原状 |
| 强制还原 | 不依赖内部状态，只要备份存在就把原始 hosts 盖回去（hosts 被弄乱时的"救命"按钮） |
| 更新 IP 列表 | 从 GitHub520 稳定地址（`https://raw.hellogithub.com/hosts`）拉取最新 IP，与本地合并去重 |
| 全部 / 选中测速 | TCP 443 并发测速，延迟显示在表格中，可按列排序 |
| 连通性测试 | 检测 `github.com` / `raw.githubusercontent.com` 当前生效 IP 是否可达 |
| 定时自动刷新 | 每 N 小时（可配置）静默执行一次完整刷新（仅加速启用时） |
| 托盘常驻 | 关闭窗口只是隐藏到托盘；托盘菜单可完成全部操作；退出前自动还原 hosts |
| 下载加速链接生成 | 用镜像代理前缀拼接 Release 文件 / 源码包的加速下载链接，一键复制或直接打开（不改 hosts、不改系统配置） |

**运行要求：** Windows 10/11，管理员权限（修改系统 hosts 必需），全系统单实例运行。

## 2. 实现原理

### 2.1 为什么改 hosts 能加速 GitHub

访问一个域名的流程是：**DNS 解析（域名 → IP）→ 与该 IP 建立连接 → 传输数据**。GitHub 慢的瓶颈通常在第一步：DNS 得到的 IP 被污染、或 CDN 把用户调度到了绕路的远端节点。

hosts 文件是操作系统级的手工 DNS 映射表，**优先级高于 DNS 查询**。把域名直接指向一个实测可达、延迟低的 IP，就跳过了"解析"这个不可控环节——连接质量完全由你指定的 IP 决定。

本工具写入的加速段长这样（只动这一段，其他内容不碰）：

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

测速参数：2 秒超时、16 线程并发（几十个 IP 的总耗时 ≈ 最慢的一个）。每个域名若有多个候选 IP，取延迟最低且连通的一个写入 hosts；全部失败则**不改动 hosts**。

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

镜像站由社区维护，可用性可能随时间变化，节点列表可在 `ui/download_dialog.py` 的 `MIRROR_NODES` 中自行增删。

## 3. 模块架构说明

```
GitHubAccelerator/
├── main.py                  入口：提权 / 单实例 / 配置注入 / 装配 / 退出收尾
├── config.json              用户配置（自动生成与保存）
├── github_hosts.txt         IP 候选清单（可手动编辑）
├── app.log                  运行日志（512KB 轮转，保留 2 份）
├── core/                    业务核心层：不 import 任何界面库，返回结果不弹窗
│   ├── app_state.py         全局状态 + 发布/订阅事件 + 操作互斥锁
│   ├── config_manager.py    config.json 读取/校验/合并写回（永不抛异常）
│   ├── hosts_manager.py     系统 hosts 读写/备份/还原 + 候选清单文件管理
│   ├── network_tester.py    TCP 443 并发测速 / 在线拉取 IP / 连通性诊断（纯函数）
│   └── service.py           业务编排：把上面几个模块串成完整操作流程
├── ui/                      界面层：可以 import core/utils，只做"调 core + 展示"
│   ├── actions.py           统一操作调度器：op_lock + 后台线程 + 忙状态 + op_done
│   ├── dialogs.py           通用模态表单对话框（手动添加 / 编辑 IP）
│   ├── download_dialog.py   下载加速链接生成器：镜像前缀拼接 / 复制 / 打开（非模态）
│   ├── main_window.py       主窗口：状态栏 / 按钮 / 排序表格 / 连通性 / 日志预览
│   └── tray_icon.py         托盘：右键菜单 / 状态换图标 / 气泡通知
├── utils/                   基础设施层：仅标准库（app_icon 另需 PIL）
│   ├── paths.py             程序目录定位（脚本 / 打包 exe 双模式）
│   ├── logger.py            日志初始化（根记录器统一配置，各模块 getLogger 即用）
│   ├── admin.py             管理员检测 / UAC 提权重启 / 单实例互斥量
│   └── app_icon.py          代码绘制应用图标 / 生成 icon.png+ico / 任务栏 AppUserModelID
└── docs/README.md           本文档
```

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

- Windows 10 / 11（需要管理员权限修改系统 hosts）；
- Python 3.8+；
- 第三方依赖只有两个：`pip install pillow pystray`

### 4.2 运行

```bat
cd GitHubAccelerator
python main.py
```

非管理员身份运行时会自动弹出 UAC 请求提权并重启自身（脚本模式用 `pythonw.exe` 重启，不弹黑窗）。

### 4.3 打包为 exe

```bat
pyinstaller -F -w --name GitHubHostsAccelerator --icon=icon.ico main.py
```

`-F` 单文件、`-w` 不显示控制台。`icon.ico` 首次运行时由程序自动生成到程序目录（也可自己替换图标文件）。打包后的 exe 同样放在任意目录运行即可，配置 / 候选清单 / 日志都生成在 exe 旁边。

### 4.4 日常操作

- **主窗口**：顶部徽标显示启用状态（绿=已启用）与 hosts 生效条目数；按钮区完成启停 / 更新 / 测速；表格支持表头点击排序（再点一次切换升降序）、双击编辑 IP、右键菜单（仅测速此项 / 复制 IP / 删除此项）、多选删除；底部是连通性结果与日志预览；
- **下载加速链接生成**（按钮区右上角「🔗 下载加速链接生成」）：粘贴 GitHub 文件链接（如 `https://github.com/owner/repo/releases/download/v1.0/setup.zip`），选一个加速节点后点「生成链接」，再「一键复制」到下载工具或「在浏览器中打开」直接下载；窗口为**非模态**，打开期间主窗口操作不受影响；切换节点 / 分支会自动重新生成（原理见 2.6 节）；粘贴 `.git` 克隆地址或仓库主页链接时自动转为源码包直链（分支默认 main，可切换 master）；
- **关闭窗口** = 隐藏到托盘（进程不退出）；**双击托盘图标**重新打开；
- **托盘右键菜单**：全部操作均可从托盘完成；
- **退出**：托盘菜单「退出」。加速启用中退出会**自动还原 hosts**；正在执行其他操作时拒绝退出，稍候再试。

## 5. 配置说明

### 5.1 config.json（自动生成，可手动编辑）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `auto_update_on_enable` | bool | `true` | 启用加速前是否先在线更新 IP 列表（失败自动降级用本地列表） |
| `refresh_interval_hours` | int | `6` | 定时自动刷新间隔（小时，最小 1；仅在加速启用时执行） |
| `window_width` / `window_height` / `window_x` / `window_y` | int | 退出时自动保存 | 主窗口尺寸与位置（下次启动恢复） |

配置文件损坏时程序不会崩溃：整文件非法 → 用默认值且**不覆盖坏文件**（方便发给别人看）；单个字段非法 → 只有该字段回退默认值。

### 5.2 github_hosts.txt（IP 候选清单）

- 每行一条 `IP 域名`，支持 `#` 注释行和空行；
- **同一域名写多个 IP 是有意义的**：启用加速时全部参与测速，只保留延迟最低的一个写入 hosts——想换 IP 就多写几行候选；
- 「更新 IP 列表」会把在线数据合并进来（完全相同的条目去重，你手写的注释和不同 IP 候选都保留）；
- 文件缺失时程序自动生成带说明的模板。

### 5.3 app.log（运行日志）

记录所有操作的详细过程（测速逐 IP 结果、写入 hosts 条目、备份还原……）。超过 512KB 自动轮转，保留 2 份历史。主窗口日志预览区按级别着色（INFO 深灰 / WARNING 橙 / ERROR 红）。

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
