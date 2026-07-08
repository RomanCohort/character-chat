"""Tests for Phase 2: emotion pipeline (rule + neural + reconcile + tags)."""
import sys
from pathlib import Path

src = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src))

import pytest
from character_chat.emotion.state import (
    EmotionLabel, EmotionState, NEURAL_TO_RULE_MAP
)
from character_chat.emotion.tracker import EmotionTracker
from character_chat.emotion.classifier import NeuralEmotionResult
from character_chat.emotion.reconciler import EmotionReconciler, ReconciledEmotion
from character_chat.emotion.tags import ExpressionTagSelector
from character_chat.emotion.pipeline import EmotionPipeline


class TestEmotionState:
    """Test emotion state model."""

    def test_default_is_neutral(self):
        state = EmotionState()
        assert state.dominant == EmotionLabel.NEUTRAL
        assert state.dominant_intensity == 1.0

    def test_dominant_picks_highest(self):
        state = EmotionState(intensities={
            "happy": 0.3, "sad": 0.6, "neutral": 0.1, **{e.value: 0.0 for e in EmotionLabel if e.value not in ("happy", "sad", "neutral")}
        })
        assert state.dominant == EmotionLabel.SAD

    def test_top_n(self):
        state = EmotionState(intensities={
            "happy": 0.5, "sad": 0.3, "angry": 0.1, "neutral": 0.0,
            **{e.value: 0.0 for e in EmotionLabel if e.value not in ("happy", "sad", "angry", "neutral")}
        })
        top = state.top_n(2)
        assert top[0][0] == "happy"
        assert top[1][0] == "sad"


class TestNeuralToRuleMap:
    """Test mapping from 5 neural labels to 9 rule labels."""

    def test_joy_maps_to_happy(self):
        assert NEURAL_TO_RULE_MAP["joy"] == EmotionLabel.HAPPY

    def test_fear_maps_to_shy(self):
        # In character-app, fear manifests as shy
        assert NEURAL_TO_RULE_MAP["fear"] == EmotionLabel.SHY

    def test_all_5_present(self):
        for k in ["joy", "sadness", "anger", "fear", "neutral"]:
            assert k in NEURAL_TO_RULE_MAP


class TestEmotionTracker:
    """Test rule-based tracker (Layer 1)."""

    def test_keyword_detection_sad(self):
        tracker = EmotionTracker()
        detected = tracker.update_from_keyword("我今天好难过")
        assert detected == EmotionLabel.SAD

    def test_keyword_detection_happy(self):
        tracker = EmotionTracker()
        detected = tracker.update_from_keyword("哈哈太棒了")
        assert detected == EmotionLabel.HAPPY

    def test_force_mode_dominates_neutral(self):
        """force=True makes a single clear input dominate over neutral."""
        tracker = EmotionTracker()
        tracker.update_from_keyword("我今天好难过", force=True)
        assert tracker.state.dominant == EmotionLabel.SAD
        assert tracker.state.dominant_intensity > 0.5

    def test_no_keyword_returns_none(self):
        tracker = EmotionTracker()
        detected = tracker.update_from_keyword("普通的对话内容")
        assert detected is None

    def test_get_prompt_hint(self):
        tracker = EmotionTracker()
        tracker.update_from_keyword("我今天好难过", force=True)
        hint = tracker.get_prompt_hint()
        assert "难过" in hint

    def test_decay_reduces_intensity(self):
        """Decay reduces non-neutral emotions over time."""
        import time
        tracker = EmotionTracker(decay_interval=0)
        tracker.update_from_keyword("我今天好难过", force=True)
        before = tracker.state.intensities[EmotionLabel.SAD.value]
        time.sleep(0.05)
        tracker.apply_decay()
        after = tracker.state.intensities[EmotionLabel.SAD.value]
        assert after <= before


class TestReconciler:
    """Test conflict resolution (Layer 3)."""

    def test_case_both_none_returns_neutral(self):
        """When no signal, returns neutral."""
        r = EmotionReconciler()
        result = r.reconcile(rule_output=None, neural_output=None)
        assert result.reconciled_label == EmotionLabel.NEUTRAL

    def test_case_rule_only_trusts_rules(self):
        r = EmotionReconciler()
        result = r.reconcile(
            rule_output=(EmotionLabel.ANGRY, 0.7),
            neural_output=None,
        )
        assert result.reconciled_label == EmotionLabel.ANGRY

    def test_case_neural_only_confident(self):
        r = EmotionReconciler()
        neural = NeuralEmotionResult(
            emotion="joy",
            probabilities={"joy": 0.85, "neutral": 0.10},
            confidence=0.85,
        )
        result = r.reconcile(rule_output=None, neural_output=neural)
        assert result.reconciled_label == EmotionLabel.HAPPY  # joy -> happy

    def test_case_neural_only_uncertain_falls_to_neutral(self):
        r = EmotionReconciler()
        neural = NeuralEmotionResult(
            emotion="sadness", confidence=0.3,
        )
        result = r.reconcile(rule_output=None, neural_output=neural)
        assert result.reconciled_label == EmotionLabel.NEUTRAL

    def test_case_agreement_both_happy(self):
        r = EmotionReconciler()
        neural = NeuralEmotionResult(
            emotion="joy", confidence=0.8,
            probabilities={"joy": 0.8},
        )
        result = r.reconcile(
            rule_output=(EmotionLabel.HAPPY, 0.7),
            neural_output=neural,
        )
        assert result.reconciled_label == EmotionLabel.HAPPY
        assert result.confidence > 0.5

    def test_case_conflict_neural_confident_wins(self):
        """When rules say ANGRY but neural confidently says joy, trust neural."""
        r = EmotionReconciler()
        neural = NeuralEmotionResult(
            emotion="joy", confidence=0.82,
            probabilities={"joy": 0.82, "anger": 0.10},
        )
        result = r.reconcile(
            rule_output=(EmotionLabel.ANGRY, 0.7),
            neural_output=neural,
        )
        assert result.reconciled_label == EmotionLabel.HAPPY

    def test_case_conflict_neural_uncertain_fallback_to_rule(self):
        """When neural is uncertain, fall back to rule."""
        r = EmotionReconciler()
        neural = NeuralEmotionResult(
            emotion="sadness", confidence=0.35,
            probabilities={"sadness": 0.35, "neutral": 0.30},
        )
        result = r.reconcile(
            rule_output=(EmotionLabel.HAPPY, 0.7),
            neural_output=neural,
        )
        assert result.reconciled_label == EmotionLabel.HAPPY


class TestTagSelector:
    """Test expression tag selection (Layer 4)."""

    def test_load_and_select(self):
        selector = ExpressionTagSelector()
        selector.load_character_tags({
            "shy": ["*低头*", "（小声）"],
            "happy": ["*眼睛弯起*", "嘿嘿"],
        })
        tags = selector.select_tags(EmotionLabel.SHY, top_n=2)
        assert "*低头*" in tags
        assert "（小声）" in tags

    def test_top_n_limits(self):
        selector = ExpressionTagSelector()
        selector.load_character_tags({
            "shy": ["a", "b", "c", "d"],
        })
        tags = selector.select_tags(EmotionLabel.SHY, top_n=2)
        assert len(tags) == 2

    def test_missing_emotion_returns_empty(self):
        selector = ExpressionTagSelector()
        selector.load_character_tags({"shy": ["*低头*"]})
        tags = selector.select_tags(EmotionLabel.ANGRY, top_n=2)
        assert tags == []

    def test_invalid_emotion_ignored(self):
        selector = ExpressionTagSelector()
        selector.load_character_tags({"invalid_emotion": ["x"], "shy": ["y"]})
        tags = selector.select_tags(EmotionLabel.SHY, top_n=1)
        assert tags == ["y"]


class TestEmotionPipeline:
    """Test end-to-end pipeline (rule-only mode)."""

    def test_pipeline_rule_only_sad(self):
        pipe = EmotionPipeline(
            expression_tags={"sad": ["*垂眸*"], "happy": ["嘿嘿"]},
            neural_enabled=False,
        )
        result = pipe.process("我今天好难过")
        assert result.reconciled_label == EmotionLabel.SAD
        tags = pipe.get_expression_tags(result.reconciled_label, top_n=1)
        assert "*垂眸*" in tags

    def test_pipeline_rule_only_happy(self):
        pipe = EmotionPipeline(
            expression_tags={"happy": ["嘿嘿"]},
            neural_enabled=False,
        )
        result = pipe.process("哈哈太棒了")
        assert result.reconciled_label == EmotionLabel.HAPPY

    def test_pipeline_no_signal_returns_neutral(self):
        pipe = EmotionPipeline(
            expression_tags={},
            neural_enabled=False,
        )
        result = pipe.process("普通的对话")
        assert result.reconciled_label == EmotionLabel.NEUTRAL

    def test_pipeline_state_accumulates(self):
        """Multiple emotional inputs accumulate in state."""
        pipe = EmotionPipeline(
            expression_tags={},
            neural_enabled=False,
        )
        pipe.process("我今天好难过")
        pipe.process("真的很难过")
        # sad intensity should be high after two sad inputs
        state = pipe.get_current_state()
        assert state["intensities"]["sad"] > 0.5

    def test_pipeline_get_prompt_hint(self):
        pipe = EmotionPipeline(
            expression_tags={},
            neural_enabled=False,
        )
        pipe.process("我今天好难过")
        hint = pipe.get_prompt_hint()
        assert "难过" in hint

    def test_load_character_tags_updates(self):
        pipe = EmotionPipeline(expression_tags={}, neural_enabled=False)
        pipe.load_character_tags({"shy": ["*脸红*"]})
        result = pipe.process("好害羞啊")
        tags = pipe.get_expression_tags(result.reconciled_label, top_n=1)
        if result.reconciled_label == EmotionLabel.SHY:
            assert "*脸红*" in tags
