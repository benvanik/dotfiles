# Ninja environment.
# Sourced by tools.sh and direnvrc after NINJA_ROOT is selected.
# shellcheck disable=SC2154
if [ -n "$NINJA_ROOT" ]; then
    export PATH="$NINJA_ROOT/bin:$PATH"
fi
