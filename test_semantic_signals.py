import unittest

from audio_quality.audio_quality_processor import TranscriptSegment, deterministic_quality_check
from audio_quality.semantic_signals import (
    LOW_FEE_INTENT,
    TV_BOX_INTENT,
    extract_business_signals,
    summarize_business_signals,
)


class SemanticSignalsTests(unittest.TestCase):
    def test_extracts_lower_fee_intent_and_amount_slot(self):
        segments = [
            TranscriptSegment(0, 2, "客户", "我这个套餐太贵了，想改8元套餐保个号。"),
            TranscriptSegment(3, 5, "客服", "我帮您查询一下套餐情况。"),
        ]
        signals = extract_business_signals(segments)
        self.assertEqual(LOW_FEE_INTENT, signals[0].intent)
        self.assertEqual(8, signals[0].slots["target_amount_yuan"])
        self.assertEqual("mobile_plan", signals[0].slots["business_object"])
        self.assertGreaterEqual(len(signals[0].votes), 3)

    def test_preserves_different_amount_slots(self):
        eight = extract_business_signals([TranscriptSegment(0, 1, "客户", "想改8元套餐")])
        eighteen = extract_business_signals([TranscriptSegment(0, 1, "客户", "想改18元套餐")])
        self.assertEqual(8, eight[0].slots["target_amount_yuan"])
        self.assertEqual(18, eighteen[0].slots["target_amount_yuan"])
        self.assertNotEqual("8元", eighteen[0].slots.get("target_plan"))

    def test_extracts_lowest_plan_without_amount(self):
        signals = extract_business_signals([TranscriptSegment(0, 1, "客户", "我不用流量了，降到最低消费就可以。")])
        self.assertEqual(LOW_FEE_INTENT, signals[0].intent)
        self.assertEqual("最低消费", signals[0].slots["target_plan"])
        self.assertNotIn("target_amount_yuan", signals[0].slots)

    def test_semantic_example_vote_catches_paraphrase(self):
        signals = extract_business_signals([TranscriptSegment(0, 1, "客户", "老人家手机平时只接电话，月租能不能弄低一点。")])
        self.assertEqual(LOW_FEE_INTENT, signals[0].intent)
        sources = {vote["source"] for vote in signals[0].votes}
        self.assertIn("semantic_examples", sources)

    def test_extracts_tv_box_intent_and_slots(self):
        signals = extract_business_signals([
            TranscriptSegment(0, 2, "客户", "机顶盒不用了，每个月扣15块，我想退掉。"),
            TranscriptSegment(3, 5, "客服", "我这边帮你申请优惠一年，您可以继续使用。"),
        ])
        tv_signal = next(signal for signal in signals if signal.intent == TV_BOX_INTENT)
        self.assertEqual("tv_box", tv_signal.slots["business_object"])
        self.assertIn(15, tv_signal.slots["fee_amounts_yuan"])
        self.assertEqual("cancel_or_stop_using", tv_signal.slots["customer_goal"])
        self.assertEqual("discount_or_fee_waiver", tv_signal.slots["retention_offer"])

    def test_no_signal_for_unrelated_query(self):
        signals = extract_business_signals([TranscriptSegment(0, 1, "客户", "我想查询一下今天流量用了多少。")])
        self.assertEqual([], signals)

    def test_summary_includes_slots_and_vote_count(self):
        signals = extract_business_signals([TranscriptSegment(0, 1, "客户", "改成18元低资费套餐")])
        summary = summarize_business_signals(signals)
        self.assertIn("目标金额=18元", summary)
        self.assertIn("投票=", summary)

    def test_tv_box_semantic_context_changes_required_action_gaps(self):
        rules = {
            "employee_speaker_keywords": ["帮你申请", "优惠", "继续使用"],
            "speaker_review_threshold": 0.5,
            "qualified_score": 80,
            "dimensions": {
                "挽留流程": {
                    "weight": 1.0,
                    "base_score": 100,
                    "rule_ids": ["missing_check", "missing_compare", "missing_retention_action"],
                }
            },
            "required_actions": [
                {"id": "missing_check", "patterns": ["查询套餐"], "description": "未查清业务情况", "deduction": 5},
                {"id": "missing_compare", "patterns": ["对比套餐"], "description": "未对比方案", "deduction": 5},
                {"id": "missing_retention_action", "patterns": ["挽留"], "description": "未挽留", "deduction": 5},
            ],
        }
        segments = [
            TranscriptSegment(0, 2, "客户", "移动高清一直没有在使用，每个月扣15块，我想退掉。"),
            TranscriptSegment(3, 6, "客服", "这个是88套餐包含的，我帮你申请优惠一年，您可以继续使用。"),
        ]
        signals = extract_business_signals(segments)
        result = deterministic_quality_check(segments, rules, semantic_context=[signal.to_dict() for signal in signals])
        self.assertEqual([], [issue.rule_id for issue in result.issues])
        self.assertEqual(100, result.score)


if __name__ == "__main__":
    unittest.main()
