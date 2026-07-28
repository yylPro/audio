import unittest

from audio_quality.audio_quality_processor import TranscriptSegment
from audio_quality.semantic_signals import LOW_FEE_INTENT, extract_business_signals, summarize_business_signals


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

    def test_no_signal_for_unrelated_query(self):
        signals = extract_business_signals([TranscriptSegment(0, 1, "客户", "我想查询一下今天流量用了多少。")])
        self.assertEqual([], signals)

    def test_summary_includes_slots_and_vote_count(self):
        signals = extract_business_signals([TranscriptSegment(0, 1, "客户", "改成18元低资费套餐")])
        summary = summarize_business_signals(signals)
        self.assertIn("目标金额=18元", summary)
        self.assertIn("投票=", summary)


if __name__ == "__main__":
    unittest.main()
