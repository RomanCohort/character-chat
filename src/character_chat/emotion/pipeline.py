"""Emotion pipeline - orchestrates the four layers for the CLI.

Single entry point: EmotionPipeline.process(user_text) -> ReconciledEmotion

Layers:
  1. Rule-based keyword/sentiment (EmotionTracker)
  2. Neural classification (NeuralEmotionClassifier)
  3. Reconciliation (EmotionReconciler)
  4. Tag selection (ExpressionTagSelector)
"""
from typing import Optional
from .tracker import EmotionTracker
from .classifier import NeuralEmotionClassifier, NeuralEmotionResult
from .reconciler import EmotionReconciler, ReconciledEmotion
from .tags import ExpressionTagSelector
from .state import EmotionLabel


class EmotionPipeline:
    """End-to-end emotion processing pipeline."""

    def __init__(
        self,
        classifier: Optional[NeuralEmotionClassifier] = None,
        expression_tags: Optional[dict] = None,
        neural_enabled: bool = True,
    ):
        """
        Args:
            classifier: Neural classifier instance. If None and neural_enabled=True,
                       a default one is created (lazy-loaded on first use).
            expression_tags: Dict from character card's expression_tags field.
            neural_enabled: If False, skip neural classification (rule-only mode).
        """
        self.tracker = EmotionTracker()
        self.classifier = classifier
        self.reconciler = EmotionReconciler()
        self.tag_selector = ExpressionTagSelector()
        if expression_tags:
            self.tag_selector.load_character_tags(expression_tags)
        self.neural_enabled = neural_enabled
        if neural_enabled and classifier is None:
            self.classifier = NeuralEmotionClassifier()

    def load_character_tags(self, expression_tags: dict):
        """Load expression tags from a character card."""
        self.tag_selector.load_character_tags(expression_tags)

    def process(self, user_text: str, context: list[str] = None) -> ReconciledEmotion:
        """Process user text through the full pipeline.

        Args:
            user_text: The user's current message.
            context: Recent user messages for neural classifier context.

        Returns:
            ReconciledEmotion with final label, intensity, confidence.
        """
        # === Layer 1: Rule-based ===
        rule_detected = self.tracker.update_from_keyword(user_text, force=True)
        rule_output = None
        if rule_detected is not None:
            rule_output = (rule_detected, self.tracker.state.dominant_intensity)

        # Apply time decay (in case of idle)
        self.tracker.apply_decay()

        # === Layer 2: Neural (if enabled) ===
        neural_output = None
        if self.neural_enabled and self.classifier is not None:
            # Build input: user_text + recent context
            input_text = user_text
            if context:
                input_text = "\n".join(context[-3:] + [user_text])
            try:
                neural_output = self.classifier.classify(input_text)
            except Exception as e:
                # Neural failed (e.g. model not loaded, OOM) -> rule-only
                print(f"[EmotionPipeline] Neural classification failed: {e}")
                neural_output = None

        # === Layer 3: Reconciliation ===
        reconciled = self.reconciler.reconcile(
            rule_output=rule_output,
            neural_output=neural_output,
        )

        return reconciled

    def get_prompt_hint(self) -> str:
        """Get behavior hint string for the current dominant emotion."""
        return self.tracker.get_prompt_hint()

    def get_expression_tags(self, label: EmotionLabel, top_n: int = 2) -> list[str]:
        """Get expression tags for a given emotion label."""
        return self.tag_selector.select_tags(label, top_n=top_n)

    def get_current_state(self) -> dict:
        """Get current emotion state snapshot."""
        return {
            "intensities": dict(self.tracker.state.intensities),
            "dominant": self.tracker.state.dominant.value,
            "dominant_intensity": self.tracker.state.dominant_intensity,
            "top_3": self.tracker.state.top_n(3),
        }
