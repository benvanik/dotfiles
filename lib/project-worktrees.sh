#!/bin/bash
# Shared Git worktree conventions for project-* commands.

# Prints typed local entries shared from the primary worktree into siblings.
# Each line is <kind><tab><path>. Creation validates the source according to
# its kind; removal consumes the same list so managed links have one ownership
# definition.
project_worktree_shared_entries() {
    printf '%s\t%s\n' \
        "file" "AGENTS.override.md" \
        "file" ".bazelrc.local" \
        "directory" ".beads"
}

# Retains the original file-only query for callers that do not manage links.
project_worktree_shared_files() {
    local shared_kind
    local shared_path

    while IFS=$'\t' read -r shared_kind shared_path; do
        if [ "$shared_kind" = "file" ]; then
            printf '%s\n' "$shared_path"
        fi
    done < <(project_worktree_shared_entries)
}

# Prints the primary worktree in the required <project>/main layout.
#
# The primary directory remains the repository anchor even while it checks out
# a release or integration branch. Secondary worktrees have a .git file;
# the primary worktree owns the ordinary .git directory.
project_main_worktree() {
    local repository_dir="$1"
    local current_worktree=""
    local line

    while IFS= read -r -d '' line; do
        case "$line" in
            "worktree "*)
                current_worktree="${line#worktree }"
                ;;
            "")
                if [ "$(basename "$current_worktree")" = "main" ] &&
                        [ -d "$current_worktree/.git" ] &&
                        [ ! -L "$current_worktree/.git" ]; then
                    printf '%s\n' "$current_worktree"
                    return 0
                fi
                current_worktree=""
                ;;
        esac
    done < <(git -C "$repository_dir" worktree list --porcelain -z)

    return 1
}

# Prints a stable short digest for the physical identity of a directory.
project_path_digest() {
    local directory="$1"
    local digest

    if ! digest=$(
        python3 - "$directory" 2>/dev/null << 'PY'
import hashlib
import os
import sys

physical_path = os.path.realpath(os.fsencode(sys.argv[1]))
if not os.path.isdir(physical_path):
    raise SystemExit(1)
sys.stdout.write(hashlib.sha256(physical_path).hexdigest()[:12])
PY
    ); then
        printf 'Could not derive physical project identity: %s\n' \
            "$directory" >&2
        return 1
    fi
    printf '%s\n' "$digest"
}

project_safe_session_component() {
    local component="$1"
    component="${component//[^A-Za-z0-9_.-]/_}"
    if [ -z "$component" ]; then
        component="project"
    fi
    printf '%s\n' "$component"
}

# A colon is legal in a tmux session name but is always parsed as the
# session/window delimiter in a target expression, including an exact target.
project_session_name_is_target_safe() {
    [ -n "$1" ] && [[ "$1" != *:* ]]
}

# Prints a stable tmux session name for a directory. Sibling worktrees whose
# primary checkout is named main use <project>-<digest>-<worktree>. Ordinary
# repositories and directories use <name>-<digest>. The physical-root digest
# prevents identically named projects in different paths from sharing a tmux
# target; the worktree suffix keeps every sibling distinct inside one project.
project_default_session_name() {
    local target_dir="$1"
    local worktree_dir
    local main_worktree
    local project_digest
    local project_name
    local project_root
    local worktree_name

    if ! worktree_dir=$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null); then
        project_name=$(project_safe_session_component "$(basename "$target_dir")")
        project_digest=$(project_path_digest "$target_dir") || return 1
        printf '%s-%s\n' "$project_name" "$project_digest"
        return 0
    fi

    if ! main_worktree=$(project_main_worktree "$worktree_dir"); then
        project_name=$(project_safe_session_component "$(basename "$worktree_dir")")
        project_digest=$(project_path_digest "$worktree_dir") || return 1
        printf '%s-%s\n' "$project_name" "$project_digest"
        return 0
    fi

    worktree_name=$(
        project_safe_session_component "$(basename "$worktree_dir")"
    )
    if [ "$(basename "$main_worktree")" = "main" ]; then
        project_root=$(dirname "$main_worktree")
        project_name=$(
            project_safe_session_component "$(basename "$project_root")"
        )
    else
        project_root="$main_worktree"
        project_name=$(
            project_safe_session_component "$(basename "$main_worktree")"
        )
    fi
    project_digest=$(project_path_digest "$project_root") || return 1

    printf '%s-%s-%s\n' "$project_name" "$project_digest" "$worktree_name"
}

# Prints a tmux target that requires an exact session-name match. Without the
# leading equals sign, tmux falls through from exact matching to prefix and
# glob matching, which can select another project's session.
project_exact_session_target() {
    printf '=%s\n' "$1"
}
