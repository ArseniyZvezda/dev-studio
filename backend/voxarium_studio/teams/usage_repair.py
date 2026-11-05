"""Auditable, offline repair from original provider journals (spec §10.4, §10.6).

The default command only audits. Applying a saved audit first backs up SQLite, checks
that both task evidence and journal files are unchanged, then replaces accounting via
an explicit cutoff record. Original transcripts and all non-accounting task fields stay
intact. Missing journals or incomplete lifetime coverage never become invented zeros.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .backends.codex_costs import estimate_cost

VERSION = 1
FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
COUNTERS = ("total_tokens", "total_uncached_tokens", "total_cached_tokens", "total_cost_usd")
TERMINAL = {"done", "cancelled"}
TASK_PATTERN = re.compile(r"You are working on task\s+([A-Za-z0-9_-]+)\.")
PROJECT_PATTERN = re.compile(r"The project you are working on is at ([^;\n]+)")
EVIDENCE_KINDS = {"accounting", "compressed summary", "context_summary"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _iso(value: str | datetime | None) -> str | None:
    parsed = value if isinstance(value, datetime) else _time(value)
    return parsed.astimezone(timezone.utc).isoformat() if parsed else None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _money(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 else 0.0


def _usage(value: Any) -> dict[str, int]:
    return {key: _count(value.get(key)) for key in FIELDS} if isinstance(value, dict) else dict.fromkeys(FIELDS, 0)


def _event(at: str, usage: dict[str, int], cost: float, model: str | None, session: str) -> dict:
    cached = usage.get("cache_read_input_tokens", 0)
    total = sum(usage.values())
    return {"kind": "result", "role": "system", "text": "", "created_at": _iso(at), "data": {
        "accounting_version": 2, "repair_source": "original_journals",
        "provider_session_id": session, "model": model, "usage": usage,
        "total_tokens": total, "total_uncached_tokens": total - cached,
        "total_cached_tokens": cached, "total_cost_usd": round(cost, 9),
    }}


def _add_usage(target: dict[str, int], usage: dict[str, int]) -> None:
    for key in FIELDS:
        target[key] = target.get(key, 0) + usage.get(key, 0)


def _new_journal(path: Path, backend: str) -> dict:
    stat = path.stat()
    return {"path": str(path.resolve()), "backend": backend, "session_id": path.stem,
            "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "labels": [], "events": [], "observations": [], "issues": [],
            "first_at": None, "last_at": None, "source_origin": None, "cwd": None,
            "summary_prompt": False, "project_roots": [], "assistant_total_tokens": 0,
            "assistant_block_tokens": 0}


def _records(path: Path, journal: dict):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            try:
                record = json.loads(raw)
            except (ValueError, UnicodeError):
                journal["issues"].append("malformed_or_partial_journal_record")
                continue
            if isinstance(record, dict):
                at = _iso(record.get("timestamp"))
                if at:
                    journal["first_at"] = journal["first_at"] or at
                    journal["last_at"] = at
                yield record
    journal["sha256"] = digest.hexdigest()


def parse_codex(path: Path) -> dict:
    """Inclusive input minus cached input, then differences of lifetime counters.

    Repeated rate-limit snapshots and reasoning output are never additional tokens.
    Every token_count is dated at its response; turn boundaries remain observations for
    proving that the old Studio results have corresponding original accounting.
    """
    journal = _new_journal(path, "codex")
    previous = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    model = None
    has_start = False
    for record in _records(path, journal):
        kind, payload = record.get("type"), record.get("payload")
        if not isinstance(payload, dict):
            continue
        at = _iso(record.get("timestamp")) or journal["last_at"]
        if kind == "session_meta":
            journal["session_id"] = payload.get("id") or payload.get("session_id") or journal["session_id"]
            journal["source_origin"] = payload.get("source")
            journal["cwd"] = payload.get("cwd")
        elif kind == "response_item" and payload.get("role") == "developer":
            content = payload.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        text = str(block.get("text", ""))
                        journal["labels"].extend(TASK_PATTERN.findall(text))
                        journal["summary_prompt"] |= text.startswith("Summarize this task history")
                        journal["project_roots"].extend(PROJECT_PATTERN.findall(text))
        elif kind == "turn_context":
            model = payload.get("model") or model
        elif kind == "event_msg":
            subtype = payload.get("type")
            if subtype == "task_started":
                has_start = True
            if subtype == "token_count":
                info = payload.get("info") or {}
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                if not isinstance(total, dict) or not at:
                    continue
                current = {key: _count(total.get(key)) for key in previous}
                if any(current[key] < previous[key] for key in previous):
                    journal["issues"].append("codex_lifetime_counter_decreased")
                    continue
                delta = {key: current[key] - previous[key] for key in previous}
                if delta["cached_input_tokens"] > delta["input_tokens"]:
                    journal["issues"].append("codex_cache_exceeds_input")
                    continue
                usage = {"input_tokens": delta["input_tokens"] - delta["cached_input_tokens"],
                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": delta["cached_input_tokens"],
                         "output_tokens": delta["output_tokens"]}
                if sum(usage.values()):
                    if not has_start:
                        journal["issues"].append("codex_missing_initial_turn_boundary")
                    cost = estimate_cost(model, usage)
                    if cost is None:
                        journal["issues"].append("codex_unknown_model_tariff")
                    journal["events"].append(_event(at, usage, cost or 0.0, model, journal["session_id"]))
                previous = current
            if subtype in {"task_complete", "turn_aborted", "task_started"} and at:
                normalized = {"input_tokens": previous["input_tokens"] - previous["cached_input_tokens"],
                              "cache_read_input_tokens": previous["cached_input_tokens"], "output_tokens": previous["output_tokens"]}
                journal["observations"].append({"at": at, "kind": subtype,
                    "total_tokens": previous["input_tokens"] + previous["output_tokens"],
                    "total_cost_usd": estimate_cost(model, normalized), "model": model})
    journal["labels"] = sorted(set(journal["labels"]))
    return journal


def parse_claude(path: Path) -> dict:
    """Deduplicate API message IDs; reconcile hidden/internal requests from cost-state.

    One assistant response may occupy many content-block records. Per-component maxima
    credit it only once. Provider modelUsage counters additionally cover compaction and
    internal requests missing from assistant records; only their unexplained remainder
    is added. Model cost-state values are cumulative and are differenced independently.
    """
    journal = _new_journal(path, "claude_code")
    messages: dict[str, dict[str, int]] = {}
    model_totals: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    model_costs: dict[str, float] = {}
    total_cost = 0.0
    has_cost = False
    state_keys = {"inputTokens": "input_tokens", "cacheCreationInputTokens": "cache_creation_input_tokens",
                  "cacheReadInputTokens": "cache_read_input_tokens", "outputTokens": "output_tokens"}
    for record in _records(path, journal):
        at = _iso(record.get("timestamp")) or journal["last_at"]
        journal["session_id"] = record.get("sessionId") or journal["session_id"]
        journal["cwd"] = journal["cwd"] or record.get("cwd")
        if record.get("type") == "attachment":
            attachment = record.get("attachment") or {}
            if isinstance(attachment, dict) and attachment.get("type") == "prompt_snapshot":
                prompt = attachment.get("systemPrompt") or []
                if isinstance(prompt, str):
                    prompt = [prompt]
                text = "\n".join(str(item) for item in prompt)
                journal["labels"].extend(TASK_PATTERN.findall(text))
                journal["project_roots"].extend(PROJECT_PATTERN.findall(text))
        if record.get("type") == "assistant" and at:
            message = record.get("message") or {}
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            identity = message.get("id")
            if not identity:
                journal["issues"].append("claude_usage_without_message_id")
                continue
            model = message.get("model") or "unknown"
            current = _usage(message["usage"])
            journal["assistant_block_tokens"] += sum(current.values())
            previous = messages.setdefault(identity, dict.fromkeys(FIELDS, 0))
            delta = {key: max(0, current[key] - previous[key]) for key in FIELDS}
            messages[identity] = {key: max(current[key], previous[key]) for key in FIELDS}
            journal["assistant_total_tokens"] += sum(delta.values())
            _add_usage(model_totals[model], delta)
            if sum(delta.values()):
                journal["events"].append(_event(at, delta, 0.0, model, journal["session_id"]))
        if record.get("type") != "cost-state" or not at:
            continue
        has_cost = True
        current_cost = _money(record.get("totalCostUSD"))
        if current_cost + 1e-7 < total_cost:
            journal["issues"].append("claude_session_cost_decreased")
            continue
        states = record.get("modelUsage")
        explained_cost = 0.0
        if isinstance(states, dict):
            for model, state in states.items():
                if not isinstance(state, dict):
                    continue
                reported = {target: _count(state.get(source)) for source, target in state_keys.items()}
                delta = {key: max(0, reported[key] - model_totals[model][key]) for key in FIELDS}
                _add_usage(model_totals[model], delta)
                cost = _money(state.get("costUSD"))
                cost_delta = max(0.0, cost - model_costs.get(model, 0.0))
                model_costs[model] = cost
                explained_cost += cost_delta
                if sum(delta.values()) or cost_delta:
                    journal["events"].append(_event(at, delta, cost_delta, model, journal["session_id"]))
        remainder = current_cost - total_cost - explained_cost
        if remainder > 1e-7:
            journal["events"].append(_event(at, dict.fromkeys(FIELDS, 0), remainder, None, journal["session_id"]))
        elif remainder < -1e-6:
            journal["issues"].append("claude_model_costs_exceed_session_cost")
        total_cost = current_cost
        journal["observations"].append({"at": at, "total_cost_usd": total_cost,
                                         "total_tokens": sum(sum(u.values()) for u in model_totals.values())})
    if journal["events"] and not has_cost:
        journal["issues"].append("claude_missing_cost_state")
    # A truncated journal may have assistant usage after the last persisted bill.
    if has_cost and journal["observations"] and journal["events"]:
        if _time(journal["events"][-1]["created_at"]) > _time(journal["observations"][-1]["at"]):
            journal["issues"].append("claude_unbilled_tail")
    journal["labels"] = sorted(set(journal["labels"]))
    return journal


def _flatten(messages: list[dict]) -> list[dict]:
    flat = []
    for message in messages:
        data = message.get("data") or {}
        if message["kind"] in EVIDENCE_KINDS:
            flat.extend(_flatten(data.get("evidence", [])))
        else:
            flat.append(message)
    return sorted(flat, key=lambda row: row.get("created_at") or "")


def _read_tasks(connection: sqlite3.Connection) -> tuple[list[dict], dict[str, list[dict]], dict[str, dict]]:
    connection.row_factory = sqlite3.Row
    tasks = [dict(row) for row in connection.execute("SELECT * FROM team_task ORDER BY number,id")]
    messages: dict[str, list[dict]] = defaultdict(list)
    query = """SELECT id,task_id,kind,text,data,created_at FROM agent_message
        WHERE kind IN ('result','accounting','compressed summary','context_summary','model','status')
        OR (kind='system' AND text IN ('init','thread.started','session_recovered'))
        ORDER BY created_at,id"""
    for row in connection.execute(query):
        entry = dict(row)
        try:
            entry["data"] = json.loads(entry["data"] or "{}")
        except ValueError:
            entry["data"] = {"invalid_json": True}
        messages[entry["task_id"]].append(entry)
    stamps = {row["task_id"]: {"count": row["n"], "last": row["last"]} for row in connection.execute(
        "SELECT task_id,COUNT(*) AS n,MAX(created_at) AS last FROM agent_message GROUP BY task_id")}
    return tasks, messages, stamps


def _fingerprint(task: dict, messages: list[dict], stamp: dict | None) -> str:
    return _digest({"task": task, "messages": messages, "stamp": stamp})


def _known_sessions(task: dict, messages: list[dict]) -> set[str]:
    result = {task["agent_session_id"]} if task.get("agent_session_id") else set()
    for message in _flatten(messages):
        data = message.get("data") or {}
        for key in ("session_id", "thread_id", "provider_session_id", "previous_session_id"):
            if isinstance(data.get(key), str) and data[key]:
                result.add(data[key])
    return result


def _aggregate(events: list[dict]) -> list[dict]:
    """One result per original day/model/session keeps canonical evidence compact."""
    rows: dict[tuple, dict] = {}
    for event in events:
        data, at = event["data"], event["created_at"]
        key = (at[:10], data["model"], data["provider_session_id"])
        if key not in rows:
            rows[key] = _event(at, dict.fromkeys(FIELDS, 0), 0.0, data["model"], data["provider_session_id"])
        target = rows[key]
        target["created_at"] = max(target["created_at"], at)
        _add_usage(target["data"]["usage"], data["usage"])
        for field in COUNTERS:
            target["data"][field] += data[field]
    for row in rows.values():
        row["data"]["total_cost_usd"] = round(row["data"]["total_cost_usd"], 9)
    return sorted(rows.values(), key=lambda row: (row["created_at"], row["data"]["model"] or ""))


def _project_journal(journal: dict, project: Path) -> bool:
    """An explicit task number alone is not globally unique across managed projects."""
    cwd = journal.get("cwd")
    if not isinstance(cwd, str):
        return False
    try:
        root = Path(cwd).resolve()
        if root == project or project in root.parents:
            return True
        return any(Path(marker).resolve() == project for marker in journal.get("project_roots", []))
    except (ValueError, OSError):
        return False


def _summary_root(journal: dict, project: Path) -> bool:
    """Parent launch roots are only candidates for a unique accounting-result match."""
    if _project_journal(journal, project):
        return True
    try:
        root = Path(journal.get("cwd") or "").resolve()
        return root.parent != root and root in project.parents
    except (ValueError, OSError):
        return False


def _unmatched_results(results: list[dict], sources: list[dict]) -> list[str | None]:
    """A charged result needs its own provider observation, not just an overlapping date.

    Old Codex/Claude results report a cumulative bill; newer Codex results report the
    invocation delta. Zero-cost results need matching tokens instead. A source point
    cannot explain two independent charged results (spec §10.4).
    """
    observations = []
    for source in sources:
        baseline_cost = baseline_tokens = 0
        for observation in source["observations"]:
            if observation.get("kind") == "task_started":
                baseline_cost = _money(observation.get("total_cost_usd"))
                baseline_tokens = _count(observation.get("total_tokens"))
                continue
            observations.append({**observation,
                "delta_cost": max(0.0, _money(observation.get("total_cost_usd")) - baseline_cost),
                "delta_tokens": max(0, _count(observation.get("total_tokens")) - baseline_tokens)})
    used = set()
    unmatched = []
    for result in results:
        data, at = result.get("data") or {}, _time(result.get("created_at"))
        cost, tokens = _money(data.get("total_cost_usd")), _count(data.get("total_tokens"))
        if not cost and not tokens:
            continue
        matches = []
        for index, observation in enumerate(observations):
            when = _time(observation.get("at"))
            if index in used or not at or not when or abs((at - when).total_seconds()) > 600:
                continue
            matches_usage = (
                any(abs(cost - value) <= 1e-6 for value in
                    (_money(observation.get("total_cost_usd")), observation["delta_cost"]))
                if cost else tokens in (_count(observation.get("total_tokens")), observation["delta_tokens"])
            )
            if matches_usage:
                matches.append((abs((at - when).total_seconds()), index))
        if matches:
            used.add(min(matches)[1])
        else:
            unmatched.append(result.get("created_at"))
    return unmatched


def build_audit(project: Path, *, claude_home: Path | None = None, codex_home: Path | None = None) -> dict:
    project = project.resolve()
    database = project / "voxarium_studio" / "app.db"
    claude_home = claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.execute("BEGIN")
        tasks, messages, stamps = _read_tasks(connection)
    from ..core.config import load_config
    from ..core.paths import ProjectPaths
    code = load_config(ProjectPaths.for_project(project)).task_code(project.name)
    labels = {f"{code}-{task['number']}": task["id"] for task in tasks}
    labels.update({f"{code}-{task['number']:02d}": task["id"] for task in tasks})
    labels.update({task["id"]: task["id"] for task in tasks})
    known = {task["id"]: _known_sessions(task, messages[task["id"]]) for task in tasks}
    by_session: dict[str, set[str]] = defaultdict(set)
    for task_id, sessions in known.items():
        for session in sessions:
            by_session[session].add(task_id)
    journals = []
    claude_files = sorted((claude_home / "projects").glob("*/*.jsonl"))
    # Only parent/project launch directories or explicitly recorded UUIDs are candidates.
    folder_names = {re.sub(r"[^A-Za-z0-9]", "-", str(parent)) for parent in (project, *project.parents) if parent.parent != parent}
    for path in claude_files:
        if path.stem in by_session or path.parent.name in folder_names:
            journal = parse_claude(path)
            explicit = {labels[label] for label in journal["labels"] if label in labels} if _project_journal(journal, project) else set()
            journal["task_ids"] = sorted(by_session.get(journal["session_id"], set()) | explicit)
            if journal["task_ids"] or (journal["summary_prompt"] and _summary_root(journal, project)):
                journals.append(journal)
    for folder in ("sessions", "archived_sessions"):
        for path in sorted((codex_home / folder).glob("**/rollout-*.jsonl")):
            # Developer instructions, unlike quoted tool output or user dialogue, name
            # the task actually assigned to this session. No arbitrary-text matching.
            journal = parse_codex(path)
            explicit = {labels[label] for label in journal["labels"] if label in labels} if _project_journal(journal, project) else set()
            journal["task_ids"] = sorted(by_session.get(journal["session_id"], set()) | explicit)
            if journal["task_ids"] or (journal["summary_prompt"] and _summary_root(journal, project)):
                journals.append(journal)
    # Compaction runs deliberately use a summarizer prompt without the worker's task
    # identity. Bind only a unique exact recorded cost/model and nearby completion,
    # within this managed project's launch root and with that exact summarizer prompt.
    retained_results = [(task["id"], index, entry) for task in tasks
                        for index, entry in enumerate(_flatten(messages[task["id"]])) if entry["kind"] == "result"]
    reserved = set()
    for task_id, index, result in retained_results:
        data, at = result.get("data") or {}, _time(result.get("created_at"))
        if not at:
            continue
        for journal in journals:
            if task_id not in journal["task_ids"]:
                continue
            if any(o.get("kind") != "task_started" and _time(o["at"]) <= at <= _time(o["at"]) + timedelta(minutes=10)
                   and abs(_money(o.get("total_cost_usd")) - _money(data.get("total_cost_usd"))) < 1e-7
                   for o in journal["observations"]):
                reserved.add((task_id, index))
                break
    summary_matches: dict[tuple, list[dict]] = defaultdict(list)
    ambiguous_summary_tasks = set()
    for journal in journals:
        if journal["task_ids"] or not journal["summary_prompt"] or journal["issues"]:
            continue
        cost = sum(e["data"]["total_cost_usd"] for e in journal["events"])
        models = {e["data"]["model"] for e in journal["events"]}
        end = _time(journal["last_at"])
        candidates = set()
        for task_id, index, result in retained_results:
            if (task_id, index) in reserved:
                continue
            data, at = result.get("data") or {}, _time(result.get("created_at"))
            if cost > 0 and at and end and end <= at <= end + timedelta(minutes=10) and \
                abs(_money(data.get("total_cost_usd")) - cost) < 1e-7 and data.get("model") in models:
                candidates.add((task_id, index))
        if len(candidates) == 1:
            summary_matches[next(iter(candidates))].append(journal)
        elif candidates:
            ambiguous_summary_tasks.update(tid for tid, _ in candidates)
    for (task_id, _index), matches in summary_matches.items():
        # Identical archive copies are one source, but two distinct sessions must not
        # consume the same recorded result merely because their price happens to match.
        identities = {j["session_id"] for j in matches}
        if len(identities) != 1:
            ambiguous_summary_tasks.add(task_id)
            continue
        for journal in matches:
            journal["task_ids"] = [task_id]
            journal["association"] = "unique_summary_cost_model_completion_time"
    # Duplicate filesystem copies of one session are not additional work.
    grouped: dict[str, list[dict]] = defaultdict(list)
    for journal in journals:
        grouped[journal["session_id"]].append(journal)
    journals = []
    for versions in grouped.values():
        if len({j["sha256"] for j in versions}) > 1:
            for journal in versions:
                journal["issues"].append("conflicting_journal_versions")
        journals.extend(versions[:1] if len({j["sha256"] for j in versions}) == 1 else versions)
    sources: dict[str, list[dict]] = defaultdict(list)
    for journal in journals:
        for task_id in journal["task_ids"]:
            sources[task_id].append(journal)
    report = {"version": VERSION, "generated_at": _iso(datetime.now(timezone.utc)),
              "project": str(project), "database": str(database), "tasks": []}
    for task in tasks:
        tid = task["id"]
        original = messages[tid]
        resets = [_time(entry.get("created_at")) for entry in original
                  if entry["kind"] == "status" and (entry.get("data") or {}).get("reason") == "reset"]
        reset_at = max((at for at in resets if at), default=None)
        # A reset deliberately disowns earlier attempts while retaining the task id.
        source = [journal for journal in sources[tid] if not reset_at or
                  not _time(journal["last_at"]) or _time(journal["last_at"]) > reset_at]
        row = {"task_id": tid, "number": task["number"], "title": task["title"],
               "before": {key: task.get(key, 0) for key in COUNTERS},
               "fingerprint": _fingerprint(task, original, stamps.get(tid)),
               "cutoff_at": _iso((stamps.get(tid) or {}).get("last") or task.get("finished_at") or task["created_at"]),
               "coverage": "unrecoverable", "reasons": [], "source_paths": [s["path"] for s in source],
               "sources": [{key: s.get(key) for key in ("path", "session_id", "backend", "sha256", "size", "mtime_ns", "association")} for s in source]}
        reasons = row["reasons"]
        if tid in ambiguous_summary_tasks:
            reasons.append("ambiguous_summary_journal")
        if task["status"] not in TERMINAL:
            reasons.append("task_not_terminal")
        if any((m.get("data") or {}).get("repair") for m in original):
            reasons.append("already_repaired")
        found = {s["session_id"] for s in source}
        missing = sorted(known[tid] - found)
        if missing:
            reasons.append("missing_recorded_sessions")
            row["missing_sessions"] = missing
        if not source:
            reasons.append("no_original_journals")
        for journal in source:
            if len(journal["task_ids"]) != 1:
                reasons.append("journal_assigned_to_multiple_tasks")
            if reset_at and _time(journal["first_at"]) and _time(journal["first_at"]) < reset_at:
                reasons.append("journal_crosses_task_reset")
            reasons.extend(journal["issues"])
        flat = _flatten(original)
        results = [entry for entry in flat if entry["kind"] == "result"]
        old_cost = sum(_money((entry.get("data") or {}).get("total_cost_usd")) for entry in results)
        old_tokens = sum(_count((entry.get("data") or {}).get("total_tokens")) for entry in results)
        if abs(old_cost - _money(task.get("total_cost_usd"))) > max(1e-5, abs(old_cost) * 1e-8):
            reasons.append("retained_results_do_not_cover_task_cost")
        unmatched = _unmatched_results(results, source)
        if unmatched:
            reasons.append("original_results_without_matching_journal")
            row["unmatched_result_dates"] = unmatched
        if not results and (task.get("total_tokens", 0) or task.get("total_cost_usd", 0)):
            reasons.append("missing_lifetime_accounting_evidence")
        for journal in source:
            first, last = _time(journal["first_at"]), _time(journal["last_at"])
            created, finished = _time(task.get("created_at")), _time(task.get("finished_at"))
            if first and created and first < created - timedelta(minutes=1):
                reasons.append("journal_predates_task")
            if last and finished and last > finished + timedelta(minutes=10):
                reasons.append("journal_continued_after_task_finished")
        evidence = _aggregate([event for s in source for event in s["events"]])
        after = {key: sum(e["data"][key] for e in evidence) for key in COUNTERS}
        after["total_cost_usd"] = round(after["total_cost_usd"], 9)
        reconstructed = dict(after)
        # Older archived tasks can have lost their last result rows. Reproducing the
        # old Codex bug (sum of thread-cumulative completed-turn counters) exactly is
        # independent evidence that every historically charged token is represented.
        completed = [observation for s in source for observation in s["observations"]
                     if s["backend"] == "codex" and observation.get("kind") == "task_complete"]
        legacy_tokens = sum(o["total_tokens"] for o in completed)
        claude_tokens = sum(s["assistant_total_tokens"] for s in source if s["backend"] == "claude_code")
        claude_blocks = sum(s["assistant_block_tokens"] for s in source if s["backend"] == "claude_code")
        before_tokens = _count(task.get("total_tokens"))
        explained_tokens = before_tokens == 0 or before_tokens in {
            legacy_tokens + claude_tokens, legacy_tokens + claude_blocks,
            after["total_tokens"],
        }
        if not explained_tokens:
            reasons.append("unexplained_task_token_balance")
            row["token_coverage_candidates"] = {"retained_result_tokens": old_tokens,
                "legacy_codex_plus_claude_unique": legacy_tokens + claude_tokens,
                "legacy_codex_plus_claude_blocks": legacy_tokens + claude_blocks,
                "original_provider_tokens": after["total_tokens"]}
        only_codex_work = all(s["backend"] == "codex" or not any(e["data"]["total_tokens"] or e["data"]["total_cost_usd"]
                                                                   for e in s["events"]) for s in source)
        token_proof = bool(source and only_codex_work and task.get("total_tokens", 0) > 0 and legacy_tokens == task["total_tokens"])
        cost_status = "exact"
        if token_proof:
            row["coverage_proof"] = {"kind": "legacy_codex_cumulative_reconciliation", "legacy_total_tokens": legacy_tokens}
            removable = {"retained_results_do_not_cover_task_cost", "original_results_without_matching_journal",
                         "missing_lifetime_accounting_evidence"}
            reasons[:] = [reason for reason in reasons if reason not in removable]
            legacy_cost = sum(o.get("total_cost_usd") or 0.0 for o in completed)
            row["coverage_proof"]["legacy_total_cost_usd"] = round(legacy_cost, 9)
            if abs(legacy_cost - _money(task.get("total_cost_usd"))) > 1e-6:
                # Token completeness does not prove an unexplained dollar balance.
                # Preserve the entire old cost separately; never label it as verified
                # provider cost or distribute its difference over corrected token rows.
                cost_status = "unverified"
                row["cost_reason"] = "legacy_cost_does_not_reconcile; original cost retained"
                row["unexplained_legacy_cost_usd"] = round(_money(task.get("total_cost_usd")) - legacy_cost, 9)
                for entry in evidence:
                    entry["data"]["total_cost_usd"] = 0.0
                before_cost = _money(task.get("total_cost_usd"))
                cost_records = results if old_cost <= before_cost + 1e-7 else []
                accounted = 0.0
                for result in cost_records:
                    amount = _money((result.get("data") or {}).get("total_cost_usd"))
                    if not amount:
                        continue
                    entry = _event(result["created_at"], dict.fromkeys(FIELDS, 0), amount,
                                   (result.get("data") or {}).get("model"), "historical-unverified")
                    entry["data"]["repair_source"] = "preserved_unverified_cost"
                    evidence.append(entry)
                    accounted += amount
                if before_cost - accounted > 1e-7:
                    entry = _event(task.get("finished_at") or task["created_at"], dict.fromkeys(FIELDS, 0),
                                   before_cost - accounted, None, "historical-unverified")
                    entry["data"]["repair_source"] = "preserved_unverified_cost"
                    evidence.append(entry)
                after["total_cost_usd"] = before_cost
        if source:
            row["coverage"] = "partial" if reasons else "exact"
            row["reconstructed"] = reconstructed
        if not reasons:
            row["after"] = after
            row["evidence"] = evidence
            row["token_status"] = "exact"
            row["cost_status"] = cost_status
        if not source and not row["before"]["total_tokens"] and not row["before"]["total_cost_usd"]:
            row["coverage"] = "no_recorded_spend"
        row["reasons"] = sorted(set(reasons))
        report["tasks"].append(row)
    report["summary"] = {status: sum(row["coverage"] == status for row in report["tasks"])
                         for status in ("exact", "partial", "unrecoverable", "no_recorded_spend")}
    report["audit_sha256"] = _digest(report)
    return report


def apply_audit(report: dict, *, backup_path: Path | None = None, database_path: Path | None = None) -> dict:
    """Apply only fully recovered, unchanged rows from a saved reviewed audit."""
    expected = report.get("audit_sha256")
    if expected != _digest({key: value for key, value in report.items() if key != "audit_sha256"}):
        raise ValueError("Audit checksum does not match; regenerate the audit")
    if report.get("version") != VERSION:
        raise ValueError("Unsupported audit version")
    audited_database = Path(report["database"]).resolve()
    project_database = Path(report["project"]).resolve() / "voxarium_studio" / "app.db"
    if audited_database != project_database:
        raise ValueError("Audit database is outside its managed project")
    database = (database_path or audited_database).resolve()
    if not database.is_file():
        raise ValueError("Apply target database does not exist")
    selected = [row for row in report["tasks"] if row["coverage"] == "exact"]
    if not selected:
        return {"applied": 0, "backup": None}
    checked = set()
    for row in selected:
        for source in row["sources"]:
            if source["path"] in checked:
                continue
            checked.add(source["path"])
            digest = hashlib.sha256()
            with Path(source["path"]).open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != source["sha256"]:
                raise ValueError(f"Original journal changed since audit: {source['path']}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_path or database.parent / "backups" / "usage-repair" / f"app-before-{stamp}.db"
    backup_path = backup_path.resolve()
    if backup_path == database or backup_path.exists():
        raise ValueError("Backup must be a new file distinct from the live database")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(backup_path) as destination:
            source.backup(destination)
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        tasks, messages, stamps = _read_tasks(connection)
        by_id = {task["id"]: task for task in tasks}
        for row in selected:
            tid = row["task_id"]
            task = by_id.get(tid)
            if not task or task["status"] not in TERMINAL or _fingerprint(task, messages[tid], stamps.get(tid)) != row["fingerprint"]:
                raise ValueError(f"Task changed since audit; no tasks repaired: {tid}")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(team_task)")}
        for field in ("total_uncached_tokens", "total_cached_tokens", "total_input_tokens", "total_output_tokens"):
            if field not in columns:
                connection.execute(f"ALTER TABLE team_task ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0")
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
        for row in selected:
            task, after = by_id[row["task_id"]], row["after"]
            # Direction totals must be rebuilt from repaired evidence, never reused from
            # accounting that the repair has invalidated (§10.4).
            connection.execute("UPDATE team_task SET total_tokens=?,total_uncached_tokens=?,total_cached_tokens=?,total_cost_usd=?,total_input_tokens=0,total_output_tokens=0 WHERE id=?",
                               tuple(after[key] for key in COUNTERS) + (task["id"],))
            data = {"accounting_version": 2,
                    "repair": {"version": VERSION, "cutoff_at": row["cutoff_at"],
                               "source_paths": row["source_paths"], "audit_sha256": expected},
                    "evidence": row["evidence"]}
            connection.execute("INSERT INTO agent_message (id,task_id,member_id,role,kind,text,data,created_at) VALUES (?,?,?,?,?,?,?,?)",
                               ("msg_" + uuid4().hex[:16], task["id"], task["assignee_id"], "system", "accounting", "", _json(data), now))
    return {"applied": len(selected), "backup": str(backup_path), "audit_sha256": expected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="Write the read-only audit JSON here")
    parser.add_argument("--apply", action="store_true", help="Apply an unchanged, previously saved --audit")
    parser.add_argument("--audit", type=Path, help="Previously saved audit, required with --apply")
    parser.add_argument("--database", type=Path, help="Explicit SQLite copy for an apply rehearsal")
    parser.add_argument("--claude-home", type=Path)
    parser.add_argument("--codex-home", type=Path)
    args = parser.parse_args()
    if args.apply:
        if not args.audit:
            parser.error("--apply requires --audit pointing to the reviewed JSON report")
        report = json.loads(args.audit.read_text(encoding="utf-8"))
        if Path(report["project"]).resolve() != args.project.resolve():
            parser.error("--project does not match the saved audit")
        print(json.dumps(apply_audit(report, database_path=args.database), ensure_ascii=False, indent=2))
        return
    report = build_audit(args.project, claude_home=args.claude_home, codex_home=args.codex_home)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"report": str(args.report.resolve()), **report["summary"]}, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
