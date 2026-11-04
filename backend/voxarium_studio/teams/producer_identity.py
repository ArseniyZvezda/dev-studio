"""The reserved, editable Producer member of each team (§10.11)."""

PRODUCER_ID = "studio-producer"


def is_producer(member) -> bool:
    return member.id == f"{member.team_id}__{PRODUCER_ID}"
