# -*- coding: utf-8 -*-
"""启动 HandWeChat 消息网关。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.__main__ import main


if __name__ == "__main__":
    main()
