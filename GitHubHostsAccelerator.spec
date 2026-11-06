# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（单文件、无控制台）。

体积优化说明（v4.1）：
  旧配置把 numpy（源码 28.5MB）等完全未使用的科学计算库打进了 exe，
  产物 26.4MB。这里通过 excludes 显式排除，预计可降到 14~17MB。
  需要新增排除项时，加到 excludes 列表即可。

版本信息说明：
  version_info.txt 提供文件版本 / 产品名 / 版权，资源管理器属性页不再为空；
  它同时是代码签名与 SmartScreen 申诉的基础信息。
"""

# 项目从未 import 这些库，但被某些依赖的 hook 或环境里的旧构建带进了产物
EXCLUDES = [
    'numpy',
    'scipy',
    'pandas',
    'matplotlib',
    'pytest',
    'IPython',
    'jupyter',
    'sphinx',
    'docutils',
    'tkinter.test',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='GitHubHostsAccelerator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX 压缩会增加杀毒软件误报率、拖慢首次启动，收益有限 —— 关闭
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
    version='version_info.txt',
)
