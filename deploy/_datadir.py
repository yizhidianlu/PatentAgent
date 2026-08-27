# -*- coding: utf-8 -*-
"""打印应用配置里的数据目录（DATA_DIR），供 update.ps1 / backup.ps1 定位备份目标。

为什么是一个独立文件而不是 `python -c "..."`：
  * Windows PowerShell 5.1 向原生程序传参时会吞掉内嵌的双引号，
    `r"C:\\path"` 到了 Python 手里变成 `rC:\\path`，直接 SyntaxError；
  * 换成单引号也不行——Windows 路径里的 `\\U`、`\\x` 会被当成 Unicode 转义。
两条路都堵死，所以把代码放进文件，PowerShell 只传文件名。

备份的目标必须与应用实际使用的库同源。DATA_DIR 可能来自环境变量或 backend/.env，
还涉及引号、相对路径、大小写等细节；与其在 PowerShell 里复刻一份必然有偏差的解析，
不如让应用自己回答。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_config  # noqa: E402

print(get_config().data_dir)
