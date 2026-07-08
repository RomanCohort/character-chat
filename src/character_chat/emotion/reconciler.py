"""Emotion reconciler (Layer 3) - combines rule-based and neural outputs.

Parallel Layers:
  - Layer 1 (rules): keyword/read sentiment
  - Layer 2 (neural): pre-trained model classification

Returns: ReconciledEmotion with confidence score and dominance.
"""
from typing import Dict, Optional
from .state import EmotionLabel, NEURAL_TO_RULE_MAP
from .classifier import NeuralEmotionResult


class ReconciledEmotion:
    """Unified emotion result after reconciliation."""

    def __init__(
        self,
        rule_output: Optional[tuple[EmotionLabel, float]] = None,  # (label, confidence)
        neural_output: Optional[tuple[str, float]] = None,  # (label, confidence)
        reconciled_label: EmotionLabel = EmotionLabel.NEUTRAL,
        reconciled_intensity: float = 0.0,
        confidence: float = 0.0,
        reasoning: str = "",
    ):
        self.rule_output = rule_output
        self.neural_output = neural_output
        self.reconciled_label = reconciled_label
        self.reconciled_intensity = reconciled_intensity
        self.confidence = confidence
        self.reasoning = reasoning

    def to_dict(self) -> dict:
        return {
            "reconciled_label": self.reconciled_label.value,
            "reconciled_intensity": self.reconciled_intensity,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
        }


class EmotionReconciler:
    """Reconciler for Layer 1 + Layer 2 emotion signals."""

    def __init__(
        self,
        neural_weight_decay: float = 0.2,  # how much neural input decays per interaction
        neural_certainty_threshold: float = 0.5,
    ):
        self.neural_weight_decay = neural_weight_decay
        self.neural_certainty_threshold = neural_certainty_threshold
        self._rule_emotion_counts: Dict[EmotionLabel, float] = {}  # accumulated intensity

    def reconcile(
        self,
        rule_output: Optional[tuple[EmotionLabel, float]] = None,
        rule_turn: int = 0,
        neural_output: Optional[NeuralEmotionResult] = None,
        neural_turn: int = 0,
    ) -> ReconciledEmotion:
        """Reconcile rule-based and neural emotion outputs.

        Strategy:
          - If rule_output exists and neural_output is None: trust rules
          - If neural_output exists and neural confidence >= threshold:
              - Convert neural label to EmotionLabel
              - If rules agree with neural: take weighted average
              - If conflict: trust neural
          - If neural confidence < threshold: fallback to rules
        """
        # Case 0: neither rule nor neural -> neutral
        if rule_output is None and neural_output is None:
            return ReconciledEmotion(
                reconciled_label=EmotionLabel.NEUTRAL,
                reconciled_intensity=0.0,
                confidence=0.0,
                reasoning="No emotion signal from rules or neural.",
            )

        # Case 1: Only rule output -> trust rules
        if rule_output and neural_output is None:
            label, intensity = rule_output
            return ReconciledEmotion(
                reconciled_label=label,
                reconciled_intensity=intensity,
                confidence=1.0,
                reasoning="Rely on rule-based emotion scan (no neural confidence).",
            )

        # Case 2: Only neural output
        if neural_output and rule_output is None:
            if neural_output.confidence >= self.neural_certainty_threshold:
                label_str = neural_output.top_emotion()
                label = NEURAL_TO_RULE_MAP.get(label_str, EmotionLabel.NEUTRAL)
                return ReconciledEmotion(
                    reconciled_label=label,
                    reconciled_intensity=neural_output.confidence,
                    confidence=neural_output.confidence,
                    reasoning=f"Neural model confidence={neural_output.confidence:.2f}.",
                )
            else:
                # Low confidence neural -> treated as neutral fallback
                return ReconciledEmotion(
                    reconciled_label=EmotionLabel.NEUTRAL,
                    reconciled_intensity=0.0,
                    confidence=0.2,
                    reasoning=f"Neural confidence too low ({neural_output.confidence:.2f} < {self.neural_certainty_threshold}), treated as unknown.",
                )

        # Case 3: Both rule and neural -> reconcile
        rule_label, rule_intensity = rule_output
        neural_label_str = neural_output.top_emotion()
        neural_label = NEURAL_TO_RULE_MAP.get(neural_label_str, EmotionLabel.NEUTRAL)
        neural_confidence = neural_output.confidence

        # Check for agreement
        if rule_label == neural_label:
            # Medium confidence if neural is uncertain
            if neural_confidence >= self.neural_certainty_threshold:
                # High agreement -> high confidence
                confidence = (rule_intensity + neural_confidence) / 2
                return ReconciledEmotion(
                    reconciled_label=rule_label,
                    reconciled_intensity=confidence,
                    confidence=confidence,
                    reasoning=f"Rule and neural agree on {rule_label.value} (rule={rule_intensity:.2f}, neural_conf={neural_confidence:.2f}).",
                )
            else:
                # Agreement but neural uncertain -> moderate confidence
                confidence = (rule_intensity + neural_confidence) / 2
                return ReconciledEmotion(
                    reconciled_label=rule_label,
                    reconciled_intensity=confidence,
                    confidence=confidence,
                    reasoning=f"Rule agrees with neural ({rule_label.value}), but neural is uncertain (conf={neural_confidence:.2f}).",
                )
        else:
            # Conflict: trust neural if confident, else fallback to rule
            if neural_confidence >= self.neural_certainty_threshold:
                return ReconciledEmotion(
                    reconciled_label=neural_label,
                    reconciled_intensity=neural_confidence,
                    confidence=neural_confidence,
                    reasoning=f"Conflict: neural ({neural_label_str}) more confident than rule ({rule_label.value}). Neural confidence={neural_confidence:.2f}.",
                )
            else:
                # Neural uncertain, fall back to rule (avoid flip-flopping)
                rule_decay = min(rule_intensity * 0.2, 0.5)  # decay rule intensity over time
                new_intensity = rule_intensity - rule_decay
                return ReconciledEmotion(
                    reconciled_label=rule_label,
                    reconciled_intensity=max(new_intensity, 0.0),
                    confidence=rule_decay,
                    reasoning=f"Neural uncertain ({neural_confidence:.2f}), fallback to Rule {rule_label.value} (decayed from {rule_intensity:.2f}).",
                )
