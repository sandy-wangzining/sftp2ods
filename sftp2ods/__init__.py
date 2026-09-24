# -*- coding: utf-8 -*-
"""sftp2ods —— 通用 SFTP 按天文件（CSV/TSV）→ MaxCompute ODS（列展开宽表 + pt 分区）

使用方式：
    python -m sftp2ods --job jobs/xxx.json ...     # 源码目录直接跑
    sftp2ods --job jobs/xxx.json ...               # pip/pipx 安装后（console script）
"""

VERSION = "1.0.0"
