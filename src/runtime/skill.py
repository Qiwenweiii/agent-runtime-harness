import json
from dataclasses import dataclass
from pathlib import Path

SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass
class SkillConfig:
    name: str
    system_prompt: str
    allowed_tools: list[str]
    plan_mode: bool


def load_skill(skill_name: str) -> SkillConfig:
    path = SKILLS_DIR / f"{skill_name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Skill 不存在: {path}")
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return SkillConfig(
        name=data["name"],
        system_prompt=data["system_prompt"],
        allowed_tools=data["allowed_tools"],
        plan_mode=data.get("plan_mode", False),
    )
