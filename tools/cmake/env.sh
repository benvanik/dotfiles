# CMake environment.
# Sourced by tools.sh and direnvrc after CMAKE_ROOT is selected.
# Linux archives expose bin/ at their root. The supported macOS universal
# archive preserves the application bundle and exposes the CLI beneath
# CMake.app/Contents/bin.
if [ -n "${CMAKE_ROOT:-}" ]; then
    if [ -x "$CMAKE_ROOT/bin/cmake" ]; then
        _cmake_executable_directory="$CMAKE_ROOT/bin"
    elif [ -x "$CMAKE_ROOT/CMake.app/Contents/bin/cmake" ]; then
        _cmake_executable_directory="$CMAKE_ROOT/CMake.app/Contents/bin"
    else
        printf 'CMake root is incomplete: %s\n' "$CMAKE_ROOT" >&2
        return 1
    fi

    _replace_managed_path_entry \
        PATH "$_cmake_executable_directory" DOTFILES_CMAKE_PATH_ENTRY ||
        return 1
    unset _cmake_executable_directory
fi
