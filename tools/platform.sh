# shellcheck shell=bash
# Platform detection utilities.
# Sourced by tools.sh and direnvrc.
# Uses 'local' which is supported by bash, zsh, and dash.

_detect_os() {
    case "$(uname -s)" in
        Linux)
            if grep -qi microsoft /proc/version 2>/dev/null; then
                echo "wsl"
            else
                echo "linux"
            fi
            ;;
        Darwin) echo "darwin" ;;
        *) echo "unknown" ;;
    esac
}

_detect_arch() {
    case "$(uname -m)" in
        x86_64|amd64) echo "x86_64" ;;
        aarch64|arm64) echo "arm64" ;;
        *) echo "unknown" ;;
    esac
}

# Check whether a managed tool publishes an artifact for one native host target.
# Architecture spelling is normalized here because install-utils.sh exposes
# aarch64 while the shell activation layer exposes arm64.
_tool_artifact_supported() {
    local tool="$1"
    local platform="$2"
    local architecture="$3"

    case "$architecture" in
        arm64) architecture="aarch64" ;;
    esac

    case "$tool" in
        bazel|beads|cmake|hf|ninja|nvm)
            case "$platform/$architecture" in
                linux/x86_64|linux/aarch64|darwin/x86_64|darwin/aarch64)
                    return 0
                    ;;
                *) return 1 ;;
            esac
            ;;
        cuda|mold)
            case "$platform/$architecture" in
                linux/x86_64|linux/aarch64) return 0 ;;
                *) return 1 ;;
            esac
            ;;
        llvm|nix)
            case "$platform/$architecture" in
                linux/x86_64|linux/aarch64|darwin/aarch64)
                    return 0
                    ;;
                *) return 1 ;;
            esac
            ;;
        rocm)
            [ "$platform/$architecture" = "linux/x86_64" ]
            ;;
        vulkan)
            [ "$platform/$architecture" = "linux/x86_64" ]
            ;;
        *) return 1 ;;
    esac
}

# Check whether a managed tool may be activated on this host. WSL consumes
# selected Linux artifacts, but CUDA, ROCm, and mold remain disabled there by
# policy. Unsupported project selections are silent so one committed .envrc
# remains portable across machines.
_platform_supports() {
    local tool="$1"
    local platform
    local architecture

    platform="$(_detect_os)"
    architecture="$(_detect_arch)"
    if [ "$platform" = "wsl" ]; then
        case "$tool" in
            cuda|mold|rocm) return 1 ;;
        esac
        platform="linux"
    fi
    _tool_artifact_supported "$tool" "$platform" "$architecture"
}

# Validate names before using eval for dynamic environment variables.
_validate_environment_variable_name() {
    local variable_name="${1:-}"

    case "$variable_name" in
        ''|[0-9]*|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_]*)
            printf 'Invalid environment variable name: %s\n' \
                "$variable_name" >&2
            return 1
            ;;
    esac
}

_validate_path_entry() {
    local variable_name="${1:-}"
    local entry="${2:-}"

    _validate_environment_variable_name "$variable_name" || return 1
    case "$entry" in
        '')
            printf 'Cannot use an empty path entry for %s\n' \
                "$variable_name" >&2
            return 1
            ;;
        *:*)
            printf 'Path entry for %s contains a colon: %s\n' \
                "$variable_name" "$entry" >&2
            return 1
            ;;
    esac
}

# Remove every exact entry from a colon-delimited environment variable while
# preserving all unrelated and empty components. If the removed entries were
# the complete nonempty value, the variable becomes unset.
_remove_path_entry() {
    local variable_name="${1:-}"
    local entry="${2:-}"
    local variable_set=""
    local current=""
    local remaining=""
    local component=""
    local rebuilt=""
    local rebuilt_set=0
    local has_more=0

    _validate_path_entry "$variable_name" "$entry" || return 1
    eval "variable_set=\${$variable_name+x}"
    [ "$variable_set" = "x" ] || return 0

    eval "current=\${$variable_name-}"
    remaining="$current"
    while :; do
        case "$remaining" in
            *:*)
                component="${remaining%%:*}"
                remaining="${remaining#*:}"
                has_more=1
                ;;
            *)
                component="$remaining"
                has_more=0
                ;;
        esac

        if [ "$component" != "$entry" ]; then
            if [ "$rebuilt_set" -eq 1 ]; then
                rebuilt="$rebuilt:$component"
            else
                rebuilt="$component"
                rebuilt_set=1
            fi
        fi

        [ "$has_more" -eq 1 ] || break
    done

    if [ "$rebuilt_set" -eq 1 ]; then
        export "$variable_name=$rebuilt"
    else
        unset "$variable_name"
    fi
}

# Move one exact entry to the front of a colon-delimited environment variable.
# Tool environments may be applied by an interactive shell and again by
# direnv. Rebuilding the list makes that repeated activation idempotent while
# retaining unrelated entries, including explicit empty entries with PATH
# semantics.
_prepend_path_entry() {
    local variable_name="${1:-}"
    local entry="${2:-}"
    local variable_set=""
    local current=""
    local remaining=""
    local component=""
    local rebuilt=""
    local rebuilt_set=0
    local has_more=0

    _validate_path_entry "$variable_name" "$entry" || return 1
    eval "variable_set=\${$variable_name+x}"
    if [ "$variable_set" != "x" ]; then
        export "$variable_name=$entry"
        return 0
    fi

    eval "current=\${$variable_name-}"
    remaining="$current"
    while :; do
        case "$remaining" in
            *:*)
                component="${remaining%%:*}"
                remaining="${remaining#*:}"
                has_more=1
                ;;
            *)
                component="$remaining"
                has_more=0
                ;;
        esac

        if [ "$component" != "$entry" ]; then
            if [ "$rebuilt_set" -eq 1 ]; then
                rebuilt="$rebuilt:$component"
            else
                rebuilt="$component"
                rebuilt_set=1
            fi
        fi

        [ "$has_more" -eq 1 ] || break
    done

    if [ "$rebuilt_set" -eq 1 ]; then
        export "$variable_name=$entry:$rebuilt"
    else
        export "$variable_name=$entry"
    fi
}

# Replace the exact path entry previously owned by one tool activation. The
# exported state lets nested shells and later explicit selections remove the
# superseded version without disturbing unrelated search paths.
_replace_managed_path_entry() {
    local variable_name="${1:-}"
    local entry="${2:-}"
    local state_variable="${3:-}"
    local previous_entry=""

    _validate_path_entry "$variable_name" "$entry" || return 1
    _validate_environment_variable_name "$state_variable" || return 1
    eval "previous_entry=\${$state_variable-}"
    if [ -n "$previous_entry" ] && [ "$previous_entry" != "$entry" ]; then
        _remove_path_entry "$variable_name" "$previous_entry" || return 1
    fi
    _prepend_path_entry "$variable_name" "$entry" || return 1
    export "$state_variable=$entry"
}

# Remove a tool-owned path slot when the selected layout no longer uses it.
_clear_managed_path_entry() {
    local variable_name="${1:-}"
    local state_variable="${2:-}"
    local previous_entry=""

    _validate_environment_variable_name "$variable_name" || return 1
    _validate_environment_variable_name "$state_variable" || return 1
    eval "previous_entry=\${$state_variable-}"
    if [ -n "$previous_entry" ]; then
        _remove_path_entry "$variable_name" "$previous_entry" || return 1
    fi
    unset "$state_variable"
}
