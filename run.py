"""PyInstaller 打包入口。"""
import os
import sys
from pathlib import Path


def unblock_bundled_dlls():
    """去掉打包目录内 DLL 的「来自网络」标记（Zone.Identifier 备用数据流）。

    从浏览器下载的 zip 经资源管理器解压后，每个文件都带 ZoneId=3 标记，
    .NET Framework 拒绝加载这类托管程序集，pythonnet 加载 Python.Runtime.dll 失败，
    主窗口无法创建（只剩托盘）。须在 pywebview 初始化 pythonnet 之前清除。
    """
    root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for dll in root.rglob("*.dll"):
        try:
            os.remove(f"{dll}:Zone.Identifier")
        except OSError:  # 无标记、非 NTFS 或目录只读
            pass


if __name__ == "__main__":
    if getattr(sys, "frozen", False):
        unblock_bundled_dlls()
    from app.main import run
    run()
