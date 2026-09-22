# -*- coding: utf-8 -*-
"""补做保守合并的 9 组收藏夹。"""
from pathlib import Path

import merge_first_batch as merger


merger.STATE_FILE = Path(__file__).resolve().parent / "data" / "merge_third_batch_state.json"
merger.MERGES = [
    ("视觉创作", "摄影", ["设计", "绘画"]),
    ("数理科学", "数学", ["物理", "化学", "材料科学", "流体力学"]),
    ("科技科普", "科普", ["科技", "技术", "航天"]),
    ("硬件开发", "硬件开发", ["Arduino"]),
    ("仿真与控制", "仿真", ["控制", "MATLAB"]),
    ("健康健身", "健康", ["健身"]),
    ("影视动漫解说", "影视解说", ["动漫解说"]),
    ("游戏内容", "游戏资讯", ["游戏攻略"]),
    ("音乐舞蹈", "音乐", ["舞蹈"]),
]


if __name__ == "__main__":
    raise SystemExit(merger.main())
