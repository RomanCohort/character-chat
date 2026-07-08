"""Emotion subpackage."""
from .state import EmotionLabel, EmotionState, NEURAL_TO_RULE_MAP
from .tracker import EmotionTracker
from .classifier import NeuralEmotionClassifier, NeuralEmotionResult
from .reconciler import EmotionReconciler, ReconciledEmotion
from .tags import ExpressionTagSelector
from .pipeline import EmotionPipeline

__all__ = [
    "EmotionLabel", "EmotionState", "NEURAL_TO_RULE_MAP",
    "EmotionTracker",
    "NeuralEmotionClassifier", "NeuralEmotionResult",
    "EmotionReconciler", "ReconciledEmotion",
    "ExpressionTagSelector",
    "EmotionPipeline",
]
