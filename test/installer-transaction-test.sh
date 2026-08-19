#!/bin/bash
# Production-path tests for shared installer ownership and publication.
set -euo pipefail

DOTFILES="${DOTFILES:-$HOME/.dotfiles}"
TEST_PARENT="${TMPDIR:-/tmp}"
TEST_ROOT=$(mktemp -d "$TEST_PARENT/dotfiles-installer-transaction.XXXXXX")
cleanup_test_root() {
    case "$TEST_ROOT" in
        "$TEST_PARENT"/dotfiles-installer-transaction.*) ;;
        *)
            echo "refusing unexpected test cleanup path: $TEST_ROOT" >&2
            return 1
            ;;
    esac
    [ ! -e "$TEST_ROOT" ] || find "$TEST_ROOT" -xdev -depth -delete
}
trap cleanup_test_root EXIT

fail() {
    echo "installer transaction test: $1" >&2
    exit 1
}

TOOL_NAME="installer-transaction-test"
# shellcheck source=../tools/install-utils.sh
source "$DOTFILES/tools/install-utils.sh"

validate_version_component "1.2.3" "test version" ||
    fail "ordinary version component was rejected"
if validate_version_component "latest" "test version" >/dev/null 2>&1; then
    fail "reserved latest selector was accepted as an installed version"
fi
if validate_version_component "../escape" "test version" >/dev/null 2>&1; then
    fail "traversing version component was accepted"
fi
OVERSIZED_COMPONENT=$(printf 'a%.0s' {1..129})
if validate_version_component \
        "$OVERSIZED_COMPONENT" "test version" >/dev/null 2>&1; then
    fail "oversized version component was accepted"
fi
if github_release_asset_sha256 \
        "owner/repository/escape" v1.0.0 asset.tar.gz >/dev/null 2>&1; then
    fail "invalid GitHub repository identity reached the API"
fi
if github_release_asset_selection \
        owner/repository v1.0.0 "../asset" >/dev/null 2>&1; then
    fail "invalid GitHub asset candidate reached the API"
fi
(
    export FORCE=true
    # shellcheck source=../tools/install-utils.sh
    source "$DOTFILES/tools/install-utils.sh"
    [ "$FORCE" = "false" ]
) || fail "ambient FORCE authorized destructive replacement"

CHECKSUM_PAYLOAD="$TEST_ROOT/checksum"
printf 'abc' > "$CHECKSUM_PAYLOAD"
ln -s checksum "$TEST_ROOT/checksum-link"
if verify_sha256 "$TEST_ROOT/checksum-link" \
        ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad \
        >/dev/null 2>&1; then
    fail "checksum verifier followed a symlinked artifact"
fi
if verify_sha256 "$CHECKSUM_PAYLOAD" "" >/dev/null 2>&1; then
    fail "checksum verifier accepted an empty expected identity"
fi

ARCHIVE_FIXTURE="$TEST_ROOT/archive-fixture"
mkdir -p "$ARCHIVE_FIXTURE/root/a"
ln -s ../../escape "$ARCHIVE_FIXTURE/root/a/link"
tar cf "$TEST_ROOT/escaping-link.tar" -C "$ARCHIVE_FIXTURE" root
if validate_single_root_tar_archive \
        "$TEST_ROOT/escaping-link.tar" root >/dev/null 2>&1; then
    fail "tar validator accepted a link escaping the published payload"
fi

SELECTOR_ROOT="$TEST_ROOT/selector"
mkdir -p "$SELECTOR_ROOT/1.2.3"
update_latest "$SELECTOR_ROOT" 1.2.3 >/dev/null
[ "$(readlink "$SELECTOR_ROOT/latest")" = "1.2.3" ] ||
    fail "latest selector did not publish the requested version"
unlink "$SELECTOR_ROOT/latest"
mkdir "$SELECTOR_ROOT/latest"
if update_latest "$SELECTOR_ROOT" 1.2.3 >/dev/null 2>&1; then
    fail "latest publication replaced an ordinary directory"
fi
[ -d "$SELECTOR_ROOT/latest" ] ||
    fail "failed latest publication damaged its ordinary destination"

PUBLISH_ROOT="$TEST_ROOT/publish"
mkdir -p "$PUBLISH_ROOT/1.0.0"
PUBLISH_STAGE=$(create_managed_staging_directory "$PUBLISH_ROOT" 1.0.0)
mkdir "$PUBLISH_STAGE/payload"
printf 'old\n' > "$PUBLISH_ROOT/1.0.0/generation"
printf 'new\n' > "$PUBLISH_STAGE/payload/generation"
publish_staged_directory \
    "$PUBLISH_ROOT" 1.0.0 "$PUBLISH_STAGE/payload"
grep -qx new "$PUBLISH_ROOT/1.0.0/generation" ||
    fail "successful publication did not activate the staged generation"
if find "$PUBLISH_ROOT" -maxdepth 1 \
        \( -name '.replace-*' -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "successful publication retained transaction state"
fi

LOCK_ROOT="$TEST_ROOT/lock-owned"
mkdir -p "$LOCK_ROOT/1.0.0" "$LOCK_ROOT/.publish-1.0.0.lock"
LOCK_STAGE=$(create_managed_staging_directory "$LOCK_ROOT" 1.0.0)
mkdir "$LOCK_STAGE/payload"
printf 'old\n' > "$LOCK_ROOT/1.0.0/generation"
printf 'new\n' > "$LOCK_STAGE/payload/generation"
if publish_staged_directory \
        "$LOCK_ROOT" 1.0.0 "$LOCK_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "publication ignored an existing transaction lock"
fi
[ -d "$LOCK_ROOT/.publish-1.0.0.lock" ] ||
    fail "failed publication removed another publisher's lock"
grep -qx old "$LOCK_ROOT/1.0.0/generation" ||
    fail "lock refusal changed the active generation"

OUTSIDE_PAYLOAD="$TEST_ROOT/outside-payload"
mkdir "$OUTSIDE_PAYLOAD"
if publish_staged_directory \
        "$PUBLISH_ROOT" 2.0.0 "$OUTSIDE_PAYLOAD" >/dev/null 2>&1; then
    fail "publication accepted a payload outside its managed root"
fi

# A dotted final child may look like installer staging, but it is still the
# active installation ownership boundary. Publishing a descendant of it would
# move the payload away with the old generation before the commit rename.
DOTTED_CHILD_ROOT="$TEST_ROOT/dotted-child"
mkdir -p "$DOTTED_CHILD_ROOT/.checkout/payload"
printf 'retain\n' > "$DOTTED_CHILD_ROOT/.checkout/payload/generation"
if publish_staged_child_directory \
        "$DOTTED_CHILD_ROOT" .checkout \
        "$DOTTED_CHILD_ROOT/.checkout/payload" >/dev/null 2>&1; then
    fail "publisher accepted the final dotted child as its staging root"
fi
grep -qx retain "$DOTTED_CHILD_ROOT/.checkout/payload/generation" ||
    fail "dotted-child staging refusal changed the existing tree"

# An intermediate staging symlink is not an owned cleanup boundary even when
# it resolves to another directory below the managed parent.
SYMLINK_STAGE_ROOT="$TEST_ROOT/symlink-stage"
mkdir -p "$SYMLINK_STAGE_ROOT/actual-stage/payload"
printf 'retain\n' > "$SYMLINK_STAGE_ROOT/actual-stage/payload/generation"
ln -s actual-stage "$SYMLINK_STAGE_ROOT/.stage"
if publish_staged_directory \
        "$SYMLINK_STAGE_ROOT" 1.0.0 \
        "$SYMLINK_STAGE_ROOT/.stage/payload" >/dev/null 2>&1; then
    fail "publisher accepted a symlinked staging root"
fi
grep -qx retain "$SYMLINK_STAGE_ROOT/actual-stage/payload/generation" ||
    fail "symlinked staging refusal changed its target"

# Pre-publication staging is reclaimable only while the caller owns the
# per-child kernel guard. A concurrent producer cannot acquire that guard.
ORPHAN_STAGE_ROOT="$TEST_ROOT/orphan-stage"
mkdir "$ORPHAN_STAGE_ROOT"
ORPHAN_STAGE=$(create_managed_staging_directory "$ORPHAN_STAGE_ROOT" 1.0.0)
printf 'abandoned\n' > "$ORPHAN_STAGE/generation"
acquire_managed_installation_guard "$ORPHAN_STAGE_ROOT" 1.0.0
if (
    DOTFILES_INSTALL_GUARD_ROOT="$TEST_ROOT/split-guard-domain" \
        acquire_managed_installation_guard "$ORPHAN_STAGE_ROOT" 1.0.0
) >/dev/null 2>&1; then
    fail "ambient state root split the canonical child guard"
fi
recover_managed_installation "$ORPHAN_STAGE_ROOT" 1.0.0
[ ! -e "$ORPHAN_STAGE" ] ||
    fail "guarded recovery retained pre-journal staging"
release_managed_installation_guard 9

# Lookalikes in the reserved namespace fail closed instead of becoming broad
# prefix-based deletion authority.
INVALID_STAGE_ROOT="$TEST_ROOT/invalid-stage"
mkdir -p "$INVALID_STAGE_ROOT/.dotfiles-stage-1.0.0.BADBAD"
printf 'retain\n' \
    > "$INVALID_STAGE_ROOT/.dotfiles-stage-1.0.0.BADBAD/sentinel"
acquire_managed_installation_guard "$INVALID_STAGE_ROOT" 1.0.0
if recover_managed_installation \
        "$INVALID_STAGE_ROOT" 1.0.0 >/dev/null 2>&1; then
    fail "orphan recovery accepted a malformed staging identity"
fi
grep -qx retain \
    "$INVALID_STAGE_ROOT/.dotfiles-stage-1.0.0.BADBAD/sentinel" ||
    fail "orphan recovery changed malformed staging"
release_managed_installation_guard 9

# A journal is data, not deletion authority. Even a schema-complete journal
# may clean only the exact child-bound staging namespace allocated above.
FORGED_JOURNAL_ROOT="$TEST_ROOT/forged-journal"
mkdir -p \
    "$FORGED_JOURNAL_ROOT/1.0.0" \
    "$FORGED_JOURNAL_ROOT/.private-state/payload"
printf 'old\n' > "$FORGED_JOURNAL_ROOT/1.0.0/generation"
printf 'retain\n' > "$FORGED_JOURNAL_ROOT/.private-state/payload/sentinel"
python3 - "$FORGED_JOURNAL_ROOT" << 'PY'
import json
import sys
from pathlib import Path

parent = Path(sys.argv[1]).absolute()
value = {
    "child": "1.0.0",
    "format": 1,
    "had_previous": True,
    "install": str(parent / "1.0.0"),
    "parent": str(parent),
    "payload": str(parent / ".private-state" / "payload"),
    "replacement": str(parent / (".replace-1.0.0." + "a" * 32)),
    "staging_root": str(parent / ".private-state"),
}
with (parent / ".publish-1.0.0.lock").open("w", encoding="utf-8") as output:
    json.dump(value, output)
    output.write("\n")
PY
if recover_staged_directory_publication \
        "$FORGED_JOURNAL_ROOT" 1.0.0 >/dev/null 2>&1; then
    fail "recovery trusted an unrelated hidden directory from its journal"
fi
grep -qx retain "$FORGED_JOURNAL_ROOT/.private-state/payload/sentinel" ||
    fail "forged journal removed unrelated hidden state"
[ -f "$FORGED_JOURNAL_ROOT/.publish-1.0.0.lock" ] ||
    fail "forged journal refusal removed ambiguous evidence"

# A replacement path from a journal must be one generated transaction UUID,
# not merely an arbitrary hidden tree sharing the replacement prefix.
FORGED_REPLACEMENT_ROOT="$TEST_ROOT/forged-replacement"
mkdir -p \
    "$FORGED_REPLACEMENT_ROOT/1.0.0" \
    "$FORGED_REPLACEMENT_ROOT/.replace-1.0.0.private-state"
FORGED_REPLACEMENT_STAGE=$(
    create_managed_staging_directory "$FORGED_REPLACEMENT_ROOT" 1.0.0
)
mkdir "$FORGED_REPLACEMENT_STAGE/payload"
printf 'old\n' > "$FORGED_REPLACEMENT_ROOT/1.0.0/generation"
printf 'new\n' > "$FORGED_REPLACEMENT_STAGE/payload/generation"
printf 'retain\n' \
    > "$FORGED_REPLACEMENT_ROOT/.replace-1.0.0.private-state/sentinel"
python3 - "$FORGED_REPLACEMENT_ROOT" "$FORGED_REPLACEMENT_STAGE" << 'PY'
import json
import sys
from pathlib import Path

parent = Path(sys.argv[1]).absolute()
staging_root = Path(sys.argv[2]).absolute()
value = {
    "child": "1.0.0",
    "format": 1,
    "had_previous": True,
    "install": str(parent / "1.0.0"),
    "parent": str(parent),
    "payload": str(staging_root / "payload"),
    "replacement": str(parent / ".replace-1.0.0.private-state"),
    "staging_root": str(staging_root),
}
with (parent / ".publish-1.0.0.lock").open("w", encoding="utf-8") as output:
    json.dump(value, output)
    output.write("\n")
PY
if recover_staged_directory_publication \
        "$FORGED_REPLACEMENT_ROOT" 1.0.0 >/dev/null 2>&1; then
    fail "recovery trusted a replacement prefix lookalike"
fi
grep -qx retain \
    "$FORGED_REPLACEMENT_ROOT/.replace-1.0.0.private-state/sentinel" ||
    fail "forged replacement journal removed unrelated hidden state"
[ -f "$FORGED_REPLACEMENT_ROOT/.publish-1.0.0.lock" ] ||
    fail "forged replacement refusal removed ambiguous evidence"

# Lexical containment is insufficient: parent traversal or an intermediate
# symlink inside an exact staging root must not select an unrelated sibling.
PHYSICAL_STAGE_ROOT="$TEST_ROOT/physical-stage"
mkdir -p "$PHYSICAL_STAGE_ROOT/private/payload"
printf 'retain\n' > "$PHYSICAL_STAGE_ROOT/private/payload/generation"
TRAVERSAL_STAGE=$(
    create_managed_staging_directory "$PHYSICAL_STAGE_ROOT" 1.0.0
)
if publish_staged_directory \
        "$PHYSICAL_STAGE_ROOT" 1.0.0 \
        "$TRAVERSAL_STAGE/../private/payload" >/dev/null 2>&1; then
    fail "publisher accepted parent traversal out of staging"
fi
grep -qx retain "$PHYSICAL_STAGE_ROOT/private/payload/generation" ||
    fail "parent-traversing payload changed its unrelated sibling"
SYMLINK_PAYLOAD_STAGE=$(
    create_managed_staging_directory "$PHYSICAL_STAGE_ROOT" 1.0.0
)
ln -s ../private "$SYMLINK_PAYLOAD_STAGE/link"
if publish_staged_directory \
        "$PHYSICAL_STAGE_ROOT" 1.0.0 \
        "$SYMLINK_PAYLOAD_STAGE/link/payload" >/dev/null 2>&1; then
    fail "publisher accepted an intermediate symlink out of staging"
fi
grep -qx retain "$PHYSICAL_STAGE_ROOT/private/payload/generation" ||
    fail "symlinked payload changed its unrelated sibling"

# Once the journal is durable, every later graceful failure belongs to the
# recovery boundary—even before the replacement directory exists.
EARLY_FAILURE_ROOT="$TEST_ROOT/early-failure"
mkdir -p "$EARLY_FAILURE_ROOT"
EARLY_FAILURE_STAGE=$(
    create_managed_staging_directory "$EARLY_FAILURE_ROOT" 1.0.0
)
mkdir "$EARLY_FAILURE_STAGE/payload"
printf 'new\n' > "$EARLY_FAILURE_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=after-journal \
        publish_staged_directory \
        "$EARLY_FAILURE_ROOT" 1.0.0 \
        "$EARLY_FAILURE_STAGE/payload" >/dev/null 2>&1; then
    fail "post-journal injected failure returned success"
fi
[ ! -e "$EARLY_FAILURE_ROOT/1.0.0" ] ||
    fail "post-journal failure committed a first installation"
[ -f "$EARLY_FAILURE_STAGE/payload/generation" ] ||
    fail "post-journal failure destroyed the uncommitted payload"
[ ! -e "$EARLY_FAILURE_ROOT/.publish-1.0.0.lock" ] ||
    fail "post-journal failure retained its recovered journal"

# If only the publisher worker dies, the caller shell gets one recovery turn
# before its own staging cleanup. That closes the first-install ambiguity
# which would otherwise arise when the shell removes the journal's payload.
SURVIVING_CALLER_ROOT="$TEST_ROOT/surviving-caller"
mkdir -p "$SURVIVING_CALLER_ROOT/1.0.0"
SURVIVING_CALLER_STAGE=$(
    create_managed_staging_directory "$SURVIVING_CALLER_ROOT" 1.0.0
)
mkdir "$SURVIVING_CALLER_STAGE/payload"
printf 'old\n' > "$SURVIVING_CALLER_ROOT/1.0.0/generation"
printf 'new\n' > "$SURVIVING_CALLER_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-journal \
        publish_staged_directory \
        "$SURVIVING_CALLER_ROOT" 1.0.0 \
        "$SURVIVING_CALLER_STAGE/payload" >/dev/null 2>&1; then
    fail "publisher-worker hard-crash fixture returned success"
fi
grep -qx old "$SURVIVING_CALLER_ROOT/1.0.0/generation" ||
    fail "publisher-worker recovery changed the active generation"
if find "$SURVIVING_CALLER_ROOT" -maxdepth 1 \
        \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
            -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "surviving caller retained publisher-worker transaction state"
fi

# Hard death immediately after journaling is replayable with the old
# generation still in place.
EARLY_JOURNAL_ROOT="$TEST_ROOT/early-journal"
mkdir -p "$EARLY_JOURNAL_ROOT/1.0.0"
EARLY_JOURNAL_STAGE=$(
    create_managed_staging_directory "$EARLY_JOURNAL_ROOT" 1.0.0
)
mkdir "$EARLY_JOURNAL_STAGE/payload"
printf 'old\n' > "$EARLY_JOURNAL_ROOT/1.0.0/generation"
printf 'new\n' > "$EARLY_JOURNAL_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-journal \
        python3 "$DOTFILES/lib/managed-directory-publication.py" \
        --parent "$EARLY_JOURNAL_ROOT" \
        --child 1.0.0 \
        --payload "$EARLY_JOURNAL_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "post-journal hard-crash fixture returned success"
fi
grep -qx old "$EARLY_JOURNAL_ROOT/1.0.0/generation" ||
    fail "post-journal hard crash changed the active generation"
[ -f "$EARLY_JOURNAL_ROOT/.publish-1.0.0.lock" ] ||
    fail "post-journal hard crash did not retain its journal"
recover_staged_directory_publication "$EARLY_JOURNAL_ROOT" 1.0.0
grep -qx old "$EARLY_JOURNAL_ROOT/1.0.0/generation" ||
    fail "post-journal replay changed the active generation"
[ ! -e "$EARLY_JOURNAL_STAGE" ] ||
    fail "post-journal replay retained abandoned staging"
[ ! -e "$EARLY_JOURNAL_ROOT/.publish-1.0.0.lock" ] ||
    fail "post-journal replay retained its journal"

# Recovery commits its classification by removing replacement+journal before
# deleting abandoned staging. Death in that cleanup gap leaves an ordinary
# guarded orphan, never an ambiguous publication state.
RECOVERY_GAP_ROOT="$TEST_ROOT/recovery-gap"
mkdir -p "$RECOVERY_GAP_ROOT/1.0.0"
RECOVERY_GAP_STAGE=$(
    create_managed_staging_directory "$RECOVERY_GAP_ROOT" 1.0.0
)
mkdir "$RECOVERY_GAP_STAGE/payload"
printf 'old\n' > "$RECOVERY_GAP_ROOT/1.0.0/generation"
printf 'new\n' > "$RECOVERY_GAP_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-journal \
        python3 "$DOTFILES/lib/managed-directory-publication.py" \
        --parent "$RECOVERY_GAP_ROOT" \
        --child 1.0.0 \
        --payload "$RECOVERY_GAP_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "recovery-gap journal fixture returned success"
fi
acquire_managed_installation_guard "$RECOVERY_GAP_ROOT" 1.0.0
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-recovery-transaction-cleanup \
        recover_managed_installation \
        "$RECOVERY_GAP_ROOT" 1.0.0 >/dev/null 2>&1; then
    fail "recovery cleanup-gap hard-crash fixture returned success"
fi
grep -qx old "$RECOVERY_GAP_ROOT/1.0.0/generation" ||
    fail "recovery cleanup-gap changed the active generation"
[ ! -e "$RECOVERY_GAP_ROOT/.publish-1.0.0.lock" ] ||
    fail "recovery cleanup-gap retained classified journal state"
[ -f "$RECOVERY_GAP_STAGE/payload/generation" ] ||
    fail "recovery cleanup-gap removed staging before its journal"
recover_managed_installation "$RECOVERY_GAP_ROOT" 1.0.0
[ ! -e "$RECOVERY_GAP_STAGE" ] ||
    fail "repeated recovery retained the classified orphan stage"
grep -qx old "$RECOVERY_GAP_ROOT/1.0.0/generation" ||
    fail "repeated recovery misclassified the prior generation as committed"
release_managed_installation_guard 9

# A first publication can die after creating its empty replacement root but
# before either rename. Replay rejects the uncommitted generation and clears
# both transaction-owned trees.
EARLY_REPLACEMENT_ROOT="$TEST_ROOT/early-replacement"
mkdir -p "$EARLY_REPLACEMENT_ROOT"
EARLY_REPLACEMENT_STAGE=$(
    create_managed_staging_directory "$EARLY_REPLACEMENT_ROOT" 1.0.0
)
mkdir "$EARLY_REPLACEMENT_STAGE/payload"
printf 'new\n' > "$EARLY_REPLACEMENT_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-replacement-create \
        python3 "$DOTFILES/lib/managed-directory-publication.py" \
        --parent "$EARLY_REPLACEMENT_ROOT" \
        --child 1.0.0 \
        --payload "$EARLY_REPLACEMENT_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "post-replacement-create hard-crash fixture returned success"
fi
[ ! -e "$EARLY_REPLACEMENT_ROOT/1.0.0" ] ||
    fail "post-replacement-create crash committed a first installation"
[ -f "$EARLY_REPLACEMENT_STAGE/payload/generation" ] ||
    fail "post-replacement-create crash lost the staged payload"
[ -f "$EARLY_REPLACEMENT_ROOT/.publish-1.0.0.lock" ] ||
    fail "post-replacement-create crash did not retain its journal"
if ! find "$EARLY_REPLACEMENT_ROOT" -maxdepth 1 -name '.replace-*' \
        -type d -print -quit | grep -q .; then
    fail "post-replacement-create crash did not retain its replacement root"
fi
recover_staged_directory_publication "$EARLY_REPLACEMENT_ROOT" 1.0.0
[ ! -e "$EARLY_REPLACEMENT_ROOT/1.0.0" ] ||
    fail "post-replacement-create replay committed an unrenamed payload"
if find "$EARLY_REPLACEMENT_ROOT" -maxdepth 1 \
        \( -name '.dotfiles-stage-*' -o -name '.replace-*' \
            -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "post-replacement-create replay retained transaction state"
fi

ROLLBACK_ROOT="$TEST_ROOT/rollback"
mkdir -p "$ROLLBACK_ROOT/1.0.0"
ROLLBACK_STAGE=$(create_managed_staging_directory "$ROLLBACK_ROOT" 1.0.0)
mkdir "$ROLLBACK_STAGE/payload"
printf 'old\n' > "$ROLLBACK_ROOT/1.0.0/generation"
printf 'new\n' > "$ROLLBACK_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=after-previous-rename \
        publish_staged_directory \
        "$ROLLBACK_ROOT" 1.0.0 "$ROLLBACK_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "injected publication failure was accepted"
fi
grep -qx old "$ROLLBACK_ROOT/1.0.0/generation" ||
    fail "failed publication did not restore the prior generation"
[ -f "$ROLLBACK_STAGE/payload/generation" ] ||
    fail "failed publication destroyed the uncommitted payload"

COMMIT_FAULT_ROOT="$TEST_ROOT/commit-fault"
mkdir -p "$COMMIT_FAULT_ROOT/1.0.0"
COMMIT_FAULT_STAGE=$(
    create_managed_staging_directory "$COMMIT_FAULT_ROOT" 1.0.0
)
mkdir "$COMMIT_FAULT_STAGE/payload"
printf 'old\n' > "$COMMIT_FAULT_ROOT/1.0.0/generation"
printf 'new\n' > "$COMMIT_FAULT_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=after-payload-rename \
        publish_staged_directory \
        "$COMMIT_FAULT_ROOT" 1.0.0 \
        "$COMMIT_FAULT_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "post-commit injected failure returned success"
fi
grep -qx new "$COMMIT_FAULT_ROOT/1.0.0/generation" ||
    fail "post-commit failure did not retain the committed generation"
if find "$COMMIT_FAULT_ROOT" -maxdepth 1 \
        \( -name '.replace-*' -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "post-commit failure retained transaction state"
fi

HARD_ROLLBACK_ROOT="$TEST_ROOT/hard-rollback"
mkdir -p "$HARD_ROLLBACK_ROOT/1.0.0"
HARD_ROLLBACK_STAGE=$(
    create_managed_staging_directory "$HARD_ROLLBACK_ROOT" 1.0.0
)
mkdir "$HARD_ROLLBACK_STAGE/payload"
printf 'old\n' > "$HARD_ROLLBACK_ROOT/1.0.0/generation"
printf 'new\n' > "$HARD_ROLLBACK_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-previous-rename \
        python3 "$DOTFILES/lib/managed-directory-publication.py" \
        --parent "$HARD_ROLLBACK_ROOT" \
        --child 1.0.0 \
        --payload "$HARD_ROLLBACK_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "hard-crash rollback fixture returned success"
fi
[ ! -e "$HARD_ROLLBACK_ROOT/1.0.0" ] ||
    fail "hard-crash rollback fixture did not displace the prior generation"
[ -f "$HARD_ROLLBACK_STAGE/payload/generation" ] ||
    fail "hard-crash rollback fixture lost the staged generation"
[ -f "$HARD_ROLLBACK_ROOT/.publish-1.0.0.lock" ] ||
    fail "hard-crash rollback fixture did not retain its durable journal"
recover_staged_directory_publication "$HARD_ROLLBACK_ROOT" 1.0.0
grep -qx old "$HARD_ROLLBACK_ROOT/1.0.0/generation" ||
    fail "hard-crash replay did not restore the prior generation"
[ ! -e "$HARD_ROLLBACK_STAGE" ] ||
    fail "hard-crash replay retained abandoned staging"
if find "$HARD_ROLLBACK_ROOT" -maxdepth 1 \
        \( -name '.replace-*' -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "hard-crash rollback replay retained transaction state"
fi

HARD_COMMIT_ROOT="$TEST_ROOT/hard-commit"
mkdir -p "$HARD_COMMIT_ROOT/1.0.0"
HARD_COMMIT_STAGE=$(
    create_managed_staging_directory "$HARD_COMMIT_ROOT" 1.0.0
)
mkdir "$HARD_COMMIT_STAGE/payload"
printf 'old\n' > "$HARD_COMMIT_ROOT/1.0.0/generation"
printf 'new\n' > "$HARD_COMMIT_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-payload-rename \
        python3 "$DOTFILES/lib/managed-directory-publication.py" \
        --parent "$HARD_COMMIT_ROOT" \
        --child 1.0.0 \
        --payload "$HARD_COMMIT_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "hard-crash commit fixture returned success"
fi
grep -qx new "$HARD_COMMIT_ROOT/1.0.0/generation" ||
    fail "hard-crash commit fixture did not publish the new generation"
[ -f "$HARD_COMMIT_ROOT/.publish-1.0.0.lock" ] ||
    fail "hard-crash commit fixture did not retain its durable journal"
recover_staged_directory_publication "$HARD_COMMIT_ROOT" 1.0.0
grep -qx new "$HARD_COMMIT_ROOT/1.0.0/generation" ||
    fail "hard-crash commit replay rolled back the committed generation"
[ ! -e "$HARD_COMMIT_STAGE" ] ||
    fail "hard-crash commit replay retained abandoned staging"
if find "$HARD_COMMIT_ROOT" -maxdepth 1 \
        \( -name '.replace-*' -o -name '.publish-*.lock' \) \
        -print -quit | grep -q .; then
    fail "hard-crash commit replay retained transaction state"
fi

# Cleanup completion is also replayable: the new generation remains committed
# when process death lands after the prior tree is gone but before journal
# unlink.
POST_CLEANUP_ROOT="$TEST_ROOT/post-cleanup"
mkdir -p "$POST_CLEANUP_ROOT/1.0.0"
POST_CLEANUP_STAGE=$(
    create_managed_staging_directory "$POST_CLEANUP_ROOT" 1.0.0
)
mkdir "$POST_CLEANUP_STAGE/payload"
printf 'old\n' > "$POST_CLEANUP_ROOT/1.0.0/generation"
printf 'new\n' > "$POST_CLEANUP_STAGE/payload/generation"
if DOTFILES_PUBLISHER_TEST_FAULT=hard-crash-after-replacement-cleanup \
        python3 "$DOTFILES/lib/managed-directory-publication.py" \
        --parent "$POST_CLEANUP_ROOT" \
        --child 1.0.0 \
        --payload "$POST_CLEANUP_STAGE/payload" \
        >/dev/null 2>&1; then
    fail "post-cleanup hard-crash fixture returned success"
fi
grep -qx new "$POST_CLEANUP_ROOT/1.0.0/generation" ||
    fail "post-cleanup crash lost the committed generation"
[ -f "$POST_CLEANUP_ROOT/.publish-1.0.0.lock" ] ||
    fail "post-cleanup crash did not retain its durable journal"
if find "$POST_CLEANUP_ROOT" -maxdepth 1 -name '.replace-*' \
        -print -quit | grep -q .; then
    fail "post-cleanup crash retained an already-cleaned replacement root"
fi
recover_staged_directory_publication "$POST_CLEANUP_ROOT" 1.0.0
grep -qx new "$POST_CLEANUP_ROOT/1.0.0/generation" ||
    fail "post-cleanup replay rolled back the committed generation"
[ ! -e "$POST_CLEANUP_STAGE" ] ||
    fail "post-cleanup replay retained abandoned staging"
[ ! -e "$POST_CLEANUP_ROOT/.publish-1.0.0.lock" ] ||
    fail "post-cleanup replay retained its journal"

# Existing mount points are not renameable managed children. /dev/shm is a
# portable Linux witness when present; the check is read-only.
if [ -d /dev/shm ] && mountpoint -q /dev/shm 2>/dev/null; then
    if python3 "$DOTFILES/lib/managed-directory-publication.py" \
            --parent /dev --child shm --recover-only \
            >/dev/null 2>&1; then
        fail "publisher accepted a mounted existing child"
    fi
fi

DISPATCH_OUTPUT="$TEST_ROOT/dispatch-output"
if bash "$DOTFILES/tools/install.sh" cuda/../llvm --help \
        >"$DISPATCH_OUTPUT" 2>&1; then
    fail "dispatcher accepted a path-equivalent unsupported tool name"
fi
grep -q 'Unknown tool: cuda/../llvm' "$DISPATCH_OUTPUT" ||
    fail "dispatcher traversal failed for the wrong reason"
bash "$DOTFILES/tools/install.sh" --help > "$DISPATCH_OUTPUT"
grep -Eq 'mold[[:space:]]+\(explicit only\)' "$DISPATCH_OUTPUT" ||
    fail "dispatcher did not expose mold as explicit-only"

expect_version_rejected() {
    local installer="$1"
    shift
    if TOOLS_DIR="$TEST_ROOT/rejected-tools" \
            bash "$DOTFILES/tools/$installer/install.sh" "$@" \
            >/dev/null 2>&1; then
        fail "$installer accepted unsafe or ambiguous arguments: $*"
    fi
}
expect_version_rejected bazel latest
expect_version_rejected beads ../escape
expect_version_rejected beads 0.2.19 extra
expect_version_rejected hf ../escape
expect_version_rejected hf 1.24.0 extra
expect_version_rejected mold ../escape
expect_version_rejected rocm ../escape
expect_version_rejected rocm 10.1.0a20260819 gfx1150 extra
expect_version_rejected rocm 10.1.0a20260819 gfx110X-all
if ROCM_GPU_TARGET='' TOOLS_DIR="$TEST_ROOT/rejected-tools" \
        bash "$DOTFILES/tools/rocm/install.sh" 10.1.0a20260819 \
        >/dev/null 2>&1; then
    fail "ROCm silently selected a default GPU target"
fi

BAZEL_ROOT_SYMLINK_TOOLS="$TEST_ROOT/bazel-root-symlink/tools"
mkdir -p "$BAZEL_ROOT_SYMLINK_TOOLS" "$TEST_ROOT/bazel-root-external"
ln -s "$TEST_ROOT/bazel-root-external" \
    "$BAZEL_ROOT_SYMLINK_TOOLS/bazel"
if TOOLS_DIR="$BAZEL_ROOT_SYMLINK_TOOLS" \
        bash "$DOTFILES/tools/bazel/install.sh" 8.2.1 \
        >"$DISPATCH_OUTPUT" 2>&1; then
    fail "Bazel accepted a symlinked managed root"
fi
grep -q 'Managed Bazel root is not an ordinary directory' "$DISPATCH_OUTPUT" ||
    fail "symlinked Bazel root failed after network access"

expect_managed_root_symlink_rejected() {
    local tool="$1"
    shift
    local tools_root="$TEST_ROOT/$tool-root-symlink/tools"
    local external_root="$TEST_ROOT/$tool-root-symlink/external"

    mkdir -p "$tools_root" "$external_root"
    printf 'retain\n' > "$external_root/sentinel"
    ln -s "$external_root" "$tools_root/$tool"
    if TOOLS_DIR="$tools_root" \
            bash "$DOTFILES/tools/$tool/install.sh" "$@" \
            >"$DISPATCH_OUTPUT" 2>&1; then
        fail "$tool accepted a symlinked managed root"
    fi
    grep -qx retain "$external_root/sentinel" ||
        fail "$tool modified a symlinked managed root"
    if find "$external_root" -mindepth 1 ! -name sentinel \
            -print -quit | grep -q .; then
        fail "$tool created state through a symlinked managed root"
    fi
}
expect_managed_root_symlink_rejected beads 0.2.19
expect_managed_root_symlink_rejected hf 1.24.0
if [ "$(uname -s)" = "Linux" ]; then
    expect_managed_root_symlink_rejected mold 2.40.4
    expect_managed_root_symlink_rejected rocm 10.1.0a20260819 gfx1150
fi

BAZEL_SYMLINK_TOOLS="$TEST_ROOT/bazel-symlink/tools"
mkdir -p "$BAZEL_SYMLINK_TOOLS/bazel/8.2.1" "$TEST_ROOT/bazel-external-bin"
ln -s "$TEST_ROOT/bazel-external-bin" \
    "$BAZEL_SYMLINK_TOOLS/bazel/8.2.1/bin"
if TOOLS_DIR="$BAZEL_SYMLINK_TOOLS" \
        bash "$DOTFILES/tools/bazel/install.sh" 8.2.1 \
        >"$DISPATCH_OUTPUT" 2>&1; then
    fail "Bazel accepted a symlinked managed binary directory"
fi
grep -q 'Refusing non-directory Bazel binary directory' "$DISPATCH_OUTPUT" ||
    fail "symlinked Bazel binary directory failed after network access"

BAZEL_FIXTURE_ROOT="$TEST_ROOT/bazel-fixture"
BAZEL_ASSET_DIRECTORY="$BAZEL_FIXTURE_ROOT/assets"
BAZEL_FAKE_BIN="$BAZEL_FIXTURE_ROOT/bin"
BAZEL_TOOLS="$BAZEL_FIXTURE_ROOT/tools"
mkdir -p "$BAZEL_ASSET_DIRECTORY" "$BAZEL_FAKE_BIN"
case "$(uname -s)_$(uname -m)" in
    Linux_x86_64|Linux_amd64)
        BAZEL_SUFFIX="linux-amd64"
        IBAZEL_SUFFIX="linux_amd64"
        IBAZEL_SHA256=761cb60545f3de5bc0615d2b0f58accd4186161ac6cdd2a168ad6ee59731b92e
        ;;
    Linux_aarch64|Linux_arm64)
        BAZEL_SUFFIX="linux-arm64"
        IBAZEL_SUFFIX="linux_arm64"
        IBAZEL_SHA256=3f2c3c0b629a426cb5452fdf54b88c92b554344689e67c592046dbbc017fa562
        ;;
    Darwin_x86_64|Darwin_amd64)
        BAZEL_SUFFIX="darwin-amd64"
        IBAZEL_SUFFIX="darwin_amd64"
        IBAZEL_SHA256=781e6113fc8f3d41299a001fe2e4780c1f6cc3d236ace8af69a87558ade07df4
        ;;
    Darwin_aarch64|Darwin_arm64)
        BAZEL_SUFFIX="darwin-arm64"
        IBAZEL_SUFFIX="darwin_arm64"
        IBAZEL_SHA256=1cfec3c53213520ddba3d8ff6dbc85ac0ec0c07e9703dc44695e1af166b009ab
        ;;
    *) fail "Bazel fixture does not recognize the host platform" ;;
esac
BAZELISK_ASSET="bazelisk-$BAZEL_SUFFIX"
BUILDIFIER_ASSET="buildifier-$BAZEL_SUFFIX"
BUILDOZER_ASSET="buildozer-$BAZEL_SUFFIX"
IBAZEL_ASSET="ibazel_$IBAZEL_SUFFIX"
for binary in bazel buildifier buildozer ibazel; do
    printf '#!/bin/sh\nprintf "%s fixture\\n"\n' "$binary" \
        > "$BAZEL_ASSET_DIRECTORY/$binary"
    chmod 755 "$BAZEL_ASSET_DIRECTORY/$binary"
done
REAL_SHA256SUM=$(command -v sha256sum)
BAZEL_SHA256=$("$REAL_SHA256SUM" "$BAZEL_ASSET_DIRECTORY/bazel")
BAZEL_SHA256="${BAZEL_SHA256%% *}"
BUILDIFIER_SHA256=$("$REAL_SHA256SUM" "$BAZEL_ASSET_DIRECTORY/buildifier")
BUILDIFIER_SHA256="${BUILDIFIER_SHA256%% *}"
BUILDOZER_SHA256=$("$REAL_SHA256SUM" "$BAZEL_ASSET_DIRECTORY/buildozer")
BUILDOZER_SHA256="${BUILDOZER_SHA256%% *}"
export \
    BAZEL_ASSET_DIRECTORY \
    BAZELISK_ASSET \
    BAZEL_SHA256 \
    BUILDIFIER_ASSET \
    BUILDIFIER_SHA256 \
    BUILDOZER_ASSET \
    BUILDOZER_SHA256 \
    IBAZEL_ASSET \
    IBAZEL_SHA256 \
    REAL_SHA256SUM
cat > "$BAZEL_FAKE_BIN/curl" << 'EOF'
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
    */bazelisk/releases/tags/v1.29.0)
        printf '{"assets":[{"name":"%s","digest":"sha256:%s"}]}\n' \
            "$BAZELISK_ASSET" "$BAZEL_SHA256"
        ;;
    */buildtools/releases/tags/v8.2.1)
        printf '%s\n' \
            "{\"assets\":[
{\"name\":\"$BUILDIFIER_ASSET\",\"digest\":\"sha256:$BUILDIFIER_SHA256\"},
{\"name\":\"$BUILDOZER_ASSET\",\"digest\":\"sha256:$BUILDOZER_SHA256\"}
]}"
        ;;
    */"$BAZELISK_ASSET")
        cp "$BAZEL_ASSET_DIRECTORY/bazel" "$output"
        ;;
    */"$BUILDIFIER_ASSET")
        cp "$BAZEL_ASSET_DIRECTORY/buildifier" "$output"
        ;;
    */"$BUILDOZER_ASSET")
        cp "$BAZEL_ASSET_DIRECTORY/buildozer" "$output"
        ;;
    */"$IBAZEL_ASSET")
        cp "$BAZEL_ASSET_DIRECTORY/ibazel" "$output"
        ;;
    *)
        echo "unexpected Bazel fixture URL: $url" >&2
        exit 72
        ;;
esac
EOF
cat > "$BAZEL_FAKE_BIN/sha256sum" << 'EOF'
#!/bin/bash
set -e
if [ "${1##*/}" = "ibazel" ] &&
        cmp -s "$1" "$BAZEL_ASSET_DIRECTORY/ibazel"; then
    printf '%s  %s\n' "$IBAZEL_SHA256" "$1"
    exit 0
fi
exec "$REAL_SHA256SUM" "$@"
EOF
chmod 755 "$BAZEL_FAKE_BIN/curl" "$BAZEL_FAKE_BIN/sha256sum"

# An inherited three-tool directory with no ibazel identity is repaired as one
# complete transaction. No inherited binary is reused without attestation.
mkdir -p "$BAZEL_TOOLS/bazel/8.2.1/bin"
for binary in bazel buildifier buildozer; do
    printf 'inherited\n' > "$BAZEL_TOOLS/bazel/8.2.1/bin/$binary"
    chmod 755 "$BAZEL_TOOLS/bazel/8.2.1/bin/$binary"
done
PATH="$BAZEL_FAKE_BIN:$PATH" TOOLS_DIR="$BAZEL_TOOLS" \
    bash "$DOTFILES/tools/bazel/install.sh" 8.2.1 \
    >"$DISPATCH_OUTPUT" 2>&1 ||
    fail "Bazel did not repair a bundle missing ibazel"
grep -q 'ibazel fixture' "$BAZEL_TOOLS/bazel/8.2.1/bin/ibazel" ||
    fail "Bazel repair did not publish the attested ibazel"
grep -qx 'format=1' \
    "$BAZEL_TOOLS/bazel/8.2.1/.dotfiles-install-identity" ||
    fail "Bazel repair did not publish a bundle identity"

# A later wrong ibazel invalidates the bundle and causes another complete,
# rollback-safe repair rather than accepting executability as identity.
printf '#!/bin/sh\nprintf "wrong ibazel\\n"\n' \
    > "$BAZEL_TOOLS/bazel/8.2.1/bin/ibazel"
chmod 755 "$BAZEL_TOOLS/bazel/8.2.1/bin/ibazel"
PATH="$BAZEL_FAKE_BIN:$PATH" TOOLS_DIR="$BAZEL_TOOLS" \
    bash "$DOTFILES/tools/bazel/install.sh" 8.2.1 \
    >"$DISPATCH_OUTPUT" 2>&1 ||
    fail "Bazel did not repair a wrong ibazel"
grep -q 'ibazel fixture' "$BAZEL_TOOLS/bazel/8.2.1/bin/ibazel" ||
    fail "Bazel retained the wrong ibazel after repair"

AMBIENT_TOOLS="$TEST_ROOT/ambient-force/tools"
AMBIENT_INSTALL="$AMBIENT_TOOLS/rocm/7.14.0"
mkdir -p "$AMBIENT_INSTALL"
printf 'retain\n' > "$AMBIENT_INSTALL/sentinel"
if FORCE=true TOOLS_DIR="$AMBIENT_TOOLS" \
        bash "$DOTFILES/tools/rocm/install.sh" 7.14.0 gfx1100 \
        >/dev/null 2>&1; then
    fail "ambient FORCE replaced an unidentified ROCm install"
fi
grep -qx retain "$AMBIENT_INSTALL/sentinel" ||
    fail "ambient FORCE damaged an unidentified ROCm install"

echo "installer transaction safety passed"
