# -*- coding: utf-8 -*-
"""第二批收藏夹合并，复用第一批的安全执行逻辑。"""
from pathlib import Path

import merge_first_batch as merger


merger.STATE_FILE = Path(__file__).resolve().parent / "data" / "merge_second_batch_state.json"
merger.MERGES = [
    ("历史与纪实", "历史纪实", ["纪录片"]),
    ("时政观察", "中国观察", ["环球视角", "香港台湾", "政治"]),
]


if __name__ == "__main__":
    raise SystemExit(merger.main())
