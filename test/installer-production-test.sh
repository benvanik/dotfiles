#!/bin/bash
# Offline production-path fixtures for managed tool installers.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-installer-production.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-installer-production.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "installer production test: $1" >&2
    exit 1
}

file_sha256() {
    local output
    if command -v sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "$1")
    else
        output=$(shasum -a 256 "$1")
    fi
    printf '%s\n' "${output%% *}"
}

case "$(uname -s)_$(uname -m)" in
    Linux_x86_64|Linux_amd64)
        BEADS_SUFFIX="linux_amd64"
        MOLD_ARCHITECTURE="x86_64"
        ;;
    Linux_aarch64|Linux_arm64)
        BEADS_SUFFIX="linux_arm64"
        MOLD_ARCHITECTURE="aarch64"
        ;;
    Darwin_x86_64|Darwin_amd64)
        BEADS_SUFFIX="darwin_amd64"
        MOLD_ARCHITECTURE=""
        ;;
    Darwin_aarch64|Darwin_arm64)
        BEADS_SUFFIX="darwin_arm64"
        MOLD_ARCHITECTURE=""
        ;;
    *) fail "installer fixtures do not recognize the host platform" ;;
esac

# br and bv are independently versioned, attested components. Exercise the
# current upstream asset naming and exact one-member extraction without network.
BEADS_ROOT="$TEST_ROOT/beads"
BEADS_ASSETS="$BEADS_ROOT/assets"
BEADS_FAKE_BIN="$BEADS_ROOT/bin"
BEADS_HOME="$BEADS_ROOT/home"
BEADS_TOOLS="$BEADS_ROOT/tools"
BR_VERSION=9.8.7
BV_VERSION=6.5.4
BR_ASSET="br-$BR_VERSION-$BEADS_SUFFIX.tar.gz"
BV_ASSET="bv_${BEADS_SUFFIX}.tar.gz"
mkdir -p "$BEADS_ASSETS" "$BEADS_FAKE_BIN" "$BEADS_HOME"
printf '#!/bin/sh\nprintf "br %s\\n"\n' "$BR_VERSION" \
    > "$BEADS_ROOT/br"
printf '#!/bin/sh\nprintf "bv %s\\n"\n' "$BV_VERSION" \
    > "$BEADS_ROOT/bv"
chmod 755 "$BEADS_ROOT/br" "$BEADS_ROOT/bv"
tar czf "$BEADS_ASSETS/$BR_ASSET" -C "$BEADS_ROOT" br
tar czf "$BEADS_ASSETS/$BV_ASSET" -C "$BEADS_ROOT" bv
BR_ARCHIVE_SHA256=$(file_sha256 "$BEADS_ASSETS/$BR_ASSET")
BV_ARCHIVE_SHA256=$(file_sha256 "$BEADS_ASSETS/$BV_ASSET")
export \
    BEADS_ASSETS \
    BEADS_SUFFIX \
    BR_ARCHIVE_SHA256 \
    BR_ASSET \
    BR_VERSION \
    BV_ARCHIVE_SHA256 \
    BV_ASSET \
    BV_VERSION
cat > "$BEADS_FAKE_BIN/curl" << 'EOF'
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
case "$url" in
    */beads_rust/releases/latest)
        printf '{"tag_name":"v%s"}\n' "$BR_VERSION"
        ;;
    */beads_viewer/releases/latest)
        printf '{"tag_name":"v%s"}\n' "$BV_VERSION"
        ;;
    */beads_rust/releases/tags/v"$BR_VERSION")
        printf '{"assets":[{"name":"%s","digest":"sha256:%s"}]}\n' \
            "$BR_ASSET" "$BR_ARCHIVE_SHA256"
        ;;
    */beads_viewer/releases/tags/v"$BV_VERSION")
        printf '{"assets":[{"name":"%s","digest":"sha256:%s"}]}\n' \
            "$BV_ASSET" "$BV_ARCHIVE_SHA256"
        ;;
    */"$BR_ASSET")
        cp "$BEADS_ASSETS/$BR_ASSET" "$output"
        ;;
    */"$BV_ASSET")
        cp "$BEADS_ASSETS/$BV_ASSET" "$output"
        ;;
    *)
        echo "unexpected beads fixture URL: $url" >&2
        exit 72
        ;;
esac
EOF
chmod 755 "$BEADS_FAKE_BIN/curl"
PATH="$BEADS_FAKE_BIN:$PATH" \
    HOME="$BEADS_HOME" \
    TOOLS_DIR="$BEADS_TOOLS" \
    bash "$DOTFILES/tools/beads/install.sh" "$BR_VERSION" >/dev/null ||
    fail "attested beads fixture did not install"
"$BEADS_TOOLS/beads/br-$BR_VERSION/bin/br" --version |
    grep -qx "br $BR_VERSION" ||
    fail "beads install did not publish br"
"$BEADS_TOOLS/beads/bv-$BV_VERSION/bin/bv" --version |
    grep -qx "bv $BV_VERSION" ||
    fail "beads install did not publish bv"
[ "$(readlink "$BEADS_HOME/.local/bin/br")" = \
    "$BEADS_TOOLS/beads/br-$BR_VERSION/bin/br" ] ||
    fail "beads install did not atomically publish its command link"

# mold is retained as an explicit-only tool. Its whole attested archive is
# staged, namespace-validated, executed, and only then published.
if [ -n "$MOLD_ARCHITECTURE" ]; then
    MOLD_ROOT="$TEST_ROOT/mold"
    MOLD_ASSETS="$MOLD_ROOT/assets"
    MOLD_FAKE_BIN="$MOLD_ROOT/bin"
    MOLD_TOOLS="$MOLD_ROOT/tools"
    MOLD_VERSION=9.8.7
    MOLD_ARCHIVE_ROOT="mold-$MOLD_VERSION-$MOLD_ARCHITECTURE-linux"
    MOLD_ASSET="$MOLD_ARCHIVE_ROOT.tar.gz"
    mkdir -p \
        "$MOLD_ASSETS" \
        "$MOLD_FAKE_BIN" \
        "$MOLD_ROOT/archive/$MOLD_ARCHIVE_ROOT/bin" \
        "$MOLD_ROOT/archive/$MOLD_ARCHIVE_ROOT/libexec/mold"
    printf '#!/bin/sh\nprintf "mold %s\\n"\n' "$MOLD_VERSION" \
        > "$MOLD_ROOT/archive/$MOLD_ARCHIVE_ROOT/bin/mold"
    chmod 755 "$MOLD_ROOT/archive/$MOLD_ARCHIVE_ROOT/bin/mold"
    ln -s mold "$MOLD_ROOT/archive/$MOLD_ARCHIVE_ROOT/bin/ld.mold"
    ln -s ../../bin/mold \
        "$MOLD_ROOT/archive/$MOLD_ARCHIVE_ROOT/libexec/mold/ld"
    tar czf "$MOLD_ASSETS/$MOLD_ASSET" \
        -C "$MOLD_ROOT/archive" "$MOLD_ARCHIVE_ROOT"
    MOLD_ARCHIVE_SHA256=$(file_sha256 "$MOLD_ASSETS/$MOLD_ASSET")
    export MOLD_ARCHIVE_SHA256 MOLD_ASSETS MOLD_ASSET MOLD_VERSION
    cat > "$MOLD_FAKE_BIN/curl" << 'EOF'
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
case "$url" in
    */mold/releases/tags/v"$MOLD_VERSION")
        printf '{"assets":[{"name":"%s","digest":"sha256:%s"}]}\n' \
            "$MOLD_ASSET" "$MOLD_ARCHIVE_SHA256"
        ;;
    */"$MOLD_ASSET")
        cp "$MOLD_ASSETS/$MOLD_ASSET" "$output"
        ;;
    *)
        echo "unexpected mold fixture URL: $url" >&2
        exit 72
        ;;
esac
EOF
    chmod 755 "$MOLD_FAKE_BIN/curl"
    PATH="$MOLD_FAKE_BIN:$PATH" TOOLS_DIR="$MOLD_TOOLS" \
        bash "$DOTFILES/tools/mold/install.sh" "$MOLD_VERSION" >/dev/null ||
        fail "attested mold fixture did not install"
    "$MOLD_TOOLS/mold/$MOLD_VERSION/bin/mold" --version |
        grep -qx "mold $MOLD_VERSION" ||
        fail "mold install did not publish its executable"
    [ "$(readlink \
        "$MOLD_TOOLS/mold/$MOLD_VERSION/libexec/mold/ld")" = \
        "../../bin/mold" ] ||
        fail "mold install did not preserve an in-payload symlink"
fi

# hf's PyPI resolver closure is captured and validated inside a staged venv.
# A tiny uv fixture exercises the same venv/metadata/entrypoint boundary.
HF_ROOT="$TEST_ROOT/hf"
HF_FAKE_BIN="$HF_ROOT/bin"
HF_TOOLS="$HF_ROOT/tools"
HF_VERSION=1.24.0
REAL_PYTHON=$(command -v python3)
mkdir -p "$HF_FAKE_BIN"
export HF_VERSION REAL_PYTHON
cat > "$HF_FAKE_BIN/uv" << 'EOF'
#!/bin/bash
set -e
if [ "${HF_FIXTURE_UV_FORBIDDEN:-false}" = "true" ]; then
    echo "hf fixture unexpectedly rebuilt its environment" >&2
    exit 71
fi
case "$1" in
    venv)
        for argument in "$@"; do
            destination="$argument"
        done
        python_version=$("$REAL_PYTHON" -c \
            'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        python_full_version=$("$REAL_PYTHON" -c \
            'import platform; print(platform.python_version())')
        python_home=$("$REAL_PYTHON" -c \
            'import sys; print(sys.base_prefix)')
        mkdir -p \
            "$destination/bin" \
            "$destination/lib/python$python_version/site-packages/hf-$HF_VERSION.dist-info"
        printf '%s\n' \
            "home = $python_home/bin" \
            "include-system-site-packages = false" \
            "version = $python_full_version" \
            > "$destination/pyvenv.cfg"
        ln -s "$REAL_PYTHON" "$destination/bin/python"
        printf 'Metadata-Version: 2.1\nName: hf\nVersion: %s\n' "$HF_VERSION" \
            > "$destination/lib/python$python_version/site-packages/hf-$HF_VERSION.dist-info/METADATA"
        cat > "$destination/bin/hf" << INNER
#!/bin/sh
printf '%s\n' "$HF_VERSION"
INNER
        chmod 755 "$destination/bin/hf"
        ;;
    pip)
        exit 0
        ;;
    *)
        echo "unexpected uv fixture invocation: $*" >&2
        exit 73
        ;;
esac
EOF
chmod 755 "$HF_FAKE_BIN/uv"
PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
    bash "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" >/dev/null ||
    fail "staged hf fixture did not install"
[ -x "$HF_TOOLS/hf/$HF_VERSION/bin/hf" ] ||
    fail "hf install did not publish its entrypoint"
grep -qx "hf==$HF_VERSION" \
    "$HF_TOOLS/hf/$HF_VERSION/.dotfiles-python-closure" ||
    fail "hf install did not record its resolved Python closure"
PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
    bash "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" >/dev/null ||
    fail "hf install did not validate and reuse its recorded closure"

# A real parent-process SIGKILL leaves the shared publisher's durable rename
# state and bypasses the installer's EXIT cleanup. The next normal invocation
# must recover before its reuse gate and without rebuilding.
if DOTFILES_INSTALLER_TEST_FAULT=process-crash-before-publication \
        PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
        bash -c 'bash "$1" --force "$2"; status=$?; exit "$status"' hf-crash \
        "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" \
        >/dev/null 2>&1; then
    fail "hf pre-publication crash fixture returned success"
fi
[ -x "$HF_TOOLS/hf/$HF_VERSION/bin/hf" ] ||
    fail "hf pre-publication crash changed the active generation"
[ ! -e "$HF_TOOLS/hf/.publish-$HF_VERSION.lock" ] ||
    fail "hf pre-publication crash unexpectedly reached the rename journal"
find "$HF_TOOLS/hf" -path '*/.dotfiles-stage-*/bin/hf' \
    -type f -print -quit |
    grep -q . ||
    fail "hf pre-publication crash did not retain child-bound staging"
HF_FIXTURE_UV_FORBIDDEN=true \
    PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
    bash "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" >/dev/null ||
    fail "hf did not reclaim pre-publication staging before reuse"
if find "$HF_TOOLS/hf" -maxdepth 1 \
        \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
            -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "hf pre-publication replay retained abandoned transaction state"
fi

if DOTFILES_PUBLISHER_TEST_FAULT=process-crash-after-journal \
        PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
        bash -c 'bash "$1" --force "$2"; status=$?; exit "$status"' hf-crash \
        "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" \
        >/dev/null 2>&1; then
    fail "hf post-journal crash fixture returned success"
fi
[ -x "$HF_TOOLS/hf/$HF_VERSION/bin/hf" ] ||
    fail "hf post-journal crash changed the active generation"
[ -f "$HF_TOOLS/hf/.publish-$HF_VERSION.lock" ] ||
    fail "hf post-journal crash did not retain its journal"
find "$HF_TOOLS/hf" -path '*/.dotfiles-stage-*/bin/hf' \
    -type f -print -quit |
    grep -q . ||
    fail "hf post-journal crash did not retain staged payload"
HF_FIXTURE_UV_FORBIDDEN=true \
    PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
    bash "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" >/dev/null ||
    fail "hf did not recover a post-journal crash before reuse"
[ -x "$HF_TOOLS/hf/$HF_VERSION/bin/hf" ] ||
    fail "hf post-journal replay lost the active generation"
if find "$HF_TOOLS/hf" -maxdepth 1 \
        \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
            -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "hf post-journal replay retained abandoned transaction state"
fi

if DOTFILES_PUBLISHER_TEST_FAULT=process-crash-after-previous-rename \
        PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
        bash -c 'bash "$1" --force "$2"; status=$?; exit "$status"' hf-crash \
        "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" \
        >/dev/null 2>&1; then
    fail "hf prior-displacement crash fixture returned success"
fi
[ ! -e "$HF_TOOLS/hf/$HF_VERSION" ] ||
    fail "hf prior-displacement crash did not reach its rename window"
[ -f "$HF_TOOLS/hf/.publish-$HF_VERSION.lock" ] ||
    fail "hf prior-displacement crash did not retain its journal"
find "$HF_TOOLS/hf" -path '*/.dotfiles-stage-*/bin/hf' \
    -type f -print -quit |
    grep -q . ||
    fail "hf prior-displacement crash did not retain staged payload"
HF_FIXTURE_UV_FORBIDDEN=true \
    PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
    bash "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" >/dev/null ||
    fail "hf did not restore an interrupted prior generation before reuse"
[ -x "$HF_TOOLS/hf/$HF_VERSION/bin/hf" ] ||
    fail "hf prior-displacement replay did not restore the active generation"
if find "$HF_TOOLS/hf" -maxdepth 1 \
        \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
            -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "hf prior-displacement replay retained abandoned transaction state"
fi

if DOTFILES_PUBLISHER_TEST_FAULT=process-crash-after-payload-rename \
        PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
        bash -c 'bash "$1" --force "$2"; status=$?; exit "$status"' hf-crash \
        "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" \
        >/dev/null 2>&1; then
    fail "hf committed-publication crash fixture returned success"
fi
[ -x "$HF_TOOLS/hf/$HF_VERSION/bin/hf" ] ||
    fail "hf committed-publication crash lost the new generation"
[ -f "$HF_TOOLS/hf/.publish-$HF_VERSION.lock" ] ||
    fail "hf committed-publication crash did not retain its journal"
HF_FIXTURE_UV_FORBIDDEN=true \
    PATH="$HF_FAKE_BIN:$PATH" TOOLS_DIR="$HF_TOOLS" \
    bash "$DOTFILES/tools/hf/install.sh" "$HF_VERSION" >/dev/null ||
    fail "hf did not complete committed publication before reuse"
if find "$HF_TOOLS/hf" -maxdepth 1 \
        \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
            -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "hf committed replay retained abandoned transaction state"
fi

# The ROCm fixture proves the flattened archive remains a conventional SDK root
# after the staged payload is renamed into its final version.
if [ "$(uname -s)" = "Linux" ]; then
    ROCM_ROOT="$TEST_ROOT/rocm"
    ROCM_ASSET_ROOT="$ROCM_ROOT/asset-root"
    ROCM_FAKE_BIN="$ROCM_ROOT/bin"
    ROCM_TOOLS="$ROCM_ROOT/tools"
    ROCM_VERSION=7.14.0a20260612
    ROCM_TARGET=gfx1150
    ROCM_ARCHIVE_NAME="therock-dist-linux-$ROCM_TARGET-$ROCM_VERSION.tar.gz"
    ROCM_ARCHIVE="$ROCM_ROOT/$ROCM_ARCHIVE_NAME"
    ROCM_INDEX_LOG="$ROCM_ROOT/index-url"
    ROCM_DOWNLOAD_LOG="$ROCM_ROOT/download-url"
    mkdir -p \
        "$ROCM_ASSET_ROOT/bin" \
        "$ROCM_ASSET_ROOT/include/hip" \
        "$ROCM_ASSET_ROOT/lib/cmake/hip" \
        "$ROCM_ASSET_ROOT/share" \
        "$ROCM_FAKE_BIN"
    printf 'fixture hip runtime\n' \
        > "$ROCM_ASSET_ROOT/include/hip/hip_runtime.h"
    printf 'fixture hip runtime library\n' \
        > "$ROCM_ASSET_ROOT/lib/libamdhip64.so"
    printf 'fixture hip cmake\n' \
        > "$ROCM_ASSET_ROOT/lib/cmake/hip/hip-config.cmake"
    for binary in hipcc hipconfig; do
        printf '#!/bin/sh\nprintf "fixture %s\\n"\n' "$binary" \
            > "$ROCM_ASSET_ROOT/bin/$binary"
        chmod 755 "$ROCM_ASSET_ROOT/bin/$binary"
    done
    tar czf "$ROCM_ARCHIVE" -C "$ROCM_ASSET_ROOT" .
    ROCM_ARCHIVE_SHA256=$(file_sha256 "$ROCM_ARCHIVE")
    export \
        ROCM_ARCHIVE \
        ROCM_DOWNLOAD_LOG \
        ROCM_INDEX_LOG \
        ROCM_VERSION
    cat > "$ROCM_FAKE_BIN/curl" << 'EOF'
#!/bin/bash
set -e
if [ "$#" -eq 2 ] && [ "$1" = "-fsSL" ]; then
    printf '%s\n' "$2" > "$ROCM_INDEX_LOG"
    cat << INDEX
{"name": "therock-dist-linux-gfx1150-7.14.0a20260611.tar.gz"},
{"name": "therock-dist-linux-gfx1100-9.99.0a20990101.tar.gz"},
{"name": "therock-dist-linux-gfx1150-$ROCM_VERSION.tar.gz"}
INDEX
    exit 0
fi

output=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o)
            shift
            output="$1"
            ;;
        -*) ;;
        *) url="$1" ;;
    esac
    shift
done
printf '%s\n' "$url" > "$ROCM_DOWNLOAD_LOG"
cp "$ROCM_ARCHIVE" "$output"
EOF
    chmod 755 "$ROCM_FAKE_BIN/curl"
    ROCM_GPU_TARGET="$ROCM_TARGET" \
        PATH="$ROCM_FAKE_BIN:$PATH" TOOLS_DIR="$ROCM_TOOLS" \
        bash "$DOTFILES/tools/rocm/install.sh" >/dev/null ||
        fail "staged ROCm fixture did not install"
    ROCM_INSTALL="$ROCM_TOOLS/rocm/$ROCM_VERSION"
    grep -qx 'https://rocm.nightlies.amd.com/tarball/' \
        "$ROCM_INDEX_LOG" ||
        fail "ROCm latest lookup did not use the flattened tarball index"
    grep -qx \
        "https://rocm.nightlies.amd.com/tarball/$ROCM_ARCHIVE_NAME" \
        "$ROCM_DOWNLOAD_LOG" ||
        fail "ROCm did not download the latest exact-target tarball"
    for directory in bin include lib share; do
        if [ ! -d "$ROCM_INSTALL/$directory" ] ||
                [ -L "$ROCM_INSTALL/$directory" ]; then
            fail "ROCm did not publish an ordinary $directory directory"
        fi
    done
    if [ -e "$ROCM_INSTALL/.venv" ] ||
            [ -e "$ROCM_INSTALL/pyvenv.cfg" ]; then
        fail "ROCm retained Python packaging state"
    fi
    [ -f "$ROCM_INSTALL/include/hip/hip_runtime.h" ] ||
        fail "ROCm SDK payload broke after publication"
    "$ROCM_INSTALL/bin/hipcc" | grep -qx 'fixture hipcc' ||
        fail "published ROCm compiler is not executable"
    grep -qx "gpu_target=$ROCM_TARGET" \
        "$ROCM_INSTALL/.dotfiles-rocm-installation" ||
        fail "ROCm install did not record its GPU-target identity"
    grep -qx \
        "sha256=$ROCM_ARCHIVE_SHA256" \
        "$ROCM_INSTALL/.dotfiles-rocm-installation" ||
        fail "ROCm install did not record its archive identity"
    [ "$(readlink "$ROCM_TOOLS/rocm/latest")" = "$ROCM_VERSION" ] ||
        fail "ROCm latest selector does not name the installed version"
    if PATH="$ROCM_FAKE_BIN:$PATH" TOOLS_DIR="$ROCM_TOOLS" \
            bash "$DOTFILES/tools/rocm/install.sh" \
            "$ROCM_VERSION" gfx90a >/dev/null 2>&1; then
        fail "ROCm silently reused one version for another GPU target"
    fi
    PATH="$ROCM_FAKE_BIN:$PATH" TOOLS_DIR="$ROCM_TOOLS" \
        bash "$DOTFILES/tools/rocm/install.sh" \
        --force "$ROCM_VERSION" gfx90a >/dev/null ||
        fail "ROCm did not transactionally replace a requested GPU target"
    grep -qx 'gpu_target=gfx90a' \
        "$ROCM_INSTALL/.dotfiles-rocm-installation" ||
        fail "forced ROCm replacement retained the old GPU-target identity"
fi

echo "offline installer production paths passed"
