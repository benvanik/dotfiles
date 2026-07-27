"""Unix ownership predicates shared by model-session trust boundaries."""

from __future__ import annotations

import grp
import os
import pwd


def owner_has_private_primary_group(metadata: os.stat_result) -> bool:
    """Whether an object's owning group contains only its owning user."""

    try:
        owner = pwd.getpwuid(metadata.st_uid)
        group = grp.getgrgid(metadata.st_gid)
        primary_members = {
            entry.pw_name
            for entry in pwd.getpwall()
            if entry.pw_gid == metadata.st_gid
        }
    except KeyError:
        return False
    return (
        owner.pw_gid == metadata.st_gid
        and primary_members | set(group.gr_mem) == {owner.pw_name}
    )
