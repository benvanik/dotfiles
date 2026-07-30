#!/bin/bash
# Shared Git worktree conventions for project-* commands.

# Prints the path of the worktree currently holding the local main branch.
project_main_worktree() {
    local repository_dir="$1"
    local current_worktree=""
    local line

    while IFS= read -r line; do
        case "$line" in
            "worktree "*)
                current_worktree="${line#worktree }"
                ;;
            "branch refs/heads/main")
                printf '%s\n' "$current_worktree"
                return 0
                ;;
        esac
    done < <(git -C "$repository_dir" worktree list --porcelain)

    return 1
}

# Prints a stable tmux session name for a directory. Sibling worktrees whose
# primary checkout is named main use <project>-<worktree>; ordinary checkouts
# retain their directory basename.
project_default_session_name() {
    local target_dir="$1"
    local worktree_dir
    local main_worktree
    local project_name
    local worktree_name

    if ! worktree_dir=$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null); then
        basename "$target_dir"
        return 0
    fi

    if ! main_worktree=$(project_main_worktree "$worktree_dir"); then
        basename "$worktree_dir"
        return 0
    fi

    worktree_name=$(basename "$worktree_dir")
    if [ "$(basename "$main_worktree")" = "main" ]; then
        project_name=$(basename "$(dirname "$main_worktree")")
    else
        project_name=$(basename "$main_worktree")
    fi

    case "$worktree_name" in
        "$project_name"|"$project_name"-*)
            printf '%s\n' "$worktree_name"
            ;;
        *)
            printf '%s-%s\n' "$project_name" "$worktree_name"
            ;;
    esac
}
