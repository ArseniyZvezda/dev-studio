"""The reserved human Director role at the head of each team (§10.2)."""

DIRECTOR_ID = "director"


def is_director(member) -> bool:
    return member.id == f"{member.team_id}__{DIRECTOR_ID}"
