"""Career role taxonomy: fields, their sub-roles and title synonyms.

A user picks a field (e.g. Cybersecurity) or a specific sub-role (e.g. cybersecurity:soc) and the taxonomy
expands that selection into the set of job-title keywords that cover it, so the filter and the heuristic
scorer catch every title variant without the user listing them by hand. Pure, data-only, no DB.
"""

from __future__ import annotations

from jobapply.roles.taxonomy import (
    TAXONOMY,
    Field,
    SubRole,
    all_fields,
    field,
    match_title,
    subrole,
    titles_for,
)

__all__ = [
    "TAXONOMY",
    "Field",
    "SubRole",
    "all_fields",
    "field",
    "match_title",
    "subrole",
    "titles_for",
]
