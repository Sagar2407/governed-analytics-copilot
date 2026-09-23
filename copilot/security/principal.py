"""A Principal is a caller plus their resolved entitlement grants.

Principals are established server-side from the `access_map` / `personas` tables -- never
from user input or the LLM. The compiler turns a principal's grants into an injected row
filter, so what a caller can see is decided by data + code, not by the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Grant:
    scope_type: str   # all | region | sub_region | segment | country | account | owned_accounts
    scope_value: str


@dataclass
class Principal:
    user_id: str
    label: str = ""
    grants: list[Grant] = field(default_factory=list)

    @property
    def is_unrestricted(self) -> bool:
        return any(g.scope_type == "all" for g in self.grants)


def load_principal(con: Any, user_id: str, label: str = "") -> Principal:
    """Resolve a user's grants from access_map."""
    rows = con.execute(
        "SELECT scope_type, scope_value FROM access_map WHERE user_id = ?",
        [user_id],
    ).fetchall()
    grants = [Grant(scope_type=r[0], scope_value=r[1]) for r in rows]
    return Principal(user_id=user_id, label=label, grants=grants)


def load_persona(con: Any, persona_id: str) -> Principal:
    """Resolve a demo persona -> its user's grants."""
    row = con.execute(
        "SELECT user_id, label FROM personas WHERE persona_id = ?", [persona_id]
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown persona {persona_id!r}")
    user_id, label = row
    p = load_principal(con, user_id, label=label)
    return p


def list_personas(con: Any) -> list[dict]:
    df = con.execute(
        "SELECT persona_id, label, description, user_id, scope_summary, is_default "
        "FROM personas ORDER BY is_default DESC, persona_id"
    ).fetchdf()
    return df.to_dict(orient="records")
