"""Neural emotion classifier (Layer 2) - using pre-trained models.

Uses HuggingFace transformers pipeline with emotional_distilroberta model.
Returns 5-label emotion classification: joy, sadness, anger, fear, neutral.
"""
from typing import Dict, Optional
import torch
from transformers import pipeline, AutoModelForSequenceClassification, AutoTokenizer


class NeuralEmotionResult:
    """Result from neural emotion classifier."""

    def __init__(
        self,
        emotion: str,  # joy/sadness/anger/fear/neutral
        probabilities: Optional[Dict[str, float]] = None,
        confidence: float = 0.0,
    ):
        self.emotion = emotion
        self.probabilities = probabilities or {}
        self.confidence = confidence

    def top_emotion(self) -> str:
        """Return top emotion with highest probability."""
        if self.probabilities:
            return max(self.probabilities.keys(), key=lambda k: self.probabilities[k])
        return self.emotion

    def to_dict(self) -> dict:
        return {
            "emotion": self.emotion,
            "probabilities": self.probabilities,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NeuralEmotionResult":
        return cls(
            emotion=data["emotion"],
            probabilities=data.get("probabilities"),
            confidence=data.get("confidence", 0.0),
        )


class NeuralEmotionClassifier:
    """Neural emotion classifier using pre-trained model.

    Uses 'j-hartmann/emotion-english-distilroberta-base' by default.
    for Chinese, could use 'cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual'.
    """

    # Model name (English default, will extend for Chinese later)
    _MODEL_NAME = "j-hartmann/emotion-english-distilroberta-base"

    def __init__(self, model_name: str | None = None):
        self._model_name = model_name or self._MODEL_NAME
        self._pipeline: Optional[pipeline] = None
        self._device = 0 if torch.cuda.is_available() else -1

    def _ensure_loaded(self):
        """Ensure model pipeline is loaded (lazy)."""
        if self._pipeline is None:
            self._pipeline = pipeline(
                "text-classification",
                model=self._model_name,
                topk=5,  # return top 5 labels
                tokenizer=self._model_name,
                device=self._device,
            )
            print(f"[NeuralEmotionClassifier] Loaded {self._model_name}")

    def classify_batch(self, texts: list[str]) -> list[NeuralEmotionResult]:
        """Classify multiple texts in batch."""
        self._ensure_loaded()
        if not texts:
            return []

        results_data = self._pipeline(texts)
        results = []
        for item in results_data:
            # item is a dict: [{"label": "joy", "score": 0.95}, ...]
            probs = {r["label"]: float(r["score"]) for r in item}
            top = max(probs.keys(), key=lambda k: probs[k])
            results.append(NeuralEmotionResult(
                emotion=top,
                probabilities=probs,
                confidence=probs[top],
            ))
        return results

    def classify(self, text: str) -> NeuralEmotionResult:
        """Classify single text."""
        results = self.classify_batch([text])
        return results[0] if results else NeuralEmotionResult(emotion="neutral")

    @classmethod
    def available_models(cls) -> list[str]:
        """Return list of available model names (placeholder)."""
        return [cls._MODEL_NAME]
