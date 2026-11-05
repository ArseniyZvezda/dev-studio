"""Project authoring through the Studio MCP server (§10.7).

Uses the same stores, plugin registry and event bus as the pages. No runner or roster
mutation is exposed here: saving a skill does not assign it to a member.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..automations.schema import Automation, PluginAction, RunPipelineAction
from ..automations.store import AutomationStore
from ..engine import dag
from ..engine.store import PipelineNotFound, PipelineStore, slugify
from ..plugins.registry import Registry
from ..schemas.pipeline import PipelineDefinition
from ..ws.bus import EventBus
from ..ws.events import EventType
from .project_skills import Skill, SkillChangedOnDisk, SkillStore, check_name


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineId(Arguments):
    pipeline_id: str


class PipelineBody(Arguments):
    pipeline: PipelineDefinition


class AutomationId(Arguments):
    automation_id: str


class NewAutomation(Automation):
    enabled: bool = Field(default=False, description="True starts listening immediately.")


class AutomationCreate(Arguments):
    automation: NewAutomation


class ExistingAutomation(Automation):
    id: str
    enabled: bool


class AutomationWrite(Arguments):
    # Unlike create, a replacement must identify the existing entry explicitly.
    automation: ExistingAutomation


class SkillName(Arguments):
    name: str


class SkillCreate(SkillName):
    content: str = Field(description="Complete SKILL.md, including YAML frontmatter.")


class SkillWrite(SkillCreate):
    expected_updated_at: str = Field(description="updated_at returned by read_skill.")


def _skill_json(store: SkillStore, skill: Skill) -> dict[str, Any]:
    return {
        "name": skill.name,
        "path": store.relative(skill.directory),
        "description": skill.description,
        "warning": skill.warning,
        "source": skill.source,
        "display_name": skill.display_name or skill.name,
        "scope": skill.scope,
        "backends": list(skill.backends),
        "readonly": skill.readonly,
        "automatic": skill.automatic,
        "files": list(skill.files),
        "updated_at": skill.updated_at,
    }


@dataclass
class ProjectAuthoring:
    pipelines: PipelineStore
    automations: AutomationStore
    skills: SkillStore
    registry: Callable[[], Registry]
    bus: EventBus

    def pipeline_result(self, pipeline: PipelineDefinition) -> dict[str, Any]:
        issues = dag.validate(pipeline, self.registry())
        return {
            "pipeline": pipeline.model_dump(mode="json", by_alias=True),
            "valid": not any(issue.level == "error" for issue in issues),
            "issues": [
                {"level": i.level, "message": i.message, "node_id": i.node_id}
                for i in issues
            ],
        }

    def save_pipeline(self, pipeline: PipelineDefinition, *, create: bool) -> dict[str, Any]:
        # Store filenames are slugs. Refuse aliases rather than overwrite another id's file.
        if pipeline.id != slugify(pipeline.id):
            raise ValueError("Pipeline id must be a nonempty lowercase slug (letters, digits, - or _)")
        exists = self.pipelines.exists(pipeline.id)
        if create and exists:
            raise ValueError(f"Pipeline {pipeline.id!r} already exists; read it before write_pipeline")
        if not create and not exists:
            raise PipelineNotFound(pipeline.id)
        if exists and self.pipelines.get(pipeline.id).id != pipeline.id:
            raise ValueError("Pipeline filename belongs to a different id")
        result = self.pipeline_result(pipeline)
        if not result["valid"]:
            raise ValueError(json.dumps({"issues": result["issues"]}, ensure_ascii=False))
        self.pipelines.save(pipeline)
        return result

    def automation_problem(self, automation: Automation) -> str | None:
        action = automation.action
        if isinstance(action, RunPipelineAction) and not self.pipelines.exists(action.pipeline_id):
            return f"No pipeline {action.pipeline_id!r}"
        if isinstance(action, PluginAction):
            if self.registry().automation_action(action.plugin_id, action.action_id) is None:
                return f"Plugin {action.plugin_id!r} offers no action {action.action_id!r}"
        return None

    async def save_automation(self, automation: Automation, *, create: bool) -> dict[str, Any]:
        loaded = self.automations.load()
        if loaded.error:
            raise ValueError(loaded.error)
        if create:
            ids = {a.id for a in loaded.automations} | {a.id for a in loaded.invalid}
            if automation.id in ids:
                raise ValueError(f"Automation {automation.id!r} already exists")
        else:
            self.automations.get(automation.id)
        problem = self.automation_problem(automation)
        if problem:
            raise ValueError(problem)
        self.automations.upsert(automation)
        await self.bus.publish(EventType.AUTOMATION_CHANGED, {})
        return automation.model_dump(mode="json")

    def check_skill_target(self, name: str) -> None:
        """Keep MCP writes inside project skills, including through symlinks/junctions."""
        if self.skills.is_readonly(name):
            raise PermissionError(f"Skill {name!r} is maintained outside the project's skills directory")
        check_name(name)
        directory = self.skills.directory.resolve()
        target = (directory / name / "SKILL.md").resolve()
        if not target.is_relative_to(directory):
            raise ValueError("Skill target must stay inside the project's skills directory")

    def tools(self) -> list[Any]:
        from claude_agent_sdk import tool

        definitions: list[Any] = []

        def register(name: str, description: str, model: type[BaseModel], handler: Any) -> None:
            async def call(args: dict[str, Any]) -> dict[str, Any]:
                try:
                    payload = model.model_validate(args)
                    result = await handler(payload)
                    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
                except (ValueError, KeyError, OSError, SkillChangedOnDisk, yaml.YAMLError) as exc:
                    return {
                        "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                        "is_error": True,
                    }

            definitions.append(tool(name, description, model.model_json_schema())(call))

        async def list_node_types(_: Arguments) -> Any:
            return [schema.model_dump(mode="json") for schema in self.registry().node_types()]

        async def list_pipelines(_: Arguments) -> Any:
            return [
                {"id": p.id, "name": p.name, "description": p.description,
                 "node_count": len(p.nodes), "edge_count": len(p.edges)}
                for p in self.pipelines.list()
            ]

        async def read_pipeline(args: PipelineId) -> Any:
            return self.pipeline_result(self.pipelines.get(args.pipeline_id))

        async def validate_pipeline(args: PipelineBody) -> Any:
            return self.pipeline_result(args.pipeline)

        async def create_pipeline(args: PipelineBody) -> Any:
            return self.save_pipeline(args.pipeline, create=True)

        async def write_pipeline(args: PipelineBody) -> Any:
            return self.save_pipeline(args.pipeline, create=False)

        async def list_automations(_: Arguments) -> Any:
            loaded = self.automations.load()
            return {
                "error": loaded.error,
                "automations": [
                    {**a.model_dump(mode="json"), "problem": self.automation_problem(a)}
                    for a in loaded.automations
                ],
                "invalid": [{"index": a.index, "id": a.id, "error": a.error} for a in loaded.invalid],
                "plugin_actions": [
                    {"plugin_id": a.plugin_id, "action_id": a.id,
                     "label": a.label, "description": a.description}
                    for a in self.registry().automation_actions()
                ],
            }

        async def read_automation(args: AutomationId) -> Any:
            automation = self.automations.get(args.automation_id)
            return {**automation.model_dump(mode="json"), "problem": self.automation_problem(automation)}

        async def create_automation(args: AutomationCreate) -> Any:
            return await self.save_automation(args.automation, create=True)

        async def write_automation(args: AutomationWrite) -> Any:
            return await self.save_automation(args.automation, create=False)

        async def list_skills(_: Arguments) -> Any:
            skills, invalid = self.skills.scan()
            return {
                "skills": [_skill_json(self.skills, skill) for skill in skills],
                "invalid": [{"name": entry.name, "error": entry.error} for entry in invalid],
            }

        async def read_skill(args: SkillName) -> Any:
            skill, content, _ = self.skills.read_text(args.name)
            return {**_skill_json(self.skills, skill), "content": content}

        async def create_skill(args: SkillCreate) -> Any:
            self.check_skill_target(args.name)
            skill = self.skills.create(args.name, content=args.content)
            return _skill_json(self.skills, skill)

        async def write_skill(args: SkillWrite) -> Any:
            self.check_skill_target(args.name)
            skill = self.skills.write(
                args.name, args.content, expected_updated_at=args.expected_updated_at,
            )
            return _skill_json(self.skills, skill)

        register(
            "list_node_types", "List available pipeline node types with input/output ports, "
            "defaults and parameter schemas. Use before designing a pipeline.",
            Arguments, list_node_types,
        )
        register(
            "list_pipelines", "List the managed project's pipelines and their ids.",
            Arguments, list_pipelines,
        )
        register(
            "read_pipeline", "Read a complete pipeline and its current validation issues "
            "before editing it.", PipelineId, read_pipeline,
        )
        register(
            "validate_pipeline", "Check a pipeline against the live plugin registry without "
            "saving or running it. Inspect valid and issues.", PipelineBody, validate_pipeline,
        )
        register(
            "create_pipeline", "Create a new pipeline. Refuses existing ids and invalid graphs. "
            "Saves without starting a run.", PipelineBody, create_pipeline,
        )
        register(
            "write_pipeline", "Replace an existing pipeline in full, keeping its id. Read it first. "
            "Invalid graphs leave the file unchanged; saving does not run it.",
            PipelineBody, write_pipeline,
        )
        register(
            "list_automations", "List automations, invalid entries and available plugin actions. "
            "Automations listen while the project is open.", Arguments, list_automations,
        )
        register(
            "read_automation", "Read an automation by id before replacing it.",
            AutomationId, read_automation,
        )
        register(
            "create_automation", "Create an automation with one trigger and one action. "
            "Disabled by default; enabled=true starts listening immediately. Omit id to generate one.",
            AutomationCreate, create_automation,
        )
        register(
            "write_automation", "Replace an existing automation in full using its id. Read it first. "
            "enabled=true starts listening immediately; false disables it.",
            AutomationWrite, write_automation,
        )
        register(
            "list_skills", "List project and read-only packaged skills, descriptions, warnings "
            "and supporting filenames.", Arguments, list_skills,
        )
        register(
            "read_skill", "Read a skill's complete SKILL.md and updated_at version before editing.",
            SkillName, read_skill,
        )
        register(
            "create_skill", "Create a project skill with complete SKILL.md content (YAML frontmatter "
            "and instructions). Name: lowercase words joined by hyphens. Refuses existing names; "
            "assigns it to nobody.", SkillCreate, create_skill,
        )
        register(
            "write_skill", "Replace a project skill's complete SKILL.md. Supply updated_at from "
            "read_skill as expected_updated_at; stale writes and packaged skill edits are refused. "
            "Does not change member assignments.", SkillWrite, write_skill,
        )
        return definitions
