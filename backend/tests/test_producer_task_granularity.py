"""Producer planning guidance discourages excessive decomposition (dev spec §10.11)."""
from pathlib import Path
import re

import pytest

from voxarium_studio.core.prompts import AgentPromptsConfig


@pytest.fixture(params=["system_prompt", "producer_skill"])
def guidance(request):
    if request.param == "system_prompt":
        text = AgentPromptsConfig().text("producer_mode", language="English")
    else:
        text = (
            Path(__file__).resolve().parents[1]
            / "voxarium_studio/teams/skill_bundle/skills/producer/SKILL.md"
        ).read_text(encoding="utf-8")
    # Markdown emphasis and wrapping must not change the policy's meaning.
    return re.sub(r"\s+", " ", text.replace("**", "")).lower()


def test_each_session_plan_item_prefers_few_cohesive_tasks(guidance):
    assert "for each session plan item, prefer a small number of cohesive board tasks" in guidance


def test_large_task_counts_are_permitted_but_strongly_discouraged_without_a_cap(guidance):
    assert "a large number of tasks is permitted, but strongly discouraged" in guidance
    assert "there is no fixed numerical cap" in guidance


def test_each_additional_task_requires_increasing_justification(guidance):
    assert (
        "the more tasks an item already has, the stronger the justification required "
        "for each additional task"
    ) in guidance


def test_reuse_and_consolidation_precede_indispensable_new_tasks(guidance):
    assert "before adding a task, first try to reuse or consolidate existing tasks" in guidance
    assert "add one only when essential work cannot be covered that way" in guidance
    assert "explain why it is indispensable" in guidance


def test_all_tasks_count_and_plan_rewrites_cannot_evade_restraint(guidance):
    assert "count new, reused and corrective tasks together, including completed tasks" in guidance
    assert "do not split or rewrite plan items merely to evade this increasing restraint" in guidance


@pytest.mark.parametrize("obsolete_instruction", [
    "use no more than 3 board tasks by default",
    "only in cases of extreme necessity may an item have 4 or 5 tasks",
    "never exceed 5 tasks per item",
    "explain why the exception is indispensable",
    "do not split or rewrite plan items merely to bypass this limit",
])
def test_old_numerical_budgets_and_ceiling_are_not_reintroduced(guidance, obsolete_instruction):
    assert obsolete_instruction not in guidance


@pytest.mark.parametrize("obsolete_instruction", [
    "many small, concrete tasks",
    "default to the smallest independently reviewable outcomes",
    "create enough tasks to make progress and review visible",
    "do not bundle independent work into a few broad briefs",
    "not a limit on board tasks",
    "as many small tasks as the goal needs",
    "if new work is discovered, add granular drafts",
])
def test_unbounded_decomposition_instructions_are_not_reintroduced(guidance, obsolete_instruction):
    assert obsolete_instruction not in guidance
