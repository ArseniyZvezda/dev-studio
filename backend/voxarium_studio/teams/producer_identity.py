"""The reserved Producer member, whose responsibility comes from Settings (§10.11)."""

PRODUCER_ID = "studio-producer"


def is_producer(member) -> bool:
    return member.id == f"{member.team_id}__{PRODUCER_ID}"
