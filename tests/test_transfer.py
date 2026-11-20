"""迁移判定测试：三属性门槛、跨厂关系、不确定性系数。"""
from changeover.transfer import GATE_ATTRIBUTES, judge_transfer
from tests._base import AppCase


class TransferTest(AppCase):
    def test_direct_sample(self):
        v = judge_transfer(self.pack, ("A", "B"), ("一厂", "L0"),
                           ("A", "B"), ("一厂", "L1"))
        self.assertTrue(v.usable)
        self.assertEqual(v.relation, "direct")
        self.assertEqual(v.uncertainty_factor, 1.0)

    def test_cross_family_allowed_when_three_gates_match(self):
        # A→B 与 X→Y：甲乙材料都是甲、普通洁净、左→右方向一致
        v = judge_transfer(self.pack, ("A", "B"), ("一厂", "L0"),
                           ("X", "Y"), ("一厂", "L1"))
        self.assertTrue(v.usable)
        self.assertEqual(v.relation, "cross_family")
        self.assertGreater(v.uncertainty_factor, 1.0)

    def test_cleanliness_change_blocks(self):
        # A→B 后批 B(普通) 迁到 A→C 后批 C(洁净)，且材料、方向也变
        v = judge_transfer(self.pack, ("A", "C"), ("一厂", "L0"),
                           ("A", "B"), ("一厂", "L1"))
        self.assertFalse(v.usable)
        self.assertEqual(v.relation, "blocked")
        joined = " ".join(v.reasons)
        self.assertIn("洁净等级", joined)
        self.assertIn("材料", joined)
        self.assertIn("模具方向", joined)

    def test_die_direction_change_blocks(self):
        # B(右)→A(左) 目标 vs A(左)→B(右) 来源：前批方向相反
        v = judge_transfer(self.pack, ("B", "A"), ("一厂", "L0"),
                           ("A", "B"), ("一厂", "L1"))
        self.assertFalse(v.usable)
        self.assertTrue(any("模具方向" in r for r in v.reasons))

    def test_gate_attributes_complete(self):
        self.assertEqual(set(GATE_ATTRIBUTES),
                         {"chemistry", "cleanliness", "die_direction"})

    def test_cross_factory_requires_interchangeable(self):
        # 一厂与二厂在档案中互为可替代
        v = judge_transfer(self.pack, ("A", "B"), ("一厂", "L0"),
                           ("A", "B"), ("二厂", "L0"))
        # 二厂 L0 不在 factory_caps（仅一厂 L0/L1、三厂 L2）→ 阻断并说明
        if not v.usable:
            self.assertTrue(any("产能档案" in r or "工具" in r
                                for r in v.reasons))
        else:
            self.assertEqual(v.relation, "cross_factory")

    def test_three_factory_not_interchangeable(self):
        # 三厂 interchangeable_factory=null，不能与一厂互借
        v = judge_transfer(self.pack, ("A", "B"), ("一厂", "L0"),
                           ("A", "B"), ("三厂", "L2"))
        self.assertFalse(v.usable)
        self.assertTrue(any("可替代厂" in r for r in v.reasons))
