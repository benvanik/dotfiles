#!/bin/bash
# Worktree-ready Git repository bootstrap for project-init.
# shellcheck disable=SC2034  # Output globals are consumed by sourcing callers.

# Classifies a directory without mutating it. Results are returned through:
#   PROJECT_REPOSITORY_CLASSIFICATION
#     absent          No repository owns the directory and no .git path exists.
#     primary-unborn  A <project>/main primary worktree without its first commit.
#     primary-ready   A <project>/main primary worktree with a commit.
#     other           A repository or worktree outside the primary layout.
#     invalid         A damaged or unsupported repository boundary.
#   PROJECT_REPOSITORY_PHYSICAL_DIRECTORY
project_repository_classify() {
    local directory="$1"
    local head_reference=""
    local physical_directory=""
    local repository_root=""

    PROJECT_REPOSITORY_CLASSIFICATION="invalid"
    PROJECT_REPOSITORY_PHYSICAL_DIRECTORY=""

    physical_directory=$(
        CDPATH=''
        cd -- "$directory" 2>/dev/null || exit 1
        pwd -P
    ) || return 1
    PROJECT_REPOSITORY_PHYSICAL_DIRECTORY="$physical_directory"

    if ! repository_root=$(
        git -C "$physical_directory" rev-parse --show-toplevel 2>/dev/null
    ); then
        if [ -e "$physical_directory/.git" ] ||
                [ -L "$physical_directory/.git" ]; then
            PROJECT_REPOSITORY_CLASSIFICATION="invalid"
        else
            PROJECT_REPOSITORY_CLASSIFICATION="absent"
        fi
        return 0
    fi
    repository_root=$(
        CDPATH=''
        cd -- "$repository_root" 2>/dev/null || exit 1
        pwd -P
    ) || return 1
    if [ "$repository_root" != "$physical_directory" ] ||
            [ "$(basename "$physical_directory")" != "main" ] ||
            [ ! -d "$physical_directory/.git" ] ||
            [ -L "$physical_directory/.git" ]; then
        PROJECT_REPOSITORY_CLASSIFICATION="other"
        return 0
    fi

    if git -C "$physical_directory" rev-parse --verify --quiet \
            'HEAD^{commit}' >/dev/null; then
        PROJECT_REPOSITORY_CLASSIFICATION="primary-ready"
        return 0
    fi
    if ! head_reference=$(
        git -C "$physical_directory" symbolic-ref --quiet HEAD 2>/dev/null
    ) || [ "$head_reference" != "refs/heads/main" ]; then
        PROJECT_REPOSITORY_CLASSIFICATION="invalid"
        return 0
    fi

    PROJECT_REPOSITORY_CLASSIFICATION="primary-unborn"
}

# An initializer must not fold caller-staged content into its generated first
# commit. Untracked working files remain caller-owned and are left untouched.
project_repository_require_empty_index() {
    local directory="$1"
    local staged_paths=""

    staged_paths=$(git -C "$directory" ls-files --stage) || return 1
    if [ -n "$staged_paths" ]; then
        printf '%s\n' \
            "The unborn repository already contains staged files: $directory" >&2
        return 1
    fi
}

# Rechecks the repository generation captured before interactive selection.
project_repository_require_bootstrap_state() {
    local directory="$1"
    local expected_classification="$2"

    project_repository_classify "$directory" || return 1
    if [ "$PROJECT_REPOSITORY_CLASSIFICATION" != \
            "$expected_classification" ]; then
        printf '%s\n' \
            "Repository state changed during project initialization: $directory" >&2
        return 1
    fi
    if [ "$expected_classification" = "primary-unborn" ]; then
        project_repository_require_empty_index "$directory" || return 1
    fi
}

# Creates the primary repository boundary but does not stage project files.
project_repository_initialize_main() {
    local directory="$1"

    if ! mkdir -- "$directory/.git"; then
        printf 'Could not claim the Git repository boundary: %s\n' \
            "$directory/.git" >&2
        return 1
    fi
    if ! git -C "$directory" init --quiet --initial-branch=main; then
        printf 'Could not initialize the Git repository: %s\n' \
            "$directory" >&2
        return 1
    fi
    project_repository_classify "$directory" || return 1
    if [ "$PROJECT_REPOSITORY_CLASSIFICATION" != "primary-unborn" ]; then
        printf 'Git did not create a primary unborn main worktree: %s\n' \
            "$directory" >&2
        return 1
    fi
}

# Creates the commit required before Git can add sibling worktrees. Only the
# caller-provided generated paths are staged; other working files stay local.
project_repository_create_initial_commit() {
    local directory="$1"
    shift

    if [ $# -eq 0 ]; then
        printf 'No generated project files were provided for the first commit\n' >&2
        return 1
    fi
    project_repository_classify "$directory" || return 1
    if [ "$PROJECT_REPOSITORY_CLASSIFICATION" != "primary-unborn" ]; then
        printf 'The primary repository is no longer waiting for its first commit: %s\n' \
            "$directory" >&2
        return 1
    fi
    project_repository_require_empty_index "$directory" || return 1
    if ! git -C "$directory" add -- "$@"; then
        printf 'Could not stage generated project files in: %s\n' \
            "$directory" >&2
        return 1
    fi
    project_repository_classify "$directory" || return 1
    if [ "$PROJECT_REPOSITORY_CLASSIFICATION" != "primary-unborn" ]; then
        printf '%s\n' \
            "Repository state changed while staging the initial project files." \
            "Generated files remain staged for inspection." >&2
        return 1
    fi
    if ! git -C "$directory" commit --quiet -m "Initialize project"; then
        printf '%s\n' \
            "Could not create the initial project commit in: $directory" \
            "Generated files remain staged for inspection or a manual commit." >&2
        return 1
    fi
    if ! git -C "$directory" show-ref --verify --quiet refs/heads/main; then
        printf 'The initial commit did not publish refs/heads/main: %s\n' \
            "$directory" >&2
        return 1
    fi
}

# Plans bootstrap at the unambiguous <project>/main boundary. Existing
# repositories and nested directories retain environment-only behavior unless
# mode is "enabled". Outputs are returned through:
#   PROJECT_REPOSITORY_EXPECTED_CLASSIFICATION
#   PROJECT_REPOSITORY_INITIALIZE
#   PROJECT_REPOSITORY_CREATE_COMMIT
project_repository_prepare_bootstrap() {
    local directory="$1"
    local mode="$2"
    local environment_enabled="$3"

    PROJECT_REPOSITORY_EXPECTED_CLASSIFICATION=""
    PROJECT_REPOSITORY_INITIALIZE=false
    PROJECT_REPOSITORY_CREATE_COMMIT=false

    case "$mode" in
        auto|enabled|disabled) ;;
        *)
            printf 'Unknown repository bootstrap mode: %s\n' "$mode" >&2
            return 1
            ;;
    esac
    if [ "$environment_enabled" != "true" ] || [ "$mode" = "disabled" ]; then
        return 0
    fi
    if ! project_repository_classify "$directory"; then
        printf 'Could not inspect the target repository boundary: %s\n' \
            "$directory" >&2
        return 1
    fi
    if ! command -v git >/dev/null 2>&1; then
        if [ "$mode" = "enabled" ] ||
                [ "$(basename "$PROJECT_REPOSITORY_PHYSICAL_DIRECTORY")" = \
                    "main" ]; then
            printf '%s\n' \
                "Git is required to bootstrap a primary project worktree." >&2
            return 1
        fi
        return 0
    fi

    case "$PROJECT_REPOSITORY_CLASSIFICATION" in
        absent)
            if [ "$(basename "$PROJECT_REPOSITORY_PHYSICAL_DIRECTORY")" != \
                    "main" ]; then
                if [ "$mode" = "enabled" ]; then
                    printf '%s\n' \
                        "A primary project worktree must be named 'main'." >&2
                    return 1
                fi
                return 0
            fi
            PROJECT_REPOSITORY_EXPECTED_CLASSIFICATION="absent"
            PROJECT_REPOSITORY_INITIALIZE=true
            PROJECT_REPOSITORY_CREATE_COMMIT=true
            ;;
        primary-unborn)
            if ! project_repository_require_empty_index "$directory"; then
                printf '%s\n' \
                    "Refusing to mix staged caller content into the initial commit." >&2
                return 1
            fi
            PROJECT_REPOSITORY_EXPECTED_CLASSIFICATION="primary-unborn"
            PROJECT_REPOSITORY_CREATE_COMMIT=true
            ;;
        primary-ready)
            return 0
            ;;
        other)
            if [ "$mode" = "enabled" ]; then
                printf '%s\n' \
                    "The target is not the primary <project>/main Git worktree." >&2
                return 1
            fi
            return 0
            ;;
        invalid)
            if [ "$mode" = "enabled" ] ||
                    [ "$(basename "$PROJECT_REPOSITORY_PHYSICAL_DIRECTORY")" = \
                        "main" ]; then
                printf '%s\n' \
                    "The target has an invalid or damaged Git repository boundary." >&2
                return 1
            fi
            return 0
            ;;
        *)
            printf 'Unknown repository classification: %s\n' \
                "$PROJECT_REPOSITORY_CLASSIFICATION" >&2
            return 1
            ;;
    esac

    if ! git -C "$directory" var GIT_AUTHOR_IDENT >/dev/null 2>&1 ||
            ! git -C "$directory" var GIT_COMMITTER_IDENT >/dev/null 2>&1; then
        printf '%s\n' \
            "Git author and committer identity must be configured before bootstrap." >&2
        return 1
    fi
}

# Publishes the planned repository and its first commit from the generated
# paths. PROJECT_REPOSITORY_COMMIT receives the abbreviated commit identity.
project_repository_publish_bootstrap() {
    local directory="$1"
    local initialize="$2"
    shift 2

    if [ "$initialize" = "true" ]; then
        project_repository_initialize_main "$directory" || return 1
    fi
    project_repository_create_initial_commit "$directory" "$@" || return 1
    PROJECT_REPOSITORY_COMMIT=$(
        git -C "$directory" rev-parse --short=12 HEAD
    ) || return 1
}
