#!/bin/bash
# Offline production-path coverage for the CMake, LLVM, Ninja, Vulkan, Nix,
# and NVM installers.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-platform-installer.XXXXXX")
OUTPUT="$TEST_ROOT/output"

cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-platform-installer.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "platform installer test: $1" >&2
    if [ -s "$OUTPUT" ]; then
        sed 's/^/  installer output: /' "$OUTPUT" >&2
    fi
    exit 1
}

file_sha256() {
    local digest

    if command -v sha256sum >/dev/null 2>&1; then
        digest=$(sha256sum "$1")
    else
        digest=$(shasum -a 256 "$1")
    fi
    printf '%s\n' "${digest%% *}"
}

FIXTURE_VERSION=9.8.7
LLVM_FIXTURE_VERSION=21.8.7
VULKAN_FIXTURE_VERSION=9.8.7.6
ASSET_DIRECTORY="$TEST_ROOT/assets"
FAKE_BIN="$TEST_ROOT/bin"
TOOLS_ROOT="$TEST_ROOT/tools"
mkdir -p "$ASSET_DIRECTORY" "$FAKE_BIN"

case "$(uname -s)_$(uname -m)" in
    Linux_x86_64|Linux_amd64)
        CMAKE_ASSET="cmake-$FIXTURE_VERSION-linux-x86_64.tar.gz"
        CMAKE_ARCHIVE_ROOT="${CMAKE_ASSET%.tar.gz}"
        CMAKE_EXECUTABLE_ROOT="bin"
        LLVM_ASSET="LLVM-$LLVM_FIXTURE_VERSION-Linux-X64.tar.xz"
        NINJA_ASSET="ninja-linux.zip"
        RUN_LLVM_FIXTURE=true
        RUN_VULKAN_FIXTURE=true
        ;;
    Linux_aarch64|Linux_arm64)
        CMAKE_ASSET="cmake-$FIXTURE_VERSION-linux-aarch64.tar.gz"
        CMAKE_ARCHIVE_ROOT="${CMAKE_ASSET%.tar.gz}"
        CMAKE_EXECUTABLE_ROOT="bin"
        LLVM_ASSET="LLVM-$LLVM_FIXTURE_VERSION-Linux-ARM64.tar.xz"
        NINJA_ASSET="ninja-linux-aarch64.zip"
        RUN_LLVM_FIXTURE=true
        RUN_VULKAN_FIXTURE=false
        ;;
    Darwin_aarch64|Darwin_arm64)
        CMAKE_ASSET="cmake-$FIXTURE_VERSION-macos-universal.tar.gz"
        CMAKE_ARCHIVE_ROOT="${CMAKE_ASSET%.tar.gz}"
        CMAKE_EXECUTABLE_ROOT="CMake.app/Contents/bin"
        LLVM_ASSET="LLVM-$LLVM_FIXTURE_VERSION-macOS-ARM64.tar.xz"
        NINJA_ASSET="ninja-mac.zip"
        RUN_LLVM_FIXTURE=true
        RUN_VULKAN_FIXTURE=false
        ;;
    Darwin_x86_64|Darwin_amd64)
        CMAKE_ASSET="cmake-$FIXTURE_VERSION-macos-universal.tar.gz"
        CMAKE_ARCHIVE_ROOT="${CMAKE_ASSET%.tar.gz}"
        CMAKE_EXECUTABLE_ROOT="CMake.app/Contents/bin"
        LLVM_ASSET=""
        NINJA_ASSET="ninja-mac.zip"
        RUN_LLVM_FIXTURE=false
        RUN_VULKAN_FIXTURE=false
        ;;
    *) fail "host platform is outside the installer fixture matrix" ;;
esac

CMAKE_SOURCE="$TEST_ROOT/cmake-source/$CMAKE_ARCHIVE_ROOT"
mkdir -p "$CMAKE_SOURCE/$CMAKE_EXECUTABLE_ROOT"
for executable in cmake cpack ctest; do
    printf '#!/bin/sh\nprintf "cmake version %s\\n"\n' "$FIXTURE_VERSION" \
        > "$CMAKE_SOURCE/$CMAKE_EXECUTABLE_ROOT/$executable"
    chmod 755 "$CMAKE_SOURCE/$CMAKE_EXECUTABLE_ROOT/$executable"
done
tar czf "$ASSET_DIRECTORY/$CMAKE_ASSET" \
    -C "$TEST_ROOT/cmake-source" "$CMAKE_ARCHIVE_ROOT"
CMAKE_SHA256=$(file_sha256 "$ASSET_DIRECTORY/$CMAKE_ASSET")
CMAKE_CHECKSUM="$ASSET_DIRECTORY/cmake-$FIXTURE_VERSION-SHA-256.txt"
printf '%s  %s\n' "$CMAKE_SHA256" "$CMAKE_ASSET" > "$CMAKE_CHECKSUM"

if [ "$RUN_LLVM_FIXTURE" = "true" ]; then
    LLVM_ARCHIVE_ROOT="${LLVM_ASSET%.tar.xz}"
    LLVM_SOURCE="$TEST_ROOT/llvm-source/$LLVM_ARCHIVE_ROOT"
    mkdir -p "$LLVM_SOURCE/bin"
    cat > "$LLVM_SOURCE/bin/clang" << 'EOF'
#!/bin/bash
set -e
if [ "${1:-}" = "--version" ]; then
    printf 'clang version %s\n' "$LLVM_FIXTURE_VERSION"
    exit 0
fi
output=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-o" ]; then
        shift
        output="$1"
    fi
    shift
done
[ -n "$output" ]
printf '#!/bin/sh\nexit 0\n' > "$output"
chmod 755 "$output"
EOF
    cat > "$LLVM_SOURCE/bin/llvm-config" << 'EOF'
#!/bin/sh
printf '%s\n' "$LLVM_FIXTURE_VERSION"
EOF
    cat > "$LLVM_SOURCE/bin/ld.lld" << 'EOF'
#!/bin/sh
printf 'LLD %s\n' "$LLVM_FIXTURE_VERSION"
EOF
    cat > "$LLVM_SOURCE/bin/mlir-opt" << 'EOF'
#!/bin/sh
exit 0
EOF
    chmod 755 \
        "$LLVM_SOURCE/bin/clang" \
        "$LLVM_SOURCE/bin/llvm-config" \
        "$LLVM_SOURCE/bin/ld.lld" \
        "$LLVM_SOURCE/bin/mlir-opt"
    ln -s clang "$LLVM_SOURCE/bin/clang++"
    tar cJf "$ASSET_DIRECTORY/$LLVM_ASSET" \
        -C "$TEST_ROOT/llvm-source" "$LLVM_ARCHIVE_ROOT"
    LLVM_SHA256=$(file_sha256 "$ASSET_DIRECTORY/$LLVM_ASSET")
else
    LLVM_SHA256=""
fi

python3 - "$ASSET_DIRECTORY/$NINJA_ASSET" "$FIXTURE_VERSION" << 'PY'
import stat
import sys
import zipfile

destination, version = sys.argv[1:]
info = zipfile.ZipInfo("ninja")
info.create_system = 3
info.external_attr = (stat.S_IFREG | 0o755) << 16
with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr(
        info,
        f"#!/bin/sh\nprintf '%s\\n' '{version}'\n".encode(),
    )
PY
NINJA_SHA256=$(file_sha256 "$ASSET_DIRECTORY/$NINJA_ASSET")

if [ "$RUN_VULKAN_FIXTURE" = "true" ]; then
    VULKAN_ASSET="vulkansdk-linux-x86_64-$VULKAN_FIXTURE_VERSION.tar.xz"
    VULKAN_SOURCE="$TEST_ROOT/vulkan-source/$VULKAN_FIXTURE_VERSION"
    mkdir -p \
        "$VULKAN_SOURCE/x86_64/bin" \
        "$VULKAN_SOURCE/x86_64/include/vulkan" \
        "$VULKAN_SOURCE/x86_64/lib"
    for executable in glslangValidator spirv-val; do
        printf '#!/bin/sh\nprintf "%s fixture\\n"\n' "$executable" \
            > "$VULKAN_SOURCE/x86_64/bin/$executable"
        chmod 755 "$VULKAN_SOURCE/x86_64/bin/$executable"
    done
    printf 'fixture vulkan header\n' \
        > "$VULKAN_SOURCE/x86_64/include/vulkan/vulkan.h"
    printf '#!/bin/sh\n' > "$VULKAN_SOURCE/setup-env.sh"
    tar cJf "$ASSET_DIRECTORY/$VULKAN_ASSET" \
        -C "$TEST_ROOT/vulkan-source" "$VULKAN_FIXTURE_VERSION"
    VULKAN_SHA256=$(file_sha256 "$ASSET_DIRECTORY/$VULKAN_ASSET")
    VULKAN_CHECKSUM="$ASSET_DIRECTORY/vulkan_sdk.tar.xz.txt"
    printf '%s  %s\n' "$VULKAN_SHA256" "$VULKAN_ASSET" \
        > "$VULKAN_CHECKSUM"
else
    VULKAN_ASSET=""
    VULKAN_SHA256=""
    VULKAN_CHECKSUM=""
fi

NIX_INSTALLER_ASSET="nix-installer-fixture"
NIX_INSTALLER_PATH="$ASSET_DIRECTORY/$NIX_INSTALLER_ASSET"
case "$(uname -s)" in
    Darwin) NIX_TEST_PLANNER="macos" ;;
    *) NIX_TEST_PLANNER="linux" ;;
esac
export NIX_TEST_PLANNER
cat > "$NIX_INSTALLER_PATH" << 'EOF'
#!/bin/bash
set -e
case "${1:-}" in
    --version)
        printf 'nix-installer fixture\n'
        ;;
    install)
        operation="$1"
        [ "${2:-}" = "--no-confirm" ]
        if env | grep -q '^NIX_INSTALLER_'; then
            echo "ambient NIX_INSTALLER setting reached fixture" >&2
            exit 91
        fi
        mkdir -p \
            "$NIX_TEST_ROOT/store/fixture/bin" \
            "$NIX_TEST_ROOT/var/nix/profiles/default/bin"
        cat > "$NIX_TEST_ROOT/store/fixture/bin/nix" << 'INNER'
#!/bin/sh
printf 'nix fixture 9.8.7\n'
INNER
        chmod 755 "$NIX_TEST_ROOT/store/fixture/bin/nix"
        ln -sfn \
            "$NIX_TEST_ROOT/store/fixture/bin/nix" \
            "$NIX_TEST_ROOT/var/nix/profiles/default/bin/nix"
        cp "$0" "$NIX_TEST_ROOT/nix-installer"
        chmod 755 "$NIX_TEST_ROOT/nix-installer"
        cat > "$NIX_TEST_ROOT/receipt.json" << INNER
{"version":"3.21.9","actions":[{"action_name":"fixture"}],"planner":{"planner":"$NIX_TEST_PLANNER"}}
INNER
        printf '%s\n' "$operation" >> "$NIX_TEST_ROOT/operation-log"
        ;;
    repair)
        operation="$1"
        [ "${2:-}" = "--no-confirm" ]
        if env | grep -q '^NIX_INSTALLER_'; then
            echo "ambient NIX_INSTALLER setting reached fixture" >&2
            exit 91
        fi
        [ -x "$NIX_TEST_ROOT/var/nix/profiles/default/bin/nix" ]
        [ -x "$NIX_TEST_ROOT/nix-installer" ]
        [ -f "$NIX_TEST_ROOT/receipt.json" ]
        printf '%s\n' "$operation" >> "$NIX_TEST_ROOT/operation-log"
        ;;
    *)
        exit 92
        ;;
esac
EOF
chmod 755 "$NIX_INSTALLER_PATH"
NIX_INSTALLER_SHA256=$(file_sha256 "$NIX_INSTALLER_PATH")

NVM_FIXTURE_DIRECTORY="$TEST_ROOT/nvm-fixture"
mkdir "$NVM_FIXTURE_DIRECTORY"
cat > "$NVM_FIXTURE_DIRECTORY/nvm.sh" << 'EOF'
nvm() {
    if [ "${1:-}" = "--version" ]; then
        printf '%s\n' "$FIXTURE_VERSION"
        return 0
    fi
    return 0
}
EOF
printf '#!/bin/sh\nexit 0\n' > "$NVM_FIXTURE_DIRECTORY/nvm-exec"
printf '# fixture completion\n' > "$NVM_FIXTURE_DIRECTORY/bash_completion"
chmod 755 "$NVM_FIXTURE_DIRECTORY/nvm-exec"
NVM_SH_SHA256=$(file_sha256 "$NVM_FIXTURE_DIRECTORY/nvm.sh")
NVM_EXEC_SHA256=$(file_sha256 "$NVM_FIXTURE_DIRECTORY/nvm-exec")
NVM_COMPLETION_SHA256=$(file_sha256 "$NVM_FIXTURE_DIRECTORY/bash_completion")
TEST_NVM_SH_SHA256="$NVM_SH_SHA256"
TEST_NVM_EXEC_SHA256="$NVM_EXEC_SHA256"
TEST_NVM_COMPLETION_SHA256="$NVM_COMPLETION_SHA256"

export \
    ASSET_DIRECTORY \
    CMAKE_ASSET \
    CMAKE_CHECKSUM \
    CMAKE_SHA256 \
    FIXTURE_VERSION \
    LLVM_ASSET \
    LLVM_FIXTURE_VERSION \
    LLVM_SHA256 \
    NINJA_ASSET \
    NINJA_SHA256 \
    NIX_INSTALLER_ASSET \
    NIX_INSTALLER_PATH \
    NVM_FIXTURE_DIRECTORY \
    VULKAN_ASSET \
    VULKAN_CHECKSUM \
    VULKAN_FIXTURE_VERSION
cat > "$FAKE_BIN/curl" << 'EOF'
#!/bin/bash
set -e
output=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o)
            shift
            output="$1"
            ;;
        -*)
            ;;
        *)
            url="$1"
            ;;
    esac
    shift
done
if [ "${FIXTURE_NETWORK_FORBIDDEN:-}" = "true" ]; then
    echo "fixture network was forbidden" >&2
    exit 73
fi
if [ -n "${FIXTURE_CURL_READY:-}" ] &&
        [ -n "${FIXTURE_CURL_RELEASE:-}" ] &&
        [ ! -e "$FIXTURE_CURL_RELEASE" ]; then
    printf 'ready\n' > "$FIXTURE_CURL_READY"
    while [ ! -e "$FIXTURE_CURL_RELEASE" ]; do
        sleep 0.01
    done
fi
case "$url" in
    */llvm/llvm-project/releases/tags/llvmorg-"$LLVM_FIXTURE_VERSION")
        printf '{"assets":[{"name":"%s","digest":"sha256:%s"}]}\n' \
            "$LLVM_ASSET" "$LLVM_SHA256"
        ;;
    */ninja-build/ninja/releases/tags/v"$FIXTURE_VERSION")
        printf '{"assets":[{"name":"%s","digest":"sha256:%s"}]}\n' \
            "$NINJA_ASSET" "$NINJA_SHA256"
        ;;
    */cmake-"$FIXTURE_VERSION"-SHA-256.txt)
        cp "$CMAKE_CHECKSUM" "$output"
        ;;
    */"$CMAKE_ASSET")
        if [ "${FIXTURE_CORRUPT:-}" = "cmake" ]; then
            printf 'corrupt\n' > "$output"
        else
            cp "$ASSET_DIRECTORY/$CMAKE_ASSET" "$output"
        fi
        ;;
    */"$LLVM_ASSET")
        cp "$ASSET_DIRECTORY/$LLVM_ASSET" "$output"
        ;;
    */"$NINJA_ASSET")
        cp "$ASSET_DIRECTORY/$NINJA_ASSET" "$output"
        ;;
    */sha/*/linux/vulkan_sdk.tar.xz.txt)
        cp "$VULKAN_CHECKSUM" "$output"
        ;;
    */download/*/linux/vulkan_sdk.tar.xz)
        cp "$ASSET_DIRECTORY/$VULKAN_ASSET" "$output"
        ;;
    */"$NIX_INSTALLER_ASSET")
        cp "$NIX_INSTALLER_PATH" "$output"
        ;;
    */nvm.sh|*/nvm-exec|*/bash_completion)
        if [ "${FIXTURE_CORRUPT:-}" = "${url##*/}" ]; then
            printf 'corrupt NVM runtime\n' > "$output"
        else
            cp "$NVM_FIXTURE_DIRECTORY/${url##*/}" "$output"
        fi
        ;;
    *)
        echo "unexpected fixture URL: $url" >&2
        exit 72
        ;;
esac
EOF
chmod 755 "$FAKE_BIN/curl"

run_tool_installer() {
    local tool_name="$1"
    shift

    PATH="$FAKE_BIN:$PATH" \
        TOOLS_DIR="$TOOLS_ROOT" \
        bash "$DOTFILES/tools/$tool_name/install.sh" "$@"
}

run_tool_installer cmake "$FIXTURE_VERSION" > "$OUTPUT" 2>&1 ||
    fail "valid CMake fixture did not install"
"$TOOLS_ROOT/cmake/$FIXTURE_VERSION/$CMAKE_EXECUTABLE_ROOT/cmake" --version |
    grep -qx "cmake version $FIXTURE_VERSION" ||
    fail "CMake executable was not published"
[ "$(readlink "$TOOLS_ROOT/cmake/latest")" = "$FIXTURE_VERSION" ] ||
    fail "CMake latest selector is incorrect"
printf 'stale\n' > "$TOOLS_ROOT/cmake/$FIXTURE_VERSION/stale-generation"
run_tool_installer cmake --force "$FIXTURE_VERSION" > "$OUTPUT" 2>&1 ||
    fail "verified CMake force replacement failed"
[ ! -e "$TOOLS_ROOT/cmake/$FIXTURE_VERSION/stale-generation" ] ||
    fail "CMake force replacement overlaid the prior generation"
printf 'retained\n' > "$TOOLS_ROOT/cmake/$FIXTURE_VERSION/retained-generation"
if FIXTURE_CORRUPT=cmake run_tool_installer \
        cmake --force "$FIXTURE_VERSION" > "$OUTPUT" 2>&1; then
    fail "CMake accepted a corrupt force-replacement archive"
fi
[ -f "$TOOLS_ROOT/cmake/$FIXTURE_VERSION/retained-generation" ] ||
    fail "failed CMake replacement damaged the active generation"

run_tool_installer ninja "$FIXTURE_VERSION" > "$OUTPUT" 2>&1 ||
    fail "valid Ninja fixture did not install"
[ "$("$TOOLS_ROOT/ninja/$FIXTURE_VERSION/bin/ninja" --version)" = \
    "$FIXTURE_VERSION" ] || fail "Ninja executable was not published"

if [ "$RUN_LLVM_FIXTURE" = "true" ]; then
    run_tool_installer llvm "$LLVM_FIXTURE_VERSION" > "$OUTPUT" 2>&1 ||
        fail "valid LLVM fixture did not install"
    "$TOOLS_ROOT/llvm/$LLVM_FIXTURE_VERSION/bin/llvm-config" --version |
        grep -qx "$LLVM_FIXTURE_VERSION" ||
        fail "LLVM tool surface was not published"
fi

if [ "$RUN_VULKAN_FIXTURE" = "true" ]; then
    run_tool_installer vulkan "$VULKAN_FIXTURE_VERSION" > "$OUTPUT" 2>&1 ||
        fail "valid Vulkan fixture did not install"
    [ -f "$TOOLS_ROOT/vulkan/$VULKAN_FIXTURE_VERSION/x86_64/include/vulkan/vulkan.h" ] ||
        fail "Vulkan SDK payload was not published"
fi

for tool_name in cmake llvm ninja vulkan; do
    if TOOLS_DIR="$TEST_ROOT/rejected-tools" \
            bash "$DOTFILES/tools/$tool_name/install.sh" ../escape \
            > "$OUTPUT" 2>&1; then
        fail "$tool_name accepted a traversing version"
    fi
    if TOOLS_DIR="$TEST_ROOT/rejected-tools" \
            bash "$DOTFILES/tools/$tool_name/install.sh" "$FIXTURE_VERSION" extra \
            > "$OUTPUT" 2>&1; then
        fail "$tool_name accepted multiple versions"
    fi
done

AMBIENT_FORCE_TOOLS="$TEST_ROOT/ambient-force-tools"
mkdir -p "$AMBIENT_FORCE_TOOLS/cmake/$FIXTURE_VERSION"
printf 'owned\n' \
    > "$AMBIENT_FORCE_TOOLS/cmake/$FIXTURE_VERSION/ownership-sentinel"
if FORCE=true TOOLS_DIR="$AMBIENT_FORCE_TOOLS" \
        bash "$DOTFILES/tools/cmake/install.sh" "$FIXTURE_VERSION" \
        > "$OUTPUT" 2>&1; then
    fail "ambient FORCE authorized CMake replacement"
fi
[ -f "$AMBIENT_FORCE_TOOLS/cmake/$FIXTURE_VERSION/ownership-sentinel" ] ||
    fail "ambient FORCE damaged the unidentified CMake directory"

bash -c '
    source "$1/tools/ninja/install.sh"
    PLATFORM=linux
    ARCH=aarch64
    VERSION=1.13.2
    select_archive
    [ "$ZIPFILE" = "ninja-linux-aarch64.zip" ]
' platform-selection "$DOTFILES" ||
    fail "Ninja did not select its Linux ARM64 archive"
if bash -c '
        source "$1/tools/vulkan/install.sh"
        PLATFORM=linux
        ARCH=aarch64
        VERSION=1.4.350.1
        select_archive
    ' platform-selection "$DOTFILES" > "$OUTPUT" 2>&1; then
    fail "Vulkan accepted an unavailable Linux ARM64 SDK archive"
fi

# The dispatcher metadata is the exact list consumed by both help and --all.
# Intel macOS excludes installers with no native artifact; Linux ARM64 excludes
# the x86_64-only TheRock and LunarG closures.
bash -c '
    source "$1/tools/install.sh"

    PLATFORM=darwin
    ARCH=x86_64
    supported=$(get_supported_tools false)
    if printf "%s\n" "$supported" | grep -Eq "^(llvm|nix)$"; then
        exit 1
    fi
    help=$(show_help)
    if printf "%s\n" "$help" | grep -Eq "^[[:space:]]+(llvm|nix)[[:space:]]*$"; then
        exit 1
    fi
    if (main nix --help) >/dev/null 2>&1; then
        exit 1
    fi

    PLATFORM=linux
    ARCH=aarch64
    supported=$(get_supported_tools false)
    if printf "%s\n" "$supported" | grep -Eq "^(rocm|vulkan)$"; then
        exit 1
    fi
    printf "%s\n" "$supported" | grep -qx cuda
    printf "%s\n" "$supported" | grep -qx llvm
    printf "%s\n" "$supported" | grep -qx nix
' dispatcher-targets "$DOTFILES" ||
    fail "dispatcher advertised a tool without an artifact for the host target"

# Direct invocation enforces the same ROCm host boundary before parsing options
# or entering the network path.
FAKE_ARM_BIN="$TEST_ROOT/fake-arm-bin"
mkdir -p "$FAKE_ARM_BIN"
cat > "$FAKE_ARM_BIN/uname" << 'EOF'
#!/bin/sh
case "${1:-}" in
    -s) printf 'Linux\n' ;;
    -m) printf 'aarch64\n' ;;
    *) exit 64 ;;
esac
EOF
chmod 755 "$FAKE_ARM_BIN/uname"
if PATH="$FAKE_ARM_BIN:$PATH" \
        bash "$DOTFILES/tools/rocm/install.sh" --help \
        > "$OUTPUT" 2>&1; then
    fail "direct ROCm installer accepted Linux ARM64"
fi
grep -q 'only available for Linux x86_64' "$OUTPUT" ||
    fail "direct ROCm target refusal did not identify its artifact boundary"

NIX_TEST_ROOT="$TEST_ROOT/nix-root"
export NIX_INSTALLER_PATH NIX_INSTALLER_SHA256 NIX_TEST_ROOT
run_nix_installer() {
    NIX_TEST_PRIOR_VERSION="${NIX_TEST_PRIOR_VERSION:-}" \
        NIX_TEST_PRIOR_SHA256="${NIX_TEST_PRIOR_SHA256:-}" \
        FIXTURE_NETWORK_FORBIDDEN="${FIXTURE_NETWORK_FORBIDDEN:-}" \
        PATH="$FAKE_BIN:$PATH" \
        NIX_INSTALLER_POISON=must-be-cleared \
        bash -c '
            source "$1/tools/nix/install.sh"
            NIX_ROOT="$NIX_TEST_ROOT"
            NIX_INSTALLER_URL_ROOT="https://fixture.invalid/nix"
            reviewed_nix_installer_sha256() {
                local version="$1"

                if [ "$version" = "$NIX_INSTALLER_VERSION" ]; then
                    printf "%s\n" "$NIX_INSTALLER_SHA256"
                elif [ -n "$NIX_TEST_PRIOR_VERSION" ] &&
                        [ "$version" = "$NIX_TEST_PRIOR_VERSION" ]; then
                    printf "%s\n" "$NIX_TEST_PRIOR_SHA256"
                else
                    return 1
                fi
            }
            select_installer() {
                INSTALLER_ASSET="$NIX_INSTALLER_ASSET"
                INSTALLER_SHA256="$NIX_INSTALLER_SHA256"
            }
            main "${@:2}"
        ' platform-nix "$DOTFILES" "$@"
}
export NIX_INSTALLER_ASSET
run_nix_installer > "$OUTPUT" 2>&1 ||
    fail "pinned Nix fixture did not install"
grep -qx install "$NIX_TEST_ROOT/operation-log" ||
    fail "Nix fixture did not execute install"
FIXTURE_NETWORK_FORBIDDEN=true \
    run_nix_installer --force > "$OUTPUT" 2>&1 ||
    fail "managed Nix force repair failed"
[ "$(tail -n 1 "$NIX_TEST_ROOT/operation-log")" = "repair" ] ||
    fail "Nix --force did not use receipt-backed repair"
[ -f "$NIX_TEST_ROOT/receipt.json" ] ||
    fail "Nix repair removed its ownership receipt"

NIX_OPERATION_COUNT=$(wc -l < "$NIX_TEST_ROOT/operation-log")
printf 'foreign installer\n' > "$NIX_TEST_ROOT/nix-installer"
chmod 755 "$NIX_TEST_ROOT/nix-installer"
if run_nix_installer --force > "$OUTPUT" 2>&1; then
    fail "Nix accepted a foreign installed-installer ownership anchor"
fi
[ "$(wc -l < "$NIX_TEST_ROOT/operation-log")" -eq "$NIX_OPERATION_COUNT" ] ||
    fail "foreign Nix installer refusal executed a repair"
cp "$NIX_INSTALLER_PATH" "$NIX_TEST_ROOT/nix-installer"
chmod 755 "$NIX_TEST_ROOT/nix-installer"

printf '{}\n' > "$NIX_TEST_ROOT/receipt.json"
if run_nix_installer --force > "$OUTPUT" 2>&1; then
    fail "Nix accepted an unidentified ownership receipt"
fi
[ "$(wc -l < "$NIX_TEST_ROOT/operation-log")" -eq "$NIX_OPERATION_COUNT" ] ||
    fail "foreign Nix receipt refusal executed a repair"

cat > "$NIX_TEST_ROOT/receipt.json" << EOF
{"version":"3.21.8","actions":[{"action_name":"fixture"}],"planner":{"planner":"$NIX_TEST_PLANNER"}}
EOF
if run_nix_installer --force > "$OUTPUT" 2>&1; then
    fail "Nix accepted a receipt from another installer version"
fi
[ "$(wc -l < "$NIX_TEST_ROOT/operation-log")" -eq "$NIX_OPERATION_COUNT" ] ||
    fail "wrong-version Nix receipt refusal executed a repair"

# A prior reviewed pin remains a durable ownership anchor. Normal invocation
# recognizes it without mutation. --force runs that exact attested installed
# binary's repair command; repair does not upgrade the installer or receipt.
cp "$NIX_INSTALLER_PATH" "$NIX_TEST_ROOT/nix-installer"
printf '\n# distinct reviewed prior fixture\n' >> "$NIX_TEST_ROOT/nix-installer"
chmod 755 "$NIX_TEST_ROOT/nix-installer"
NIX_TEST_PRIOR_VERSION=3.21.8
NIX_TEST_PRIOR_SHA256=$(file_sha256 "$NIX_TEST_ROOT/nix-installer")
export NIX_TEST_PRIOR_VERSION NIX_TEST_PRIOR_SHA256
run_nix_installer > "$OUTPUT" 2>&1 ||
    fail "Nix did not recognize a prior reviewed installer pin"
grep -q 'Recognized reviewed installer v3.21.8' "$OUTPUT" ||
    fail "prior reviewed Nix pin was not reported accurately"
[ "$(wc -l < "$NIX_TEST_ROOT/operation-log")" -eq "$NIX_OPERATION_COUNT" ] ||
    fail "prior reviewed Nix reuse executed a repair"
FIXTURE_NETWORK_FORBIDDEN=true \
    run_nix_installer --force > "$OUTPUT" 2>&1 ||
    fail "Nix could not repair a prior reviewed installer pin"
[ "$(tail -n 1 "$NIX_TEST_ROOT/operation-log")" = "repair" ] ||
    fail "Nix prior-pin repair did not use the installed repair transaction"
grep -q '"version":"3.21.8"' "$NIX_TEST_ROOT/receipt.json" ||
    fail "Nix prior-pin repair rewrote its ownership receipt"
[ "$(file_sha256 "$NIX_TEST_ROOT/nix-installer")" = "$NIX_TEST_PRIOR_SHA256" ] ||
    fail "Nix prior-pin repair replaced its installed installer binary"

if run_nix_installer unexpected > "$OUTPUT" 2>&1; then
    fail "Nix accepted an unknown positional argument"
fi

NVM_HOME="$TEST_ROOT/nvm-home"
NVM_ROOT="$NVM_HOME/.nvm"
NVM_ORIGINALS="$TEST_ROOT/nvm-originals"
mkdir -p "$NVM_ROOT" "$NVM_ORIGINALS" "$NVM_ROOT/versions/node/preserved"
cp "$NVM_FIXTURE_DIRECTORY/nvm.sh" "$NVM_ROOT/nvm.sh"
cp "$NVM_FIXTURE_DIRECTORY/nvm-exec" "$NVM_ROOT/nvm-exec"
cp "$NVM_FIXTURE_DIRECTORY/bash_completion" "$NVM_ROOT/bash_completion"
chmod 755 "$NVM_ROOT/nvm-exec"
cp "$NVM_ROOT/nvm.sh" "$NVM_ORIGINALS/nvm.sh"
cp "$NVM_ROOT/nvm-exec" "$NVM_ORIGINALS/nvm-exec"
cp "$NVM_ROOT/bash_completion" "$NVM_ORIGINALS/bash_completion"
printf 'preserve\n' > "$NVM_ROOT/versions/node/preserved/state"
git -C "$NVM_ROOT" init -q
git -C "$NVM_ROOT" config user.name fixture
git -C "$NVM_ROOT" config user.email fixture@example.invalid
git -C "$NVM_ROOT" add nvm.sh nvm-exec bash_completion
git -C "$NVM_ROOT" commit -qm 'fixture runtime'
git -C "$NVM_ROOT" remote add origin https://github.com/nvm-sh/nvm.git

# Migration ownership covers the complete tracked checkout, not only the three
# runtime files that will become links.
printf 'tracked\n' > "$NVM_ROOT/tracked-state"
git -C "$NVM_ROOT" add tracked-state
git -C "$NVM_ROOT" commit -qm 'tracked fixture state'
printf 'changed\n' > "$NVM_ROOT/tracked-state"

export \
    TEST_NVM_COMPLETION_SHA256 \
    TEST_NVM_EXEC_SHA256 \
    TEST_NVM_SH_SHA256
run_nvm_installer_for_home() {
    local selected_home="$1"
    shift

    PATH="$FAKE_BIN:$PATH" \
        HOME="$selected_home" \
        NVM_DIR="" \
        DOTFILES_INSTALLER_TEST_FAULT="${DOTFILES_INSTALLER_TEST_FAULT:-}" \
        FIXTURE_CORRUPT="${FIXTURE_CORRUPT:-}" \
        FIXTURE_NETWORK_FORBIDDEN="${FIXTURE_NETWORK_FORBIDDEN:-}" \
        FIXTURE_CURL_READY="${FIXTURE_CURL_READY:-}" \
        FIXTURE_CURL_RELEASE="${FIXTURE_CURL_RELEASE:-}" \
        bash -c '
            source "$1/tools/nvm/install.sh"
            NVM_VERSION="$FIXTURE_VERSION"
            NVM_COMMIT=1111111111111111111111111111111111111111
            NVM_SH_SHA256="$TEST_NVM_SH_SHA256"
            NVM_EXEC_SHA256="$TEST_NVM_EXEC_SHA256"
            NVM_COMPLETION_SHA256="$TEST_NVM_COMPLETION_SHA256"
            NVM_SOURCE_ROOT="https://fixture.invalid/nvm"
            main "${@:2}"
        ' platform-nvm "$DOTFILES" "$@"
}

run_nvm_installer() {
    run_nvm_installer_for_home "$NVM_HOME" "$@"
}

if run_nvm_installer --migrate > "$OUTPUT" 2>&1; then
    fail "NVM migrated a checkout with unrelated tracked changes"
fi
git -C "$NVM_ROOT" show HEAD:tracked-state > "$NVM_ROOT/tracked-state"

if DOTFILES_INSTALLER_TEST_FAULT=term-after-nvm.sh \
        run_nvm_installer --migrate > "$OUTPUT" 2>&1; then
    fail "signal-interrupted NVM publication returned success"
fi
for runtime_name in nvm.sh nvm-exec bash_completion; do
    [ ! -L "$NVM_ROOT/$runtime_name" ] ||
        fail "NVM rollback did not restore regular legacy $runtime_name"
    cmp -s "$NVM_ROOT/$runtime_name" "$NVM_ORIGINALS/$runtime_name" ||
        fail "NVM rollback changed legacy $runtime_name"
done
if [ -e "$NVM_ROOT/.dotfiles-current" ] ||
        [ -L "$NVM_ROOT/.dotfiles-current" ]; then
    fail "NVM rollback retained the interrupted release pointer"
fi
if find "$NVM_ROOT" -maxdepth 1 \
        -name '.dotfiles-runtime-recovery.*' -print -quit | grep -q .; then
    fail "successful NVM rollback retained recovery state"
fi
[ -f "$NVM_ROOT/versions/node/preserved/state" ] ||
    fail "NVM rollback damaged installed Node state"

# Kill one real, lock-owning main process after its rollback state is durable,
# then prove the next main process reclaims the stale lock and replays recovery.
if DOTFILES_INSTALLER_TEST_FAULT=kill-after-nvm.sh \
        run_nvm_installer --migrate > "$OUTPUT" 2>&1; then
    fail "hard-killed NVM publication returned success"
fi
[ -d "$NVM_ROOT/.dotfiles-install.lock" ] ||
    fail "hard-killed NVM publication did not retain its install lock"
if ! find "$NVM_ROOT/.dotfiles-releases" -maxdepth 1 \
        -name '.dotfiles-stage-*' -print -quit | grep -q .; then
    fail "hard-killed NVM publication did not retain owned release staging"
fi
if ! find "$NVM_ROOT" -maxdepth 1 \
        -name '.dotfiles-runtime-recovery.*' -print -quit | grep -q .; then
    fail "interrupted NVM publication did not preserve durable recovery state"
fi
run_nvm_installer --migrate > "$OUTPUT" 2>&1 ||
    fail "later NVM main process could not reclaim and replay recovery state"
if find "$NVM_ROOT/.dotfiles-releases" -maxdepth 1 \
        -name '.dotfiles-stage-*' -print -quit | grep -q .; then
    fail "recovered NVM main retained abandoned release staging"
fi
for runtime_name in nvm.sh nvm-exec bash_completion; do
    [ "$(readlink "$NVM_ROOT/$runtime_name")" = \
        ".dotfiles-current/$runtime_name" ] ||
        fail "recovered NVM main did not publish managed $runtime_name"
done
[ "$(readlink "$NVM_ROOT/.dotfiles-current")" = \
    ".dotfiles-releases/$FIXTURE_VERSION" ] ||
    fail "NVM did not publish its versioned release pointer"
for runtime_name in nvm.sh nvm-exec bash_completion; do
    [ "$(readlink "$NVM_ROOT/$runtime_name")" = \
        ".dotfiles-current/$runtime_name" ] ||
        fail "NVM did not publish managed $runtime_name"
done
[ -f "$NVM_ROOT/versions/node/preserved/state" ] ||
    fail "NVM migration damaged installed Node state"
FIXTURE_NETWORK_FORBIDDEN=true \
    run_nvm_installer --migrate > "$OUTPUT" 2>&1 ||
    fail "idempotent NVM migration re-entered the network path"

NVM_RELEASE_PATH="$NVM_ROOT/.dotfiles-releases/$FIXTURE_VERSION"
NVM_EXTERNAL_RELEASE="$TEST_ROOT/nvm-external-release"
mv "$NVM_RELEASE_PATH" "$NVM_EXTERNAL_RELEASE"
ln -s "$NVM_EXTERNAL_RELEASE" "$NVM_RELEASE_PATH"
if FIXTURE_NETWORK_FORBIDDEN=true \
        run_nvm_installer --migrate > "$OUTPUT" 2>&1; then
    fail "NVM accepted a symlinked managed release root"
fi
unlink "$NVM_RELEASE_PATH"
mv "$NVM_EXTERNAL_RELEASE" "$NVM_RELEASE_PATH"

if run_nvm_installer positional > "$OUTPUT" 2>&1; then
    fail "NVM accepted an unknown positional argument"
fi

SIGNAL_NVM_HOME="$TEST_ROOT/signal-nvm-home"
mkdir "$SIGNAL_NVM_HOME"
if DOTFILES_INSTALLER_TEST_FAULT=term-after-lock-directory \
        run_nvm_installer_for_home "$SIGNAL_NVM_HOME" --migrate \
            > "$OUTPUT" 2>&1; then
    fail "signal during NVM lock-directory acquisition returned success"
fi
if [ -e "$SIGNAL_NVM_HOME/.nvm/.dotfiles-install.lock" ] ||
        [ -L "$SIGNAL_NVM_HOME/.nvm/.dotfiles-install.lock" ]; then
    fail "signal during NVM lock-directory acquisition leaked its lock"
fi
if DOTFILES_INSTALLER_TEST_FAULT=term-after-lock-owner \
        run_nvm_installer_for_home "$SIGNAL_NVM_HOME" --migrate \
            > "$OUTPUT" 2>&1; then
    fail "signal during NVM lock-owner publication returned success"
fi
if [ -e "$SIGNAL_NVM_HOME/.nvm/.dotfiles-install.lock" ] ||
        [ -L "$SIGNAL_NVM_HOME/.nvm/.dotfiles-install.lock" ]; then
    fail "signal during NVM lock-owner publication leaked its lock"
fi
run_nvm_installer_for_home "$SIGNAL_NVM_HOME" --migrate \
    > "$OUTPUT" 2>&1 ||
    fail "NVM could not install after signal-interrupted lock acquisition"
if DOTFILES_INSTALLER_TEST_FAULT=kill-after-committed \
        run_nvm_installer_for_home "$SIGNAL_NVM_HOME" --force \
            > "$OUTPUT" 2>&1; then
    fail "hard-killed committed NVM publication returned success"
fi
[ -d "$SIGNAL_NVM_HOME/.nvm/.dotfiles-install.lock" ] ||
    fail "hard-killed committed NVM publication did not retain its lock"
if ! find "$SIGNAL_NVM_HOME/.nvm" -maxdepth 1 \
        -name '.dotfiles-runtime-recovery.*' -print -quit | grep -q .; then
    fail "hard-killed committed NVM publication lost its recovery state"
fi
FIXTURE_NETWORK_FORBIDDEN=true \
    run_nvm_installer_for_home "$SIGNAL_NVM_HOME" --migrate \
        > "$OUTPUT" 2>&1 ||
    fail "NVM could not recover committed publication without downloading"

CORRUPT_NVM_HOME="$TEST_ROOT/corrupt-nvm-home"
mkdir "$CORRUPT_NVM_HOME"
if FIXTURE_CORRUPT=nvm.sh \
        run_nvm_installer_for_home "$CORRUPT_NVM_HOME" --migrate \
            > "$OUTPUT" 2>&1; then
    fail "NVM accepted a checksum-mismatched runtime"
fi
if find "$CORRUPT_NVM_HOME/.nvm/.dotfiles-releases" -maxdepth 1 \
        -name '.dotfiles-stage-*' -print -quit | grep -q .; then
    fail "failed NVM checksum validation retained owned staging"
fi
run_nvm_installer_for_home "$CORRUPT_NVM_HOME" --migrate \
    > "$OUTPUT" 2>&1 ||
    fail "NVM could not retry after checksum-validation cleanup"

ORPHANED_RELEASE_HOME="$TEST_ROOT/orphaned-release-home"
mkdir "$ORPHANED_RELEASE_HOME"
if DOTFILES_INSTALLER_TEST_FAULT=kill-after-release-publication \
        run_nvm_installer_for_home "$ORPHANED_RELEASE_HOME" --migrate \
            > "$OUTPUT" 2>&1; then
    fail "hard-killed pre-link NVM publication returned success"
fi
[ -d "$ORPHANED_RELEASE_HOME/.nvm/.dotfiles-releases/$FIXTURE_VERSION" ] ||
    fail "hard-killed pre-link NVM publication lost its verified release"
FIXTURE_NETWORK_FORBIDDEN=true \
    run_nvm_installer_for_home "$ORPHANED_RELEASE_HOME" --migrate \
        > "$OUTPUT" 2>&1 ||
    fail "NVM could not resume its sole verified orphaned release"
[ "$(readlink "$ORPHANED_RELEASE_HOME/.nvm/.dotfiles-current")" = \
    ".dotfiles-releases/$FIXTURE_VERSION" ] ||
    fail "resumed orphaned NVM release did not publish its runtime pointer"

UNOWNED_STAGING_HOME="$TEST_ROOT/unowned-staging-home"
UNOWNED_STAGING="$UNOWNED_STAGING_HOME/.nvm/.dotfiles-releases/.dotfiles-stage-$FIXTURE_VERSION.BADBAD"
mkdir -p "$UNOWNED_STAGING/payload"
printf 'not installer owned\n' > "$UNOWNED_STAGING/payload/private-state"
if run_nvm_installer_for_home "$UNOWNED_STAGING_HOME" --migrate \
        > "$OUTPUT" 2>&1; then
    fail "NVM removed release staging without an exact ownership record"
fi
[ -f "$UNOWNED_STAGING/payload/private-state" ] ||
    fail "NVM changed unowned release staging"

PUBLISH_STATE_HOME="$TEST_ROOT/publish-state-home"
PUBLISH_STATE_ROOT="$PUBLISH_STATE_HOME/.nvm/.dotfiles-releases"
PUBLISH_STATE_STAGING="$PUBLISH_STATE_ROOT/.dotfiles-stage-$FIXTURE_VERSION.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
mkdir -p \
    "$PUBLISH_STATE_STAGING/payload" \
    "$PUBLISH_STATE_ROOT/.replace-$FIXTURE_VERSION.ABC123" \
    "$PUBLISH_STATE_ROOT/.publish-$FIXTURE_VERSION.lock"
{
    printf 'schema=1\n'
    printf 'tool=nvm\n'
    printf 'version=%s\n' "$FIXTURE_VERSION"
    printf 'lock_token=12345-123-456\n'
    printf 'state=staging\n'
} > "$PUBLISH_STATE_STAGING/.dotfiles-nvm-staging"
printf 'partial verified download\n' > "$PUBLISH_STATE_STAGING/payload/nvm.sh"
if run_nvm_installer_for_home "$PUBLISH_STATE_HOME" --migrate \
        > "$OUTPUT" 2>&1; then
    fail "NVM crossed an abandoned shared release-publication transaction"
fi
[ -f "$PUBLISH_STATE_STAGING/payload/nvm.sh" ] ||
    fail "NVM deleted staging referenced by shared publication state"

CONCURRENT_NVM_HOME="$TEST_ROOT/concurrent-nvm-home"
CONCURRENT_READY="$TEST_ROOT/concurrent-ready"
CONCURRENT_RELEASE="$TEST_ROOT/concurrent-release"
CONCURRENT_FIRST_OUTPUT="$TEST_ROOT/concurrent-first-output"
CONCURRENT_SECOND_OUTPUT="$TEST_ROOT/concurrent-second-output"
mkdir "$CONCURRENT_NVM_HOME"
mkfifo "$CONCURRENT_READY"
FIXTURE_CURL_READY="$CONCURRENT_READY" \
FIXTURE_CURL_RELEASE="$CONCURRENT_RELEASE" \
    run_nvm_installer_for_home "$CONCURRENT_NVM_HOME" --migrate \
        > "$CONCURRENT_FIRST_OUTPUT" 2>&1 &
CONCURRENT_INSTALL_PID=$!
IFS= read -r concurrent_state < "$CONCURRENT_READY"
[ "$concurrent_state" = "ready" ] ||
    fail "concurrent NVM fixture did not reach its download boundary"
CONCURRENT_LOCK="$CONCURRENT_NVM_HOME/.nvm/.dotfiles-install.lock"
[ -f "$CONCURRENT_LOCK/owner" ] ||
    fail "active NVM install did not publish lock ownership"
CONCURRENT_OWNER=$(cat "$CONCURRENT_LOCK/owner")
if run_nvm_installer_for_home "$CONCURRENT_NVM_HOME" --migrate \
        > "$CONCURRENT_SECOND_OUTPUT" 2>&1; then
    fail "a concurrent NVM writer acquired the active install root"
fi
grep -Fq "Another or interrupted NVM install owns: $CONCURRENT_LOCK" \
    "$CONCURRENT_SECOND_OUTPUT" ||
    fail "concurrent NVM refusal omitted the exact lock path"
[ "$(cat "$CONCURRENT_LOCK/owner")" = "$CONCURRENT_OWNER" ] ||
    fail "concurrent NVM refusal changed another writer's lock"
printf 'release\n' > "$CONCURRENT_RELEASE"
if ! wait "$CONCURRENT_INSTALL_PID"; then
    cp "$CONCURRENT_FIRST_OUTPUT" "$OUTPUT"
    fail "the lock-owning NVM install did not complete"
fi
if [ -e "$CONCURRENT_LOCK" ] || [ -L "$CONCURRENT_LOCK" ]; then
    fail "the lock-owning NVM install did not release its lock"
fi
[ "$(readlink "$CONCURRENT_NVM_HOME/.nvm/.dotfiles-current")" = \
    ".dotfiles-releases/$FIXTURE_VERSION" ] ||
    fail "the lock-owning NVM install did not publish its release"

echo "platform installer production paths passed"
