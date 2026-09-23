"""Translate a principal's grants into a parameterized SQL row filter over `accounts a`.

Design guarantees:
  * Fails closed: a principal with no grants sees nothing (predicate = FALSE).
  * Union semantics: multiple grants are OR-ed (a caller sees the union of their scopes).
  * `owned_accounts` is resolved AS-OF the query date against account_ownership, so a rep
    sees the book they owned on that date, not today's book.
  * Every scope value is bound as a parameter -- never string-interpolated -- so this layer
    cannot be used for SQL injection even though grants come from trusted tables.
"""

from __future__ import annotations

from .principal import Principal

_SIMPLE_COLUMN = {
    "region": "a.region",
    "sub_region": "a.sub_region",
    "segment": "a.segment",
    "country": "a.country",
    "account": "a.account_id",
}


def build_rls_predicate(principal: Principal, as_of: str) -> tuple[str, list]:
    """Return (sql_predicate, params). The predicate references alias `a` (accounts)."""
    if principal.is_unrestricted:
        return "TRUE", []
    if not principal.grants:
        return "FALSE", []  # fail closed

    clauses: list[str] = []
    params: list = []
    for g in principal.grants:
        if g.scope_type in _SIMPLE_COLUMN:
            clauses.append(f"{_SIMPLE_COLUMN[g.scope_type]} = ?")
            params.append(g.scope_value)
        elif g.scope_type == "owned_accounts":
            clauses.append(
                "a.account_id IN (SELECT o.account_id FROM account_ownership o "
                "WHERE o.owner_user_id = ? AND o.valid_from <= ? "
                "AND (o.valid_to IS NULL OR o.valid_to > ?))")
            params.extend([g.scope_value, as_of, as_of])
        elif g.scope_type == "all":
            return "TRUE", []
        else:
            # unknown scope type -> contributes nothing (fail closed for this grant)
            continue

    if not clauses:
        return "FALSE", []
    return "(" + " OR ".join(clauses) + ")", params
