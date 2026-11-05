# -*- coding: utf-8 -*-
"""路径工具：统一解析程序运行目录。

为什么要单独一个模块？
  - config.json / github_hosts.txt / app.log / icon.png 都要放到"程序所在目录"，
    如果每个模块自己拼路径，打包成 exe 后目录解析稍有不同就会各写各的地方；
  - 集中在这里，全项目只有一处目录逻辑。

本模块属于项目最底层（utils 层）：只依赖标准库，不依赖项目内任何其他模块，
也不引入任何界面库 —— 这样上层（core / ui / main）都可以放心导入它。
"""

import os
import sys


def get_app_dir():
    """获取程序所在目录（数据文件的基准目录）。

    功能：返回程序运行时应读写数据文件的目录。

    入参：无。
    返回值：str —— 绝对路径目录。

    为什么区分两种情况：
      - 直接运行 .py：数据放在"项目根目录"（本文件位于 项目根/utils/ 下，
        所以要向上两级）；这样源码目录下就是完整的可运行项目；
      - PyInstaller 打包成 exe：数据放在 exe 所在目录。注意绝不能用
        sys._MEIPASS（那是 exe 运行时解包出来的临时目录，每次运行都不同），
        否则用户保存的配置 / 候选列表在程序升级后会"丢失"。
    """
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后：sys.executable 指向 exe 本身
        return os.path.dirname(os.path.abspath(sys.executable))
    # 脚本模式：__file__ 是 utils/paths.py，向上两级才是项目根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
