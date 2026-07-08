"""Application config loader."""
import os
from pathlib import Path
from dataclasses import dataclass
from ruamel.yaml import YAML
from .llm.client import LLMConfig


@dataclass
class AppConfig:
    llm: LLMConfig
    characters_dir: str
    data_dir: str
    project_root: str

    def resolve_path(self, *parts: str) -> str:
        """Resolve a path relative to project root."""
        return os.path.join(self.project_root, *parts)


def load_config(config_path: str = "config/config.yaml") -> AppConfig:
    """Load app config from YAML. Falls back to example if missing."""
    yaml = YAML()
    path = Path(config_path)
    if not path.exists():
        example = Path("config/config.example.yaml")
        if example.exists():
            path = example
        else:
            raise FileNotFoundError(f"Config not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.load(f)

    llm_data = data["llm"]
    llm = LLMConfig(
        api_key=llm_data.get("api_key", ""),
        model_id=llm_data.get("model_id", "deepseek-chat"),
        base_url=llm_data.get("base_url", "https://api.deepseek.com/v1"),
        temperature=llm_data.get("temperature", 0.7),
    )
    project_root = os.getcwd()
    return AppConfig(
        llm=llm,
        characters_dir=data.get("characters_dir", "config/characters"),
        data_dir=data.get("data_dir", "data"),
        project_root=project_root,
    )
