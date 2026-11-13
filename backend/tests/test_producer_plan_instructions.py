"""Stable milestone Plans are distinct from task approval plans (dev spec §10.11)."""

from pathlib import Path

import pytest

from voxarium_studio.core.prompts import AgentPromptsConfig


@pytest.mark.parametrize("source", ["system_prompt", "producer_skill"])
def test_session_plan_rules_preserve_stable_task_free_milestones(source):
    if source == "system_prompt":
        instructions = AgentPromptsConfig().text("producer_mode", language="English")
    else:
        instructions = (
            Path(__file__).resolve().parents[1]
            / "voxarium_studio/teams/skill_bundle/skills/producer/SKILL.md"
        ).read_text(encoding="utf-8")
    instructions = " ".join(instructions.split())

    assert "Plan free of task IDs and direct links to tasks" in instructions
    assert "describe milestones, not individual board entries" in instructions
    assert "do not substantially rewrite or expand it as tasks change" in instructions
    assert "Before changing its wording, structure or size, double-check" in instructions
    assert "necessary for the user's goal and cannot be covered by existing milestones" in instructions
    assert "make only the smallest necessary change" in instructions
    assert "Routine completion updates do not require rewriting milestones" in instructions
    assert "50 characters" in instructions
    assert "Before assigning any task, check its scope and acceptance criteria against the current stage of the session Plan" in instructions
    assert "Apply this check to new tasks, reused tasks and corrective tasks alike" in instructions
    assert "Do not assign work that deviates from the current stage; narrow or defer it" in instructions
    assert "instead of changing the Plan merely to justify it" in instructions
