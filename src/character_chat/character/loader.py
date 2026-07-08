"""Character card loader — scans /config/characters/*.yaml."""
import os
from pathlib import Path
from typing import Dict, Optional
from ruamel.yaml import YAML
from .schema import CharacterCard


class CharacterLoader:
    """Load and cache character cards from a directory."""

    def __init__(self, characters_dir: str | os.PathLike):
        self.characters_dir = Path(characters_dir)
        self._yaml = YAML()
        self._cache: Dict[str, CharacterCard] = {}

    def load_all(self) -> Dict[str, CharacterCard]:
        """Load all .yaml/.yml character cards from the directory."""
        if not self.characters_dir.exists():
            return {}
        cards = {}
        for ext in ("*.yaml", "*.yml"):
            for path in sorted(self.characters_dir.glob(ext)):
                try:
                    card = self._load_file(path)
                    cards[card.bot_name] = card
                except Exception as e:
                    print(f"[warn] Failed to load {path.name}: {e}")
        self._cache = cards
        return cards

    def _load_file(self, path: Path) -> CharacterCard:
        with open(path, "r", encoding="utf-8") as f:
            data = self._yaml.load(f)
        return CharacterCard.model_validate(data)

    def get(self, name: str) -> Optional[CharacterCard]:
        """Get a cached character card by name. Call load_all() first."""
        return self._cache.get(name)

    def list_names(self) -> list[str]:
        """List available character names."""
        return list(self._cache.keys())
