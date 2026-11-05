# -*- coding: utf-8 -*-
"""应用图标：用 Pillow 代码生成 GitHub 风格图标（不依赖外部图片文件）。

为什么用代码画图标？
  - 免去"部署时记得带一张 icon.png"的负担，仓库本身就是完整资源；
  - 程序首次运行时把图标落盘为 icon.png / icon.ico（仅缺失时创建），
    icon.ico 供 pyinstaller --icon=icon.ico 打包 exe 使用。

图标应用在三个地方（同一套绘制，风格完全统一）：
  1. 主窗口左上角 / 默认任务栏：root.iconphoto(True, ImageTk.PhotoImage(...))
  2. 任务栏分组识别：SetCurrentProcessExplicitAppUserModelID（本模块提供）
  3. 系统托盘：pystray.Icon(image=...)

为什么放在 utils 而不是 ui？
  主窗口和托盘都要用同一份图标绘制，放任何一侧都会造成另一侧跨界引用；
  图标属于"应用资源"，放 utils 最合适（PIL 是图像库不是界面库，不违反分层）。
"""

import ctypes
import logging
import os

from PIL import Image, ImageDraw, ImageFont

from utils import paths

logger = logging.getLogger(__name__)

# 图标文件名（生成到程序目录）
ICON_PNG_FILENAME = 'icon.png'
ICON_ICO_FILENAME = 'icon.ico'
# 任务栏分组识别用 AppUserModelID（让任务栏把本程序当成独立应用而不是 python.exe）
ICON_APP_ID = 'mycompany.github_accelerator'

# 状态点配色：绿色=加速启用，灰色=未启用
COLOR_ON = (34, 197, 94, 255)
COLOR_OFF = (130, 135, 140, 255)


def _load_font(size):
    """加载系统 TrueType 字体绘制图标字母（Segoe UI -> Arial 依次尝试）。

    入参：size —— 字号（像素）。
    返回值：ImageFont 对象；全部失败时回退 Pillow 内置位图字体，
             保证图标在任何环境都能画出来。
    """
    for name in ('segoeuib.ttf', 'segoeui.ttf', 'arialbd.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make_app_icon(variant='neutral'):
    """绘制 GitHub 风格应用图标：深色圆角方块 + 白色字母 G + 右下角状态圆点。

    入参：variant —— 'on'=启用（绿色状态点）/ 'off'=未启用（灰色状态点）/
           'neutral'=不带状态点（中性图标）。
    返回值：PIL.Image（RGBA，64x64）。
    """
    size = 64
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 深色圆角方块背景（GitHub 深色主题色）+ 浅色描边
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=14,
                           fill=(36, 41, 46, 255),
                           outline=(139, 148, 158, 255), width=1)
    # 居中白色字母 G（anchor='mm' 表示以坐标为中心绘制）
    draw.text((size // 2, size // 2 - 3), 'G', font=_load_font(34),
              fill=(255, 255, 255, 255), anchor='mm')
    # 右下角状态圆点（启用绿 / 未启用灰），白色描边增强对比
    if variant in ('on', 'off'):
        color = COLOR_ON if variant == 'on' else COLOR_OFF
        draw.ellipse((41, 41, 59, 59), fill=color,
                     outline=(255, 255, 255, 255), width=2)
    return image


def ensure_icon_files():
    """生成 icon.png / icon.ico 到程序目录（仅缺失时创建，已存在不覆盖）。

    入参：无。
    返回值：无；生成失败仅记日志，不影响程序运行
             （iconphoto / 托盘用的是内存中的图像对象，不依赖这两个文件）。
    """
    png_path = os.path.join(paths.get_app_dir(), ICON_PNG_FILENAME)
    ico_path = os.path.join(paths.get_app_dir(), ICON_ICO_FILENAME)
    try:
        if not os.path.exists(png_path):
            make_app_icon('on').save(png_path)
            logger.info('已生成图标文件：%s', png_path)
        if not os.path.exists(ico_path):
            # ico 内含多尺寸帧（16/32/48/64），资源管理器和任务栏各取所需
            make_app_icon('on').save(ico_path, sizes=[(16, 16), (32, 32),
                                                      (48, 48), (64, 64)])
            logger.info('已生成图标文件：%s', ico_path)
    except Exception as error:
        logger.warning('生成图标文件失败（不影响运行）：%s', error)


def set_app_user_model_id():
    """设置进程级 AppUserModelID，让 Windows 任务栏正确关联并显示程序图标。

    入参：无。返回值：无。

    背景：不设置的话，脚本模式运行时任务栏把窗口归到 python.exe 名下，
    图标显示为 Python 图标；设置后任务栏用我们注册的 ID 关联独立图标。
    """
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [
            ctypes.c_void_p]
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ICON_APP_ID)
    except Exception:
        logger.exception('设置 AppUserModelID 失败（任务栏图标可能不准确）')
