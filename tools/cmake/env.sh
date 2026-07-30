# CMake environment.
# Sourced by tools.sh and direnvrc after CMAKE_ROOT is selected.
# shellcheck disable=SC2154
if [ -n "$CMAKE_ROOT" ]; then
    export PATH="$CMAKE_ROOT/bin:$PATH"
fi
