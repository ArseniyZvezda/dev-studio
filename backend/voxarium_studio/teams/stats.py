"""What a team's work has cost — the figures behind the Statistics page (§10.4).

One function, ``cost_stats``, over the rows the caller has already read. It is plain data
in and plain data out so the rules below are testable without a database, and so the REST
handler stays a lookup.

**Every task counts, archived ones included.** Archiving takes a card off the board and
nothing else; a page that summed only what the board shows would tell the user the project
got cheaper the day they tidied it (§10.4).

**The time series is built from the runs, not from the tasks.** A task's ``total_cost_usd``
is one number accumulated across every session it ever ran, so a card that was resumed
across three days would land its whole cost on one of them. The ``result`` transcript
entries carry each run's own cost with the moment it landed — that is what
``_store_result_stats`` adds up — and are the honest timeline. The task's figure is still
the authority on the *total*: whatever it holds beyond what its result entries explain
(a transcript trimmed by an older build, a cost recorded with no entry) is attributed to
the task's own end, so the curve always climbs to exactly the sum over the board.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

from ..models.teams import AgentMessage, TeamMember, TeamTask, TaskStatus
from .usage import token_count

#: Below this a residual is floating-point noise, not money nobody accounted for.
_EPSILON = 1e-9
_TOKEN_PARTS = ("tokens", "uncached_tokens", "cached_tokens", "unclassified_tokens")


def _token_parts(total: Any, uncached: Any = 0, cached: Any = 0) -> dict[str, int]:
    """Separate confirmed cache reads from new input/output; legacy balances stay unknown."""
    total = token_count(total) or 0
    uncached, cached = token_count(uncached) or 0, token_count(cached) or 0
    if uncached + cached > total:
        uncached = cached = 0
    return {"tokens": total, "uncached_tokens": uncached, "cached_tokens": cached,
            "unclassified_tokens": total - uncached - cached}


def _task_tokens(task: TeamTask) -> dict[str, int]:
    return _token_parts(task.total_tokens, getattr(task, "total_uncached_tokens", 0),
                        getattr(task, "total_cached_tokens", 0))


def _empty_tokens() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_PARTS, 0)


def _add_tokens(target: dict[str, int], values: dict[str, int]) -> None:
    for key in _TOKEN_PARTS:
        target[key] += values[key]


def _token_fields(values: dict[str, int], prefix: str) -> dict[str, int]:
    return {f"{prefix}_{key}": value for key, value in values.items()}


def _run_cost(message: AgentMessage) -> float:
    """The cost one ``result`` entry reports, or 0 for one that reports none."""
    if message.kind != "result":
        return 0.0
    try:
        return max(0.0, float(message.data.get("total_cost_usd") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _landing(task: TeamTask) -> datetime:
    """When a task's unexplained cost is booked: the end of its last run, or its start,
    or — for a task that never ran but somehow holds a figure — its creation."""
    return task.finished_at or task.started_at or task.created_at


def cost_events(
    tasks: Iterable[TeamTask], messages: Iterable[AgentMessage]
) -> list[tuple[datetime, float, str]]:
    """Every moment money was spent: ``(when, usd, task_id)``, oldest first.

    One event per ``result`` entry that carries a cost, plus one per task for the
    remainder its entries do not explain. A task whose entries add up to *more* than its
    total (a row somebody edited by hand, a reset that raced a landing result) is booked
    from its total alone at its landing time, so the series never exceeds the board.
    """
    by_task: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for message in messages:
        cost = _run_cost(message)
        if cost > _EPSILON:
            by_task[message.task_id].append((message.created_at, cost))

    events: list[tuple[datetime, float, str]] = []
    for task in tasks:
        total = max(0.0, float(task.total_cost_usd or 0.0))
        runs = by_task.get(task.id, [])
        explained = sum(cost for _, cost in runs)
        if explained > total + _EPSILON:
            runs = []
            explained = 0.0
        events.extend((when, cost, task.id) for when, cost in runs)
        residual = total - explained
        if residual > _EPSILON:
            events.append((_landing(task), residual, task.id))
    events.sort(key=lambda event: event[0])
    return events


def _token_records(
    tasks: list[TeamTask], messages: list[AgentMessage]
) -> list[tuple[datetime, dict[str, int], str, str | None]]:
    """Recorded run tokens plus the task's unexplained balance (§10.4).

    Live usage snapshots include tokens already present in results, so they are never
    summed. A current snapshot can date the balance (including a cancelled run without
    a result); otherwise the same landing fallback as money applies. Missing history
    stays in the unknown-model bucket rather than being attributed to today's model.
    """
    recorded: dict[str, str | None] = {}
    runs: dict[str, list[tuple[datetime, dict[str, int], str | None]]] = defaultdict(list)
    snapshots: dict[str, AgentMessage] = {}
    for message in sorted(messages, key=lambda entry: entry.created_at):
        model = message.data.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None
        if message.kind == "model" or (message.kind == "system" and message.text == "init"):
            recorded[message.task_id] = model
        if message.kind == "result":
            count = _token_parts(message.data.get("total_tokens"),
                                 message.data.get("total_uncached_tokens"),
                                 message.data.get("total_cached_tokens"))
            if count["tokens"]:
                runs[message.task_id].append(
                    (message.created_at, count, model or recorded.get(message.task_id))
                )
        elif message.kind == "system" and message.data.get("subtype") == "usage_update":
            snapshots[message.task_id] = message

    records: list[tuple[datetime, dict[str, int], str, str | None]] = []
    for task in tasks:
        total = _task_tokens(task)
        entries = runs.get(task.id, [])
        explained = _empty_tokens()
        for _, count, _ in entries:
            _add_tokens(explained, count)
        # The authoritative task partitions must reconcile too, not just the total.
        # Old unknown telemetry cannot silently become uncached input after a repair.
        if any(explained[key] > total[key] for key in _TOKEN_PARTS):
            entries, explained = [], _empty_tokens()
        records.extend((when, count, task.id, model) for when, count, model in entries)
        residual = {key: total[key] - explained[key] for key in _TOKEN_PARTS}
        if residual["tokens"]:
            snapshot = snapshots.get(task.id)
            # A stale snapshot must not move a newer task balance back in time.
            when = (
                snapshot.created_at
                if snapshot is not None
                and token_count(snapshot.data.get("task_total_tokens")) == total["tokens"]
                else _landing(task)
            )
            records.append((when, residual, task.id, None))
    records.sort(key=lambda event: event[0])
    return records


def average_task_days(tasks: Iterable[TeamTask]) -> list[dict[str, Any]]:
    """Lifetime spend of completed tasks, grouped by their UTC completion day (§10.4).

    Zero-spend tasks share the denominator. Tasks without a recorded completion date
    cannot be placed on this chart; pending, cancelled and review tasks do not qualify.
    """
    days: dict[date, dict[str, Any]] = {}
    for task in tasks:
        if _status_of(task) != TaskStatus.DONE or task.finished_at is None:
            continue
        entry = days.setdefault(_day(task.finished_at), {"tasks": 0, "usd": 0.0, **_empty_tokens()})
        entry["tasks"] += 1
        entry["usd"] += max(0.0, float(task.total_cost_usd or 0.0))
        _add_tokens(entry, _task_tokens(task))
    return [
        {"day": day.isoformat(), "tasks": entry["tasks"],
         "average_usd": round(entry["usd"] / entry["tasks"], 6),
         **{f"average_{key}": round(entry[key] / entry["tasks"], 6) for key in _TOKEN_PARTS}}
        for day, entry in sorted(days.items())
    ]


def average_task_duration_days(
    tasks: Iterable[TeamTask], messages: Iterable[AgentMessage],
) -> list[dict[str, Any]]:
    """Average execution time of completed tasks by completion day (§10.4).

    Provider durations cover separate runs, so resumed work is added without the idle
    time between runs. Older tasks fall back to the same start/end interval shown in
    their header. Missing/invalid timing is omitted, never counted as a zero-time task.
    Context-summary requests are auxiliary accounting, not task execution runs.
    """
    runs: dict[str, list[int | None]] = defaultdict(list)
    for message in messages:
        if message.kind == "result" and message.data.get("purpose") != "context_compaction":
            runs[message.task_id].append(token_count(message.data.get("duration_ms")))
    days: dict[date, list[int]] = defaultdict(list)
    for task in tasks:
        if _status_of(task) != TaskStatus.DONE or task.finished_at is None:
            continue
        durations = runs.get(task.id, [])
        if durations and all(value is not None for value in durations):
            duration = sum(value for value in durations if value is not None)
        elif task.started_at is not None:
            duration = round((task.finished_at - task.started_at).total_seconds() * 1000)
            if duration < 0:
                continue
        else:
            continue
        days[_day(task.finished_at)].append(duration)
    return [{"day": day.isoformat(), "tasks": len(durations),
             "average_duration_ms": round(sum(durations) / len(durations), 3)}
            for day, durations in sorted(days.items())]


def _day(when: datetime) -> date:
    """The UTC calendar day a moment falls on. Stored timestamps are UTC (``UtcDateTime``)."""
    return when.date()


def _status_of(task: TeamTask) -> TaskStatus | None:
    """The task's status as the enum, or ``None`` for a value this build does not know
    (``EnumString`` hands an older or newer Studio's spelling back untouched)."""
    try:
        return TaskStatus(task.status)
    except ValueError:
        return None


def cost_stats(
    tasks: list[TeamTask], messages: list[AgentMessage], members: list[TeamMember]
) -> dict[str, Any]:
    """The Statistics payload for one team.

    ``totals``
        ``spent_usd`` is every task summed, whatever its status — what the project has
        paid. ``done_usd`` is the finished work (the board header's figure, archived tasks
        included), ``in_flight_usd`` everything not yet finished or cancelled, and
        ``cancelled_usd`` work that was stopped. The four are one partition of ``spent_usd``
        less a rounding error.
    ``series``
        One point per UTC day on which something was spent, oldest first, with that day's
        spend and the running total up to and including it. Days with no spend are absent;
        a reader draws the gaps.
    ``by_member``
        Spend per assignee, most expensive first, with how many of their tasks the figure
        covers and how many of those are finished.
    ``activity``
        One entry per UTC day on which anything happened, oldest first — the activity
        heatmap's cells (:func:`activity_days`).
    ``token_series``, ``by_model_tokens``
        The corresponding token timeline and historical-model breakdown. Totals,
        members and activity also carry token counts alongside their existing costs.
    ``average_task_series``
        Full lifetime cost and tokens per completed task, grouped by completion day.
    ``average_task_duration_series``
        Execution duration per completed task with known timing, by completion day.
    """
    events = cost_events(tasks, messages)
    token_records = _token_records(tasks, messages)

    per_day: dict[date, float] = defaultdict(float)
    runs_per_day: dict[date, int] = defaultdict(int)
    for when, cost, _task_id in events:
        day = _day(when)
        per_day[day] += cost
        runs_per_day[day] += 1

    series: list[dict[str, Any]] = []
    running = 0.0
    for day in sorted(per_day):
        running += per_day[day]
        series.append(
            {
                "day": day.isoformat(),
                "spent_usd": round(per_day[day], 6),
                "cumulative_usd": round(running, 6),
                "runs": runs_per_day[day],
            }
        )

    tokens_per_day: dict[date, dict[str, int]] = defaultdict(_empty_tokens)
    token_runs_per_day: dict[date, int] = defaultdict(int)
    tokens_per_model: dict[str | None, dict[str, int]] = defaultdict(_empty_tokens)
    for when, count, _task_id, model in token_records:
        day = _day(when)
        _add_tokens(tokens_per_day[day], count)
        token_runs_per_day[day] += 1
        _add_tokens(tokens_per_model[model], count)
    token_series: list[dict[str, Any]] = []
    running_tokens = _empty_tokens()
    for day in sorted(tokens_per_day):
        _add_tokens(running_tokens, tokens_per_day[day])
        token_series.append({
            "day": day.isoformat(), **_token_fields(tokens_per_day[day], "spent"),
            **_token_fields(running_tokens, "cumulative"), "runs": token_runs_per_day[day],
        })

    spent = done = in_flight = cancelled = 0.0
    token_totals = {status: _empty_tokens() for status in ("spent", "done", "in_flight", "cancelled")}
    done_count = archived_count = 0
    for task in tasks:
        cost = max(0.0, float(task.total_cost_usd or 0.0))
        tokens = _task_tokens(task)
        spent += cost
        _add_tokens(token_totals["spent"], tokens)
        status = _status_of(task)
        if status == TaskStatus.DONE:
            done += cost
            _add_tokens(token_totals["done"], tokens)
            done_count += 1
            if task.archived:
                archived_count += 1
        elif status == TaskStatus.CANCELLED:
            cancelled += cost
            _add_tokens(token_totals["cancelled"], tokens)
        else:
            in_flight += cost
            _add_tokens(token_totals["in_flight"], tokens)

    roster = {member.id: member for member in members}
    per_member: dict[str, dict[str, Any]] = {}
    for task in tasks:
        member = roster.get(task.assignee_id)
        entry = per_member.setdefault(
            task.assignee_id,
            {
                "member_id": task.assignee_id,
                "name": member.name if member else task.assignee_id,
                "kind": member.kind if member else None,
                "avatar_color": member.avatar_color if member else None,
                "avatar_icon": member.avatar_icon if member else None,
                "spent_usd": 0.0,
                **_token_fields(_empty_tokens(), "spent"),
                "tasks": 0,
                "done": 0,
            },
        )
        entry["spent_usd"] += max(0.0, float(task.total_cost_usd or 0.0))
        for key, value in _token_fields(_task_tokens(task), "spent").items():
            entry[key] += value
        entry["tasks"] += 1
        if _status_of(task) == TaskStatus.DONE:
            entry["done"] += 1
    by_member = sorted(per_member.values(), key=lambda row: (-row["spent_usd"], row["name"]))
    for row in by_member:
        row["spent_usd"] = round(row["spent_usd"], 6)

    return {
        "totals": {
            "spent_usd": round(spent, 6),
            "done_usd": round(done, 6),
            "in_flight_usd": round(in_flight, 6),
            "cancelled_usd": round(cancelled, 6),
            **{key: value for status, parts in token_totals.items()
               for key, value in _token_fields(parts, status).items()},
            "tasks": len(tasks),
            "done": done_count,
            "archived": archived_count,
        },
        "series": series,
        "token_series": token_series,
        "average_task_series": average_task_days(tasks),
        "average_task_duration_series": average_task_duration_days(tasks, messages),
        "by_member": by_member,
        "by_model": model_spend(tasks, messages),
        "by_model_tokens": [
            {"model": model, **_token_fields(count, "spent")}
            for model, count in sorted(
                tokens_per_model.items(), key=lambda row: (-row[1]["tokens"], row[0] or "")
            )
        ],
        "activity": activity_days(tasks, messages, per_day, tokens_per_day),
    }


def model_spend(tasks: list[TeamTask], messages: list[AgentMessage]) -> list[dict[str, Any]]:
    """Attribute spend to recorded run models, never today's roster (§10.4).

    Unexplained money (including inconsistent transcripts) stays in the unknown bucket,
    using the same task-total authority as cost_events.
    """
    recorded: dict[str, str | None] = {}
    runs: dict[str, list[tuple[str | None, float]]] = defaultdict(list)
    for message in sorted(messages, key=lambda entry: entry.created_at):
        model = message.data.get("model")
        model = model.strip() if isinstance(model, str) and model.strip() else None
        if message.kind == "model" or (message.kind == "system" and message.text == "init"):
            recorded[message.task_id] = model
        if message.kind == "result":
            cost = _run_cost(message)
            if cost > _EPSILON:
                runs[message.task_id].append((model or recorded.get(message.task_id), cost))

    spend: dict[str | None, float] = defaultdict(float)
    for task in tasks:
        total = max(0.0, float(task.total_cost_usd or 0.0))
        entries = runs.get(task.id, [])
        explained = sum(cost for _, cost in entries)
        if explained > total + _EPSILON:
            entries, explained = [], 0.0
        for model, cost in entries:
            spend[model] += cost
        if total - explained > _EPSILON:
            spend[None] += total - explained
    return [
        {"model": model, "spent_usd": round(cost, 6)}
        for model, cost in sorted(spend.items(), key=lambda row: (-row[1], row[0] or ""))
    ]


def activity_days(
    tasks: Iterable[TeamTask],
    messages: Iterable[AgentMessage],
    spend_per_day: dict[date, float] | None = None,
    tokens_per_day: dict[date, dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """What the team did on each UTC day — the cells of the activity heatmap (§10.4).

    A day's ``activity`` is what the cell's shade is drawn from: the agent runs that landed
    (every ``result`` entry, whatever it cost — a free run is still a run) plus the tasks
    that reached ``done`` that day, since a person closing their own task is the team
    working too. The two are also carried apart, with the day's spend, so the tooltip can
    say what the shade is made of. Only days with any of the three are listed; the grid
    draws the rest empty.
    """
    runs: dict[date, int] = defaultdict(int)
    for message in messages:
        if message.kind == "result":
            runs[_day(message.created_at)] += 1

    finished: dict[date, int] = defaultdict(int)
    for task in tasks:
        if _status_of(task) == TaskStatus.DONE and task.finished_at is not None:
            finished[_day(task.finished_at)] += 1

    spend = spend_per_day or {}
    tokens = tokens_per_day or {}
    days = (set(runs) | set(finished) | {day for day, usd in spend.items() if usd > _EPSILON}
            | {day for day, count in tokens.items() if count["tokens"] > 0})
    return [
        {
            "day": day.isoformat(),
            "runs": runs[day],
            "finished": finished[day],
            "spent_usd": round(spend.get(day, 0.0), 6),
            **_token_fields(tokens.get(day, _empty_tokens()), "spent"),
            "activity": runs[day] + finished[day],
        }
        for day in sorted(days)
    ]
