"""堆场资料：危险品箱区、垛位、吊机与类别隔离表。

本模块只描述相对静态的堆场基础资料（纸质平面图的数字化版本），
供建表种子数据与落位规则共同引用，不包含任何数据库或HTTP逻辑。
"""
from typing import Dict, List, Set

# 危险品类别（按IMDG危险货物分类简化为9大类）
DG_CLASS_NAMES: Dict[str, str] = {
    "1": "爆炸品",
    "2": "气体",
    "3": "易燃液体",
    "4": "易燃固体",
    "5": "氧化性物质",
    "6": "毒性及感染性物质",
    "7": "放射性物质",
    "8": "腐蚀性物质",
    "9": "杂类危险物质",
}
DG_CLASSES: List[str] = list(DG_CLASS_NAMES.keys())

# 只能与同类同垛的类别（爆炸品、放射性物质不得与任何其他类别混垛）
SOLO_CLASSES: Set[str] = {"1", "7"}

# 不相容类别对（双向）：氧化剂远离易燃气体/液体/固体
INCOMPATIBLE_PAIRS: Set[frozenset] = {
    frozenset(("2", "5")),
    frozenset(("3", "5")),
    frozenset(("4", "5")),
}

# 到港班次：代码 -> (名称, 开始小时, 结束小时)
SHIFTS: Dict[str, tuple] = {
    "night": ("夜班", 0, 8),
    "early": ("早班", 8, 16),
    "middle": ("中班", 16, 24),
}

# 箱区资料：最大层数与每垛最大总重量（吨）均为箱区限制
# stacks 为该箱区下的垛位（堆场平面图上的具体垛位编号）
DEFAULT_ZONES: List[Dict[str, object]] = [
    {
        "code": "A",
        "name": "爆炸/放射品专管箱区",
        "max_layers": 2,
        "max_stack_weight_t": 50.0,
        "stacks": ["A-01", "A-02", "A-03"],
    },
    {
        "code": "B",
        "name": "普通危化品箱区",
        "max_layers": 3,
        "max_stack_weight_t": 65.0,
        "stacks": ["B-01", "B-02", "B-03", "B-04"],
    },
    {
        "code": "C",
        "name": "重型危化品箱区",
        "max_layers": 4,
        "max_stack_weight_t": 80.0,
        "stacks": ["C-01", "C-02", "C-03", "C-04"],
    },
]

# 吊机资料
DEFAULT_CRANES: List[Dict[str, str]] = [
    {"code": "QC-01", "name": "1号岸桥"},
    {"code": "QC-02", "name": "2号岸桥"},
]
