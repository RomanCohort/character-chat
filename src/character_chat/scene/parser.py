"""Scene tag parser — extract `<scene:...,phase:...>` from LLM output.

Prompts must constrain LLM to use this format. The parser cleans up
common variants (e.g. spaces, newlines) and returns a clean dict.
"""
import re
from typing import Optional, Tuple

SceneTag = Tuple[str, Optional[str]]  # (scene_name, optional_phase)


# Example matches:
# <scene:回家,phase:夜>
# <scene:客厅,phase:午后>
# IM Talk: 这是一个句子，场景A,phase:黄昏
def parse_scene_tag(text: str) -> SceneTag | None:
    """Extract scene tag from text. Returns (name, phase) or (name, None)."""
    # First try strict tag format (most common constraint)
    strict_match = re.search(r"<\s*scene\s*:\s*([^>,]+)\s*(?:,\s*phase\s*:\s*([^>]+))?\s*>", text, re.IGNORECASE)
    if strict_match:
        return strict_match.group(1).strip(), strict_match.group(2).strip() if strict_match.group(2) else None

    # Fallback: find "scene:..." or "场景:..." anywhere in text
    weak_match = re.search(r"(?:scene|场景)\s*[:：]\s*([^,,\s]+)\s*(?:,\s*phase\s*[:：]\s*([^,\s]+))?", text, re.IGNORECASE)
    if weak_match:
        return weak_match.group(1).strip(), weak_match.group(2).strip() if weak_match.group(2) else None

    return None
