#!/usr/bin/env python3
"""命令行启动器：python3 -I run_cli.py <子命令> ...

使用 -I 隔离模式时脚本所在目录仍会进入 sys.path，因此可以在忽略
PYTHONPATH/用户站点包的情况下运行本仓库代码。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from changeover.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
