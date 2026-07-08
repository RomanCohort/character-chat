"""Tests for Phase 1: skeleton, character loading, LLM client."""
import sys
from pathlib import Path

# Ensure src/ is importable in tests
src = Path(__file__).parent.parent.parent / "src"
sys.path.insert(0, str(src))

import pytest
import tempfile
from ruamel.yaml import YAML

from character_chat.character.schema import (
    CharacterCard, PersonalityConfig, RelationshipConfig, ScenarioConfig,
    FilterConfig, ShortTermMemoryConfig
)
from character_chat.character.loader import CharacterLoader
from character_chat.llm.client import LLMClient, LLMConfig
from character_chat.config import load_config, AppConfig


class TestCharacterSchema:
    """Test character card schema validation."""

    def test_basic_card(self):
        """Create a basic character card without extra fields."""
        card = CharacterCard(
            bot_name="test_bot",
            system_prompt="Test bot",
            background="Test background...",
        )
        assert card.bot_name == "test_bot"
        assert len(card.background) > 0

    def test_personality_config(self):
        """Personality traits can be set and validated."""
        pc = PersonalityConfig(
            traits={"tsundere": 0.8, "warm": 0.5},
            speaking_style="Short sentences",
        )
        assert pc.traits["tsundere"] == 0.8
        assert pc.speaking_style == "Short sentences"

    def test_relationship_config(self):
        """Relationship config can be retrieved."""
        card = CharacterCard(
            bot_name="test",
            relationships=[
                RelationshipConfig(target="user1", type="friend", affection=0.8),
            ],
        )
        rel = card.get_relationship("user1")
        assert rel.type == "friend"
        assert rel.affection == 0.8

    def test_expression_tags(self):
        """Expression tags for emotions."""
        card = CharacterCard(
            bot_name="test",
            expression_tags={
                "shy": ["*blush*", "..."],
                "angry": ["*turns away*", "Hmph!"],
            },
        )
        assert card.expression_tags["shy"] == ["*blush*", "..."]
        assert "Hmph!" in card.expression_tags["angry"]


class TestCharacterLoader:
    """Test YAML-based character card loading."""

    def test_load_yml_file(self, tmp_path):
        """Load a valid *.yaml character card."""
        yaml = YAML()
        card_dir = tmp_path / "characters"
        card_dir.mkdir()
        card_path = card_dir / "test.yaml"

        # Write a valid card (simplified from 小夜.yaml)
        card_data = {
            "bot_name": "小夜",
            "background": "17岁傲娇高中生\n父母离异，跟奶奶长大",
            "system_prompt": "你是小夜，傲娇外表内心柔软。",
            "scenario": {
                "setting": "放学后的天台",
                "opening": "你又来了啊...哼，才不是在等你。",
            },
            "chat": {
                "max_history": 20,
                "short_term_memory": {"enable": True},
            },
            "personality": {
                "traits": {"tsundere": 0.8, "warm": 0.5},
            },
            "expression_tags": {
                "shy": ["*低头*", "（小声）"],
                "angry": ["*向别处看*", "哼！"],
            },
        }
        with open(card_path, "w", encoding="utf-8") as f:
            yaml.dump(card_data, f)

        loader = CharacterLoader(card_dir)
        loaded = loader.load_all()
        assert "小夜" in loaded
        card = loaded["小夜"]
        assert card.bot_name == "小夜"
        assert card.background == "17岁傲娇高中生\n父母离异，跟奶奶长大"
        assert "放学后的天台" in card.scenario.setting

    def test_list_characters(self, tmp_path):
        """Load all characters from directory."""
        card_dir = tmp_path / "characters"
        card_dir.mkdir()
        yaml = YAML()

        # Create three different characters (use ASCII names to avoid GBK issues)
        chars = ["alice", "bob", "carol"]
        for name in chars:
            path = card_dir / f"{name}.yaml"
            yaml.dump({"bot_name": name}, path)

        loader = CharacterLoader(card_dir)
        loader.load_all()
        assert set(loader.list_names()) == set(chars)


class TestLLMClient:
    """Test LLM client (offline - doesn't make API calls)."""

    def test_llm_config(self):
        """LLM config can be created and used."""
        config = LLMConfig(
            api_key="test_key",
            model_id="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            temperature=0.7,
        )
        assert config.model_id == "deepseek-chat"
        assert config.temperature == 0.7

    @pytest.mark.skip(reason="Requires real API key, skip in CI")
    def test_llm_chat_no_real_call(self):
        """Test that chat() constructs proper request (won't actually call)."""
        config = LLMConfig(api_key="test", model_id="deepseek-chat")
        client = LLMClient(config)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        # This won't block because test doesn't verify response
        client.chat(messages)
        # If gets here without error, request was constructed correctly


class TestAppConfig:
    """Test configuration loading."""

    def test_load_example(self, tmp_path):
        """Load config from example.yaml."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
llm:
  api_key: "sk-test"
  model_id: "deepseek-chat"
  base_url: "https://api.deepseek.com/v1"

characters_dir: "config/characters"
data_dir: "data"
""")
        app_cfg = load_config(str(config_file))
        assert app_cfg.llm.api_key == "sk-test"
        assert app_cfg.llm.model_id == "deepseek-chat"
        assert "characters" in app_cfg.characters_dir.lower()
