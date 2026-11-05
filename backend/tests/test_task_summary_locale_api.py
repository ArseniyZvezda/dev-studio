"""A reader's language is saved with the task rather than the managed project (§10.4)."""

import pytest


TEAM = "team_default"
DEV = f"{TEAM}__dev"
TASKS_URL = f"/api/teams/{TEAM}/tasks"


def test_task_summary_language_survives_reads_and_changes_on_release(client) -> None:
    created = client.post(TASKS_URL, json={
        "assignee_id": DEV, "title": "Task summary", "auto_start": False,
        "summary_locale": "ru",
    })
    assert created.status_code == 201, created.text
    task = created.json()
    task_url = f"{TASKS_URL}/{task['id']}"
    assert task["summary_locale"] == "ru"
    assert client.get(task_url).json()["summary_locale"] == "ru"
    assert client.get(TASKS_URL).json()[0]["summary_locale"] == "ru"

    edited = client.patch(task_url, json={"title": "Renamed task"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["summary_locale"] == "ru"
    released = client.patch(task_url, json={"status": "pending", "summary_locale": "en"})
    assert released.status_code == 200, released.text
    assert released.json()["summary_locale"] == "en"
    detail = client.get(task_url).json()
    assert detail["status"] == "pending"
    assert not detail["is_running"]
    assert all("summary_locale" not in row["data"].get("fields", {}) for row in detail["messages"])


def test_legacy_task_creation_leaves_summary_language_unspecified(client) -> None:
    created = client.post(TASKS_URL, json={"assignee_id": DEV, "title": "Legacy caller"})
    assert created.status_code == 201, created.text
    assert created.json()["summary_locale"] is None


@pytest.mark.parametrize("language", ["", "fr", "Russian", 1])
def test_unsupported_summary_language_is_rejected(client, language) -> None:
    created = client.post(TASKS_URL, json={
        "assignee_id": DEV, "title": "Invalid language", "summary_locale": language,
    })
    assert created.status_code == 422
    assert client.get(TASKS_URL).json() == []
