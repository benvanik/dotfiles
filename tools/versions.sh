# shellcheck shell=bash
# Semantic version utilities.
# Supports version comparison and requirement matching.
# Uses 'local' which is supported by bash, zsh, and dash.

# Compare versions: returns 0 if $1 >= $2.
_version_gte() {
    local v1="$1" v2="$2"

    # Extract major.minor.patch components.
    local v1_major v1_minor v1_patch
    local v2_major v2_minor v2_patch

    v1_major="${v1%%.*}"
    v1_minor="${v1#*.}"
    v1_minor="${v1_minor%%.*}"
    v1_patch="${v1##*.}"
    # Strip prerelease suffixes (e.g., 0a20251127 -> 0).
    v1_patch="${v1_patch%%[!0-9]*}"

    v2_major="${v2%%.*}"
    v2_minor="${v2#*.}"
    v2_minor="${v2_minor%%.*}"
    v2_patch="${v2##*.}"
    # Strip prerelease suffixes.
    v2_patch="${v2_patch%%[!0-9]*}"

    # Default missing components to 0.
    : "${v1_major:=0}" "${v1_minor:=0}" "${v1_patch:=0}"
    : "${v2_major:=0}" "${v2_minor:=0}" "${v2_patch:=0}"

    # Compare numerically.
    [ "$v1_major" -gt "$v2_major" ] && return 0
    [ "$v1_major" -lt "$v2_major" ] && return 1
    [ "$v1_minor" -gt "$v2_minor" ] && return 0
    [ "$v1_minor" -lt "$v2_minor" ] && return 1
    [ "$v1_patch" -ge "$v2_patch" ] && return 0
    return 1
}

# Find best version matching requirement.
# Args: tool_dir, requirement (>=X.Y.Z, X.Y.Z, or "latest").
# Returns: full path to version directory.
_find_version() {
    local tool_dir="$1" requirement="$2"

    # Handle "latest" symlink.
    if [ "$requirement" = "latest" ]; then
        if [ -L "$tool_dir/latest" ]; then
            readlink -f "$tool_dir/latest"
            return 0
        else
            # No latest symlink; fall through to find highest version.
            requirement=">="
        fi
    fi

    # Handle exact version.
    if [ -d "$tool_dir/$requirement" ]; then
        echo "$tool_dir/$requirement"
        return 0
    fi

    # Handle >=X.Y.Z requirement - find highest matching.
    if [ "${requirement#>=}" != "$requirement" ]; then
        local min_version="${requirement#>=}"
        local best_version=""
        local best_path=""

        for dir in "$tool_dir"/*/; do
            [ -d "$dir" ] || continue
            local ver
            ver="$(basename "$dir")"

            # Skip non-version directories.
            case "$ver" in
                [0-9]*) ;;
                *) continue ;;
            esac

            # Check if version meets minimum requirement.
            if _version_gte "$ver" "$min_version"; then
                # Track highest matching version.
                if [ -z "$best_version" ] || _version_gte "$ver" "$best_version"; then
                    best_version="$ver"
                    best_path="${dir%/}"
                fi
            fi
        done

        if [ -n "$best_path" ]; then
            echo "$best_path"
            return 0
        fi
    fi

    return 1
}

# Get version string from path (just the directory name).
_version_from_path() {
    basename "$1"
}
