"""堆场资料：箱区/吊机目录与危险品隔离矩阵（主数据，调整规则只改这里）。

类别采用简化的 IMDG 危规分类；不相容关系为对称矩阵，
爆炸品（1.x）按分项隔离，其余类别通过不相容对声明。
"""
from typing import FrozenSet, List

# 危险品类别（IMDG 风格，已按原型需要裁剪）
HAZARD_CLASSES: List[str] = [
    "1.1", "1.4", "2.1", "2.2", "3", "4.1", "4.2", "4.3",
    "5.1", "5.2", "6.1", "6.2", "7", "8", "9",
]

CLASS_LABELS = {
    "1.1": "爆炸品（整体爆炸）",
    "1.4": "爆炸品（无显著危险）",
    "2.1": "易燃气体",
    "2.2": "非易燃无毒气体",
    "3": "易燃液体",
    "4.1": "易燃固体",
    "4.2": "易于自燃物质",
    "4.3": "遇水放出易燃气体物质",
    "5.1": "氧化性物质",
    "5.2": "有机过氧化物",
    "6.1": "毒性物质",
    "6.2": "感染性物质",
    "7": "放射性物质",
    "8": "腐蚀品",
    "9": "杂类危险物质",
}

_EXPLOSIVE_PREFIX = "1."

# 非爆炸品之间的不相容类别对（对称，无序）
_INCOMPATIBLE_PAIRS: FrozenSet[FrozenSet[str]] = {
    frozenset(pair)
    for pair in [
        ("2.1", "3"), ("2.1", "4.2"), ("2.1", "5.1"), ("2.1", "5.2"),
        ("3", "4.2"), ("3", "4.3"), ("3", "5.1"), ("3", "5.2"),
        ("4.1", "5.1"), ("4.1", "5.2"),
        ("4.2", "5.1"), ("4.2", "5.2"),
        ("4.3", "5.1"), ("4.3", "8"),
        ("5.1", "8"), ("5.2", "8"),
        ("6.2", "7"), ("7", "8"),
    ]
}


def incompatible(class_a: str, class_b: str) -> bool:
    """两个类别能否同垛：相同类别可以；爆炸品只与同分项同垛。"""
    if class_a == class_b:
        return False
    if class_a.startswith(_EXPLOSIVE_PREFIX) or class_b.startswith(_EXPLOSIVE_PREFIX):
        return True
    return frozenset((class_a, class_b)) in _INCOMPATIBLE_PAIRS


# 箱区目录：单垛限重（吨）、单垛限层、垛位数
BLOCK_CATALOG = [
    {"code": "A01", "name": "危险品甲区", "max_stack_weight_t": 30.0, "max_layers": 2, "stack_count": 4},
    {"code": "A02", "name": "危险品乙区", "max_stack_weight_t": 26.0, "max_layers": 2, "stack_count": 4},
    {"code": "B01", "name": "危险品丙区", "max_stack_weight_t": 40.0, "max_layers": 3, "stack_count": 6},
    {"code": "C01", "name": "爆炸品专区", "max_stack_weight_t": 24.0, "max_layers": 1, "stack_count": 3},
]

# 岸桥（吊机）目录
CRANE_CATALOG = [
    {"code": "QC-01", "name": "1号岸桥"},
    {"code": "QC-02", "name": "2号岸桥"},
]

BLOCK_CODES = [item["code"] for item in BLOCK_CATALOG]
CRANE_CODES = [item["code"] for item in CRANE_CATALOG]
