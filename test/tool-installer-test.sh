#!/bin/bash
# Safety coverage for tool installer paths, arguments, and attestations.

set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-cuda-install-test.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-cuda-install-test.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "tool installer test: $1" >&2
    if [ -f "${OUTPUT:-}" ] && [ -s "$OUTPUT" ]; then
        sed 's/^/  installer output: /' "$OUTPUT" >&2
    fi
    exit 1
}

TEST_TOOLS="$TEST_ROOT/tools"
SENTINEL="$TEST_TOOLS/sentinel"
OUTPUT="$TEST_ROOT/output"
mkdir -p "$TEST_TOOLS"
printf 'retain\n' > "$SENTINEL"

expect_rejected() {
    if TOOLS_DIR="$TEST_TOOLS" \
        bash "$DOTFILES/tools/cuda/install.sh" "$@" >"$OUTPUT" 2>&1; then
        fail "unsafe or ambiguous arguments were accepted: $*"
    fi
    [ -f "$SENTINEL" ] || fail "argument rejection damaged the tools root"
    [ ! -e "$TEST_TOOLS/cuda" ] ||
        fail "argument rejection created a CUDA installation root"
}

expect_rejected ".."
expect_rejected "../escape"
expect_rejected "12.9"
expect_rejected "12.9.1/escape"
expect_rejected "12.9.1" "13.0.0"
expect_rejected --unknown

# Bazel's version is also a managed installation path.
expect_bazel_rejected() {
    if TOOLS_DIR="$TEST_TOOLS" \
        bash "$DOTFILES/tools/bazel/install.sh" "$@" >"$OUTPUT" 2>&1; then
        fail "unsafe Bazel arguments were accepted: $*"
    fi
    [ -f "$SENTINEL" ] || fail "Bazel argument rejection damaged the tools root"
    [ ! -e "$TEST_TOOLS/bazel" ] ||
        fail "Bazel argument rejection created an installation root"
}
expect_bazel_rejected ".."
expect_bazel_rejected "8.2.1" "8.2.0"

# The shared checksum verifier must work through the host's supported SHA-256
# implementation and reject a mismatch.
TOOL_NAME="installer-test"
# shellcheck source=../tools/install-utils.sh
source "$DOTFILES/tools/install-utils.sh"
CHECKSUM_PAYLOAD="$TEST_ROOT/checksum-payload"
printf 'abc' > "$CHECKSUM_PAYLOAD"
verify_sha256 \
    "$CHECKSUM_PAYLOAD" \
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad" ||
    fail "valid SHA-256 was rejected"
if verify_sha256 \
    "$CHECKSUM_PAYLOAD" \
    "0000000000000000000000000000000000000000000000000000000000000000" \
    >/dev/null 2>&1; then
    fail "invalid SHA-256 was accepted"
fi

if [ "$(uname -s)" = "Linux" ]; then
    case "$(uname -m)" in
        x86_64|amd64)
            CUDA_PLATFORM_KEY="linux-x86_64"
            CUDA_PLATFORM_PACKAGES="cuda_opencl"
            ;;
        aarch64|arm64)
            CUDA_PLATFORM_KEY="linux-sbsa"
            CUDA_PLATFORM_PACKAGES=""
            ;;
        *) fail "test does not recognize the host architecture" ;;
    esac

    CUDA_CORE_PACKAGES="cuda_cudart cuda_cccl cuda_nvcc"
    CUDA_FULL_PACKAGES="cuda_cudart cuda_nvcc cuda_cccl cuda_cupti cuda_nvdisasm
cuda_nvml_dev cuda_nvrtc cuda_nvtx cuda_profiler_api
libcublas libcufft libcufile libcurand libcusolver libcusparse
libnpp libnvfatbin libnvjitlink libnvjpeg${CUDA_PLATFORM_PACKAGES:+ $CUDA_PLATFORM_PACKAGES}"
    CUDA_ASSET_DIR="$TEST_ROOT/cuda-assets"
    CUDA_FIXTURE_PARENT="$TEST_ROOT/cuda-fixtures"
    CUDA_FAKE_BIN="$TEST_ROOT/fake-bin"
    mkdir -p "$CUDA_ASSET_DIR" "$CUDA_FIXTURE_PARENT" "$CUDA_FAKE_BIN"

    file_sha256() {
        if command -v sha256sum >/dev/null 2>&1; then
            sha256sum "$1" | cut -d' ' -f1
        else
            shasum -a 256 "$1" | cut -d' ' -f1
        fi
    }

    write_cuda_manifest() {
        local output="$1"
        local archive_name="$2"
        local archive_sha256="$3"
        local packages="$4"
        local separator=""
        local package

        {
            printf '{\n'
            for package in $packages; do
                printf '%s  "%s": {"%s": {' \
                    "$separator" "$package" "$CUDA_PLATFORM_KEY"
                printf '"relative_path": "fixtures/%s", ' "$archive_name"
                printf '"sha256": "%s"}}' "$archive_sha256"
                separator=$',\n'
            done
            printf '\n}\n'
        } > "$output"
    }

    create_cuda_archive() {
        local fixture_name="$1"
        local archive_name="$2"
        local fixture_root="$CUDA_FIXTURE_PARENT/$fixture_name/cuda-fixture"

        mkdir -p \
            "$fixture_root/include" \
            "$fixture_root/bin" \
            "$fixture_root/nvvm/libdevice"
        printf 'fixture cuda header\n' > "$fixture_root/include/cuda.h"
        printf '#!/bin/sh\nexit 0\n' > "$fixture_root/bin/nvcc"
        chmod 755 "$fixture_root/bin/nvcc"
        printf 'fixture libdevice\n' \
            > "$fixture_root/nvvm/libdevice/libdevice.10.bc"
        tar cf "$CUDA_ASSET_DIR/$archive_name" \
            -C "$CUDA_FIXTURE_PARENT/$fixture_name" cuda-fixture
    }

    create_cuda_archive "complete" "complete.tar"
    COMPLETE_SHA256=$(file_sha256 "$CUDA_ASSET_DIR/complete.tar")
    CORE_MANIFEST="$TEST_ROOT/core-manifest.json"
    FULL_MANIFEST="$TEST_ROOT/full-manifest.json"
    write_cuda_manifest \
        "$CORE_MANIFEST" "complete.tar" "$COMPLETE_SHA256" \
        "$CUDA_CORE_PACKAGES"
    write_cuda_manifest \
        "$FULL_MANIFEST" "complete.tar" "$COMPLETE_SHA256" \
        "$CUDA_FULL_PACKAGES"

    MISSING_NVCC_ROOT="$CUDA_FIXTURE_PARENT/missing-nvcc/cuda-fixture"
    mkdir -p "$MISSING_NVCC_ROOT/include" "$MISSING_NVCC_ROOT/nvvm/libdevice"
    printf 'fixture cuda header\n' > "$MISSING_NVCC_ROOT/include/cuda.h"
    printf 'fixture libdevice\n' \
        > "$MISSING_NVCC_ROOT/nvvm/libdevice/libdevice.10.bc"
    tar cf "$CUDA_ASSET_DIR/missing-nvcc.tar" \
        -C "$CUDA_FIXTURE_PARENT/missing-nvcc" cuda-fixture
    MISSING_NVCC_SHA256=$(file_sha256 "$CUDA_ASSET_DIR/missing-nvcc.tar")
    MISSING_NVCC_MANIFEST="$TEST_ROOT/missing-nvcc-manifest.json"
    write_cuda_manifest \
        "$MISSING_NVCC_MANIFEST" "missing-nvcc.tar" \
        "$MISSING_NVCC_SHA256" "$CUDA_CORE_PACKAGES"

    # This link is safe while the archive's top-level root exists, but escapes
    # the payload after package roots are merged away. The archive validator
    # must account for that namespace rebase before GNU tar writes anything.
    REBASED_LINK_ROOT="$CUDA_FIXTURE_PARENT/rebased-link/cuda-fixture"
    mkdir -p "$REBASED_LINK_ROOT/a"
    printf 'outside payload after merge\n' > "$REBASED_LINK_ROOT/outside"
    ln -s ../../cuda-fixture/outside "$REBASED_LINK_ROOT/a/link"
    tar cf "$CUDA_ASSET_DIR/rebased-link.tar" \
        -C "$CUDA_FIXTURE_PARENT/rebased-link" cuda-fixture
    REBASED_LINK_SHA256=$(file_sha256 "$CUDA_ASSET_DIR/rebased-link.tar")
    REBASED_LINK_MANIFEST="$TEST_ROOT/rebased-link-manifest.json"
    write_cuda_manifest \
        "$REBASED_LINK_MANIFEST" "rebased-link.tar" \
        "$REBASED_LINK_SHA256" "$CUDA_CORE_PACKAGES"

    cat > "$CUDA_FAKE_BIN/curl" <<'EOF'
#!/bin/bash
set -e
output=""
url=""
write_out=""
head_request=false
while [ $# -gt 0 ]; do
    case "$1" in
        -o)
            shift
            output="$1"
            ;;
        -w|--write-out)
            shift
            write_out="$1"
            ;;
        -I|--head)
            head_request=true
            ;;
        -*I*)
            head_request=true
            ;;
        -C)
            shift
            ;;
        -*)
            ;;
        *)
            url="$1"
            ;;
    esac
    shift
done
write_head_result() {
    local response_code="$1"

    if [ -n "$write_out" ]; then
        [ "$write_out" = '%{http_code}' ]
        printf '%s' "$response_code"
    else
        # Model an HTTPS proxy response followed by the origin response. The
        # old `curl | head -1` probe mistook this CONNECT status for success.
        printf 'HTTP/1.1 200 Connection established\r\n\r\n'
        printf 'HTTP/2 %s fixture\r\n\r\n' "$response_code"
    fi
    if [ "$response_code" != "200" ]; then
        exit 22
    fi
}
case "$url" in
    */redistrib_13.9.3.json)
        if [ "$head_request" = true ]; then
            write_head_result 404
        fi
        cat "$CUDA_TEST_MANIFEST"
        ;;
    */redistrib_13.9.2.json)
        if [ "$head_request" = true ]; then
            write_head_result 200
            exit 0
        fi
        cat "$CUDA_TEST_MANIFEST"
        ;;
    */redistrib_*.json)
        if [ "$head_request" = true ]; then
            write_head_result 404
        fi
        cat "$CUDA_TEST_MANIFEST"
        ;;
    *)
        [ -n "$output" ]
        cp "$CUDA_TEST_ASSET_DIR/${url##*/}" "$output"
        ;;
esac
EOF
    chmod 755 "$CUDA_FAKE_BIN/curl"

    run_cuda_install() {
        local tools_dir="$1"
        local manifest="$2"
        shift 2

        PATH="$CUDA_FAKE_BIN:$PATH" \
            TOOLS_DIR="$tools_dir" \
            CUDA_TEST_MANIFEST="$manifest" \
            CUDA_TEST_ASSET_DIR="$CUDA_ASSET_DIR" \
            DOTFILES_INSTALLER_TEST_FAULT="${DOTFILES_INSTALLER_TEST_FAULT:-}" \
            DOTFILES_PUBLISHER_TEST_FAULT="${DOTFILES_PUBLISHER_TEST_FAULT:-}" \
            bash "$DOTFILES/tools/cuda/install.sh" "$@"
    }

    expect_cuda_install_failure() {
        local failure_name="$1"
        local tools_dir="$2"
        local manifest="$3"
        shift 3

        if run_cuda_install "$tools_dir" "$manifest" "$@" \
                > "$OUTPUT" 2>&1; then
            fail "$failure_name was accepted"
        fi
    }

    # A complete core install publishes nvcc and an exact core closure record.
    CORE_TOOLS="$TEST_ROOT/core-success/tools"
    run_cuda_install "$CORE_TOOLS" "$CORE_MANIFEST" 13.0.1 \
        > "$OUTPUT" 2>&1 ||
        fail "valid core CUDA closure failed to install"
    CORE_INSTALL="$CORE_TOOLS/cuda/13.0.1"
    [ -x "$CORE_INSTALL/bin/nvcc" ] ||
        fail "core CUDA install omitted executable nvcc"
    grep -qx 'profile=core' "$CORE_INSTALL/.dotfiles-cuda-closure" ||
        fail "core CUDA install has no core closure identity"
    [ "$(readlink "$CORE_TOOLS/cuda/latest")" = "13.0.1" ] ||
        fail "core CUDA install did not publish latest"

    # Latest probing must use curl's final origin status and preserve its exit
    # code. An HTTPS proxy's CONNECT 200 cannot turn an origin 404 into a
    # selected CUDA release.
    LATEST_TOOLS="$TEST_ROOT/latest-probe/tools"
    run_cuda_install "$LATEST_TOOLS" "$CORE_MANIFEST" > "$OUTPUT" 2>&1 ||
        fail "CUDA latest probe rejected the first real origin success"
    [ "$(readlink "$LATEST_TOOLS/cuda/latest")" = "13.9.2" ] ||
        fail "CUDA latest probe selected a proxy CONNECT response"
    [ ! -e "$LATEST_TOOLS/cuda/13.9.3" ] ||
        fail "CUDA latest probe published an origin 404"

    # Requesting full against that exact version is an atomic closure upgrade,
    # while a later core request accepts the full superset without downgrading.
    printf 'old core generation\n' > "$CORE_INSTALL/core-generation"
    run_cuda_install "$CORE_TOOLS" "$FULL_MANIFEST" --full 13.0.1 \
        > "$OUTPUT" 2>&1 ||
        fail "core CUDA closure did not expand to full"
    grep -qx 'profile=full' "$CORE_INSTALL/.dotfiles-cuda-closure" ||
        fail "full CUDA install retained the core closure identity"
    [ ! -e "$CORE_INSTALL/core-generation" ] ||
        fail "full CUDA closure was merged into the core generation"
    CUDA_TEST_MANIFEST="$TEST_ROOT/manifest-must-not-be-read" \
        run_cuda_install \
            "$CORE_TOOLS" "$TEST_ROOT/manifest-must-not-be-read" 13.0.1 \
            > "$OUTPUT" 2>&1 ||
        fail "full CUDA closure did not satisfy a later core request"
    grep -qx 'profile=full' "$CORE_INSTALL/.dotfiles-cuda-closure" ||
        fail "core request downgraded an installed full CUDA closure"

    # A checksum mismatch fails before publication and leaves no version path.
    BAD_HASH_MANIFEST="$TEST_ROOT/bad-hash-manifest.json"
    write_cuda_manifest \
        "$BAD_HASH_MANIFEST" "complete.tar" \
        "0000000000000000000000000000000000000000000000000000000000000000" \
        "$CUDA_CORE_PACKAGES"
    BAD_HASH_TOOLS="$TEST_ROOT/bad-hash/tools"
    expect_cuda_install_failure \
        "CUDA archive with the wrong checksum" \
        "$BAD_HASH_TOOLS" "$BAD_HASH_MANIFEST" 13.0.2
    [ ! -e "$BAD_HASH_TOOLS/cuda/13.0.2" ] ||
        fail "checksum failure published a CUDA version"

    # Headers and libdevice alone are not an SDK: the compiler exported by
    # env.sh is part of the minimum accepted core closure.
    MISSING_NVCC_TOOLS="$TEST_ROOT/missing-nvcc/tools"
    expect_cuda_install_failure \
        "CUDA core closure without executable nvcc" \
        "$MISSING_NVCC_TOOLS" "$MISSING_NVCC_MANIFEST" 13.0.8
    grep -q 'executable bin/nvcc not found' "$OUTPUT" ||
        fail "missing CUDA compiler failed for the wrong reason"
    [ ! -e "$MISSING_NVCC_TOOLS/cuda/13.0.8" ] ||
        fail "CUDA closure without nvcc was published"

    # Selected packages are a closed set: a missing manifest entry is fatal.
    MISSING_PACKAGE_MANIFEST="$TEST_ROOT/missing-package-manifest.json"
    write_cuda_manifest \
        "$MISSING_PACKAGE_MANIFEST" "complete.tar" "$COMPLETE_SHA256" \
        "cuda_cudart cuda_cccl"
    MISSING_PACKAGE_TOOLS="$TEST_ROOT/missing-package/tools"
    expect_cuda_install_failure \
        "CUDA manifest with a missing selected package" \
        "$MISSING_PACKAGE_TOOLS" "$MISSING_PACKAGE_MANIFEST" 13.0.3
    grep -q 'Required core package cuda_nvcc is unavailable' "$OUTPUT" ||
        fail "missing CUDA package failure was not explicit"

    # Safe links in an extracted package must remain safe after its archive
    # root is stripped for the merged SDK payload.
    REBASED_LINK_TOOLS="$TEST_ROOT/rebased-link/tools"
    expect_cuda_install_failure \
        "CUDA archive link escaping after package merge" \
        "$REBASED_LINK_TOOLS" "$REBASED_LINK_MANIFEST" 13.0.4
    grep -q 'archive link escapes after package merge' "$OUTPUT" ||
        fail "rebased CUDA archive link failed for the wrong reason"

    # A graceful failure after displacing the prior generation restores that
    # exact generation and removes the transaction journal.
    ROLLBACK_TOOLS="$TEST_ROOT/rollback/tools"
    run_cuda_install "$ROLLBACK_TOOLS" "$CORE_MANIFEST" 13.0.5 \
        > "$OUTPUT" 2>&1 ||
        fail "CUDA rollback fixture could not install its prior generation"
    printf 'prior generation\n' \
        > "$ROLLBACK_TOOLS/cuda/13.0.5/prior-generation"
    DOTFILES_PUBLISHER_TEST_FAULT=after-previous-rename \
        expect_cuda_install_failure \
            "failed forced CUDA publication" \
            "$ROLLBACK_TOOLS" "$CORE_MANIFEST" --force 13.0.5
    [ -f "$ROLLBACK_TOOLS/cuda/13.0.5/prior-generation" ] ||
        fail "failed CUDA publication did not restore the prior generation"
    if find "$ROLLBACK_TOOLS/cuda" -maxdepth 1 \
            \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
                -o -name '.publish-*.lock' \) \
            -print -quit | grep -q .; then
        fail "successful CUDA rollback retained transaction scratch"
    fi

    # Whole-installer death before the publication journal leaves only a
    # child-bound staging root. The next invocation reclaims it under the
    # kernel guard before any manifest request or rebuild.
    PRE_JOURNAL_TOOLS="$TEST_ROOT/pre-journal/tools"
    run_cuda_install "$PRE_JOURNAL_TOOLS" "$CORE_MANIFEST" 13.0.6 \
        > "$OUTPUT" 2>&1 ||
        fail "CUDA pre-journal fixture could not install its prior generation"
    printf 'prior generation\n' \
        > "$PRE_JOURNAL_TOOLS/cuda/13.0.6/prior-generation"
    DOTFILES_INSTALLER_TEST_FAULT=process-crash-before-publication \
        expect_cuda_install_failure \
            "pre-journal CUDA process death" \
            "$PRE_JOURNAL_TOOLS" "$CORE_MANIFEST" --force 13.0.6
    if ! find "$PRE_JOURNAL_TOOLS/cuda" -maxdepth 1 \
            -name '.dotfiles-stage-13.0.6.*' -type d \
            -print -quit | grep -q .; then
        fail "pre-journal CUDA death did not retain owned staging"
    fi
    CUDA_TEST_MANIFEST="$TEST_ROOT/manifest-must-not-be-read" \
        run_cuda_install \
            "$PRE_JOURNAL_TOOLS" "$TEST_ROOT/manifest-must-not-be-read" \
            13.0.6 > "$OUTPUT" 2>&1 ||
        fail "CUDA did not reclaim pre-journal staging before reuse"
    [ -f "$PRE_JOURNAL_TOOLS/cuda/13.0.6/prior-generation" ] ||
        fail "pre-journal CUDA recovery changed the active generation"
    if find "$PRE_JOURNAL_TOOLS/cuda" -maxdepth 1 \
            -name '.dotfiles-stage-13.0.6.*' -print -quit | grep -q .; then
        fail "pre-journal CUDA recovery retained abandoned staging"
    fi

    # Whole-installer death after the prior rename is rolled back by the next
    # invocation before its existing-installation reuse gate.
    HARD_ROLLBACK_TOOLS="$TEST_ROOT/hard-rollback/tools"
    run_cuda_install "$HARD_ROLLBACK_TOOLS" "$CORE_MANIFEST" 13.0.7 \
        > "$OUTPUT" 2>&1 ||
        fail "CUDA hard-rollback fixture could not install its prior generation"
    printf 'prior generation\n' \
        > "$HARD_ROLLBACK_TOOLS/cuda/13.0.7/prior-generation"
    DOTFILES_PUBLISHER_TEST_FAULT=process-crash-after-previous-rename \
        expect_cuda_install_failure \
            "hard-killed CUDA rollback publication" \
            "$HARD_ROLLBACK_TOOLS" "$CORE_MANIFEST" --force 13.0.7
    [ ! -e "$HARD_ROLLBACK_TOOLS/cuda/13.0.7" ] ||
        fail "hard-killed CUDA rollback did not reach its rename window"
    CUDA_TEST_MANIFEST="$TEST_ROOT/manifest-must-not-be-read" \
        run_cuda_install \
            "$HARD_ROLLBACK_TOOLS" "$TEST_ROOT/manifest-must-not-be-read" \
            13.0.7 > "$OUTPUT" 2>&1 ||
        fail "CUDA did not replay its hard-killed rollback before reuse"
    [ -f "$HARD_ROLLBACK_TOOLS/cuda/13.0.7/prior-generation" ] ||
        fail "hard-killed CUDA rollback did not restore the prior generation"

    # The payload rename is the durable commit point. Death immediately after
    # it leaves the complete new generation active; replay only finishes old
    # generation and transaction cleanup.
    HARD_COMMIT_TOOLS="$TEST_ROOT/hard-commit/tools"
    run_cuda_install "$HARD_COMMIT_TOOLS" "$CORE_MANIFEST" 13.0.9 \
        > "$OUTPUT" 2>&1 ||
        fail "CUDA hard-commit fixture could not install its prior generation"
    printf 'prior generation\n' \
        > "$HARD_COMMIT_TOOLS/cuda/13.0.9/prior-generation"
    DOTFILES_PUBLISHER_TEST_FAULT=process-crash-after-payload-rename \
        expect_cuda_install_failure \
            "hard-killed committed CUDA publication" \
            "$HARD_COMMIT_TOOLS" "$CORE_MANIFEST" --force 13.0.9
    grep -qx 'fixture cuda header' \
        "$HARD_COMMIT_TOOLS/cuda/13.0.9/include/cuda.h" ||
        fail "hard-killed CUDA commit lost the complete new generation"
    [ ! -e "$HARD_COMMIT_TOOLS/cuda/13.0.9/prior-generation" ] ||
        fail "hard-killed CUDA commit exposed a mixed generation"
    CUDA_TEST_MANIFEST="$TEST_ROOT/manifest-must-not-be-read" \
        run_cuda_install \
            "$HARD_COMMIT_TOOLS" "$TEST_ROOT/manifest-must-not-be-read" \
            13.0.9 > "$OUTPUT" 2>&1 ||
        fail "CUDA did not finish its hard-killed commit before reuse"
    if find "$HARD_COMMIT_TOOLS/cuda" -maxdepth 1 \
            \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
                -o -name '.publish-*.lock' \) \
            -print -quit | grep -q .; then
        fail "hard-killed CUDA commit replay retained transaction state"
    fi
fi

echo "tool installer path and checksum safety passed"
