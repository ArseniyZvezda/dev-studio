"""The project memory tools: ``recall``, ``remember``, ``forget`` (§10.12).

A run is sent the memory's index — one line per entry — and these are how it reads an entry
whole, records what it learned, and removes what turned out wrong. They write through the
Studio's own store rather than the agent's checkout, so an entry is there for every colleague
the moment it is written, whichever worktree they run in (§10.5).
"""

from __future__ import annotations

from typing import Any

from sqlmodel import select

from ..models.teams import TeamMember
from .memory import MAX_CONTENT_CHARS, MAX_DESCRIPTION_CHARS, MemoryEntry, MemoryStore

def build_memory_tools(orchestrator, *, member_id: str, read_only: bool = False) -> list[Any]:
    from claude_agent_sdk import tool

    store: MemoryStore = orchestrator.memory

    def text(body: str, *, error: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
        if error:
            result["is_error"] = True
        return result

    async def author_name(author: str | None) -> str:
        if not author:
            return "the user"
        async with orchestrator.db.session() as session:
            found = (await session.exec(select(TeamMember).where(TeamMember.id == author))).first()
        return found.name if found is not None else author

    async def render(entry: MemoryEntry) -> str:
        head = [
            f"name: {entry.name}",
            f"description: {entry.description or '(none)'}",
            f"updated: {entry.updated_at} by {await author_name(entry.author)}",
        ]
        return "\n".join(head) + ("\n\n" + entry.content if entry.content else "")

    @tool(
        "recall",
        "Read the project memory: lasting facts the team recorded for every agent. With "
        "`name`, the whole entry. With `query`, the entries whose name, description or text "
        "contain every word of it. With neither, the full index — every entry's name and "
        "description, including any the index in your instructions left out.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "An entry's name, e.g. build-uses-ue-5-4"},
                "query": {"type": "string", "description": "Words to search the entries for."},
            },
        },
    )
    async def recall(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        query = str(args.get("query") or "").strip()
        try:
            if name:
                entry = store.get(name)
                if entry is None:
                    return text(
                        f"No memory entry named {name!r}. Call recall with no arguments to list "
                        "them, or with a query to search.",
                        error=True,
                    )
                return text(await render(entry))
            if query:
                found = store.search(query)
                if not found:
                    return text(f"No memory entry matches {query!r}.")
                return text("\n".join(entry.index_line() for entry in found))
            index = store.index()
            return text(index.text or "Project memory is empty.")
        except (ValueError, OSError) as exc:
            return text(f"{type(exc).__name__}: {exc}", error=True)

    # A subagent reads memory; changing it, like the board, is the parent's (§10.3).
    if read_only:
        return [recall]

    @tool(
        "remember",
        "Save one lasting fact to the project memory. Every agent on the team is shown each "
        "entry's name and description at the start of every run, and reads the text with "
        "recall. Use it for decisions, conventions, pitfalls and locations that future work on "
        "this project needs and that the code and documents do not already say — not for task "
        "progress. One fact per entry. Fails when the name is taken unless `replace` is true: "
        "read the existing entry with recall first, then send the corrected whole entry.",
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The entry's id: lowercase letters, digits and dashes, at most 64 "
                        "characters, e.g. build-uses-ue-5-4."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": (
                        f"One line, at most {MAX_DESCRIPTION_CHARS} characters: the fact and "
                        "when it matters. This is the line every agent sees."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        f"The fact in Markdown, at most {MAX_CONTENT_CHARS} characters: what, "
                        "why, and how to apply it. May be empty when the description says it all."
                    ),
                },
                "replace": {
                    "type": "boolean",
                    "description": "Overwrite the existing entry with this name.",
                },
            },
            "required": ["name", "description", "content"],
        },
    )
    async def remember(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        replace = args.get("replace") in (True, "true", "True", 1)
        try:
            entry = await store.write(
                name,
                description=str(args.get("description") or ""),
                content=str(args.get("content") or ""),
                author=member_id,
                create=not replace,
            )
        except FileExistsError:
            existing = store.get(name)
            said = f" ({existing.description})" if existing and existing.description else ""
            return text(
                f"A memory entry named {name!r} already exists{said}. Read it with recall, then "
                "call remember again with replace=true and the corrected whole entry, or choose "
                "another name.",
                error=True,
            )
        except (ValueError, OSError) as exc:
            return text(f"{type(exc).__name__}: {exc}", error=True)
        return text(f"Saved memory entry {entry.name}. Every agent's next run is shown it.")

    @tool(
        "forget",
        "Delete a project memory entry that turned out to be wrong or no longer holds. To "
        "correct an entry instead, use remember with replace=true.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The entry's name."}},
            "required": ["name"],
        },
    )
    async def forget(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        try:
            await store.delete(name, author=member_id)
        except FileNotFoundError as exc:
            return text(str(exc), error=True)
        except (ValueError, OSError) as exc:
            return text(f"{type(exc).__name__}: {exc}", error=True)
        return text(f"Deleted memory entry {name}.")

    return [recall, remember, forget]
