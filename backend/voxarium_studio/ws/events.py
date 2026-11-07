"""Event taxonomy for the multiplexed WebSocket channel (§11.2)."""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    # Pipeline execution
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_PAUSED = "run.paused"
    """A node with ``pause_on_complete`` finished and the run is waiting to be continued.
    Carries ``node_ids`` — every node currently holding it, not only the newest."""

    RUN_RESUMED = "run.resumed"
    NODE_STATUS_CHANGED = "node.status_changed"
    NODE_LOG = "node.log"
    NODE_PROGRESS = "node.progress"
    """How far along a running node is, as it says so itself. Deliberately not persisted:
    it describes a node while it runs, and a finished ``NodeRun`` has its outputs and its
    log instead. A client that missed a frame just gets the next one."""

    # Teams
    TEAM_UPDATED = "team.updated"
    TEAM_TASK_STATUS_CHANGED = "team.task.status_changed"
    TEAM_TASK_CREATED = "team.task.created"
    TEAM_TASK_UPDATED = "team.task.updated"
    """A task's own fields were edited. A move between columns is a status change and
    arrives as ``team.task.status_changed``, so this fires only for the rest."""

    TEAM_TASK_DELETED = "team.task.deleted"
    TEAM_TASK_COMMIT_CHANGED = "team.task.commit_changed"
    """A task's commit draft moved: generation started or finished, or the Commit button
    landed (§10.4). Carries ``task_id``, ``team_id`` and ``generating``; the draft itself is
    read back through ``GET …/tasks/{id}/commit``."""
    TEAM_MEMBER_MESSAGE = "team.member.message"
    """Carries ``member_id`` in the payload; the spec's ``team.{member_id}.message``
    shape is preserved as the ``channel`` field so clients can filter per member."""

    TEAM_DOC_CHANGED = "team.doc.changed"
    """A project document was written — by a team member's tool call or by the user — or,
    with ``deleted`` set, deleted."""

    TEAM_DOC_FOLDER_CHANGED = "team.doc.folder_changed"
    """A folder of the Documents tree changed its look or its name, or was deleted (block-editor
    spec §5.2). Carries ``path``, ``previous_path`` (set when it was renamed), ``author``, and
    ``deleted`` when it went with everything in it. Its own type rather than a
    ``team.doc.changed`` with a folder in it, because ``path`` there is a document a listener may
    go and read. The documents a rename rewrote arrive as ``team.doc.changed``; the documents a
    delete took do not arrive one by one."""

    TEAM_CHAT_UPDATED = "team.chat.updated"
    """A chat with a team member was created or changed — its title, its running state, its
    totals (``teams/chat.py``). Carries the whole chat as ``GET /api/chats/{id}`` returns it
    minus the messages, so a client renders the state and a missed frame is corrected by the
    next one."""

    TEAM_CHAT_DELETED = "team.chat.deleted"
    TEAM_CHAT_MESSAGE = "team.chat.message"
    """One transcript entry of a chat — the same shape as ``team.member.message`` with
    ``chat_id`` in place of ``task_id``."""

    # Explorer
    EXPLORER_FILE_CHANGED = "explorer.file_changed"

    # Plugins
    PLUGIN_CHANGED = "plugin.changed"
    PLUGIN_EVENT = "plugin.event"
    """Something a plugin's own backend code said, through ``PluginHost.publish`` (§9.7).
    Carries ``plugin_id``, the plugin's ``name`` for the event and its ``data``. One type
    for every plugin rather than a type per plugin: the taxonomy is the Studio's, and a
    plugin's frontend filters on the two fields it knows."""

    # Automations (§6.5)
    AUTOMATION_CHANGED = "automation.changed"
    """``.voxarium_studio/automations.yaml`` was written through the API — a client re-reads the page.
    A hand edit arrives as ``explorer.file_changed`` for the file instead."""

    AUTOMATION_FIRED = "automation.fired"
    """An automation's trigger matched and its action ran, failed or was refused — or, with
    ``status: running``, is still at it. Carries the log row as ``GET /api/automations`` lists
    it."""

    AUTOMATION_RUN_UPDATED = "automation.run_updated"
    """A row that was in progress ended — the same ``id``, now ``ok``, ``error`` or
    ``cancelled``. A client puts it where the running row was."""

    AUTOMATION_NOTIFICATION = "automation.notification"
    """The *notify* action. ``title`` and ``message`` are the automation's own texts with
    their placeholders filled, either possibly empty; ``trigger`` and ``values`` are what a
    client words a default from, in its own language; ``route`` is where a click leads."""

    # The Studio updating itself (§12.1)
    UPDATE_STATUS = "update.status"
    """Everything ``GET /api/updates`` returns, whenever any of it changes — a check
    starting or finishing, a newer release appearing, a step of an update beginning. One
    event carrying the whole answer rather than a verb per transition: the client renders
    the state, and a missed frame is corrected by the next one instead of being replayed."""

    UPDATE_LOG = "update.log"
    """One line from the update's transcript, as the command it came from prints it."""

    # Workspace
    PROJECT_OPENED = "project.opened"
    """The Studio was pointed at a different managed project. Every client's view of
    every section is now stale, so this is a reload, not a patch."""

    # Transport-level
    HELLO = "system.hello"
    DROPPED = "system.dropped"
    """Emitted to a single slow subscriber whose queue overflowed — resync via REST."""

    PONG = "system.pong"
    """Reply to a client ``ping``. Sent straight down the socket, so it carries no ``seq``."""
