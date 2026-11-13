#!/bin/bash
# Updates /vns from https://github.com/schlagerdk/VNS.Linux
# Usage: update.sh              (clones the repository to a temporary directory)
#        update.sh <source-dir>  (uses an existing copy, used by init.sh)
# Without a source it installs the newest release. VNS_UPDATE_REF=v1.2.0 installs that release, and
# VNS_UPDATE_REF=main the newest commit on main, which is also used before the first release.
set -euo pipefail

REPO_URL="https://github.com/schlagerdk/VNS.Linux.git"
TARGET="/vns"

main() {
    local src="${1:-}"
    local tmp=""
    local revision=""

    if [ -z "$src" ]; then
        tmp="$(mktemp -d)"
        trap "rm -rf '$tmp'" EXIT
        local ref="${VNS_UPDATE_REF:-}"
        if [ -z "$ref" ]; then
            ref="$(git ls-remote --tags --refs "$REPO_URL" 'v*' | awk '{sub("refs/tags/", "", $2); print $2}' \
                | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -n 1 || true)"
        fi
        [ "$ref" != main ] || ref=""
        if [ -n "$ref" ]; then
            [[ $ref =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Invalid release: $ref" >&2; return 1; }
            echo "Install release $ref"
            git clone --depth 1 --branch "$ref" "$REPO_URL" "$tmp/VNS.Linux"
        else
            git clone --depth 1 "$REPO_URL" "$tmp/VNS.Linux"
        fi
        src="$tmp/VNS.Linux"
    fi
    if git -C "$src" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        revision="$(git -C "$src" rev-parse HEAD)"
        if [ -n "$(git -C "$src" status --porcelain --untracked-files=normal)" ]; then
            echo "WARNING: Source has local changes; installed revision will be unknown." >&2
            revision=""
        fi
    else
        echo "WARNING: Source has no Git metadata; installed revision will be unknown." >&2
    fi

    # If the repository has a vns subdirectory, deploy that directory
    [ -d "$src/vns" ] && src="$src/vns"

    sudo mkdir -p "$TARGET"
    sudo chown "$(id -un):$(id -gn)" "$TARGET"
    # A failed synchronization must not retain a success-shaped version marker.
    printf '\n' > "$TARGET/.vns-version"

    # Save the sa password from the installed copy so it can be restored after synchronization
    local sa_quoted=""
    if [ -f "$TARGET/sql/drop_db.sh" ]; then
        sa_quoted="$(sed -n 's/.* -U sa -P \(.*\) -i scripts\/drop_db.sql$/\1/p' "$TARGET/sql/drop_db.sh")"
        [ "$sa_quoted" = "'@SA_PASSWORD@'" ] && sa_quoted=""
    fi

    # Do not use --delete, so local files (e.g. SQL data) are preserved.
    rsync -a \
        --exclude '.git' \
        --exclude '.DS_Store' \
        --exclude '.gitignore' \
        --exclude '.vns-version' \
        --exclude '.vscode' \
        "$src"/ "$TARGET"/

    # Site ports are discovered from active configs now; remove the obsolete counter.
    rm -f "$TARGET/apache/_portnumber.txt"

    # Templates hold no local data, so remove templates that no longer exist in the repository
    if [ -d "$src/apache/solutionfiles" ]; then
        rsync -a --delete "$src/apache/solutionfiles"/ "$TARGET/apache/solutionfiles"/
    fi

    if [ -n "$sa_quoted" ]; then
        source "$TARGET/sql/lib/sa_password.sh"
        sa_apply_quoted "$TARGET/sql" "$sa_quoted"
    fi

    find "$TARGET" -name '*.sh' -exec chmod +x {} +
    sudo bash "$TARGET/apache/install_tls_policy.sh"
    # Not needed for the update itself: a failure is reported, and the update goes on
    sudo bash "$TARGET/apache/install_apache_conf.sh" || echo "The server-wide Apache configs were not installed." >&2
    # Harbor's copy of the sites' state, equal to the site folders (they stay the truth until phase 6)
    sudo python3 "$TARGET/bin/harbor" state sync || echo "Harbor's copy of the sites' state was not updated." >&2
    printf '%s\n' "$revision" > "$TARGET/.vns-version"
    # Harbor as a release in /opt/harbor (phase 3): /vns keeps working whether or not this does
    sudo python3 "$TARGET/bin/harbor" self install "$TARGET" || echo "Harbor was not installed in /opt/harbor." >&2
    # The bridge to Harbor (phase 6): Harbor holds every site's settings, and its own scripts and .vns-publish leave the
    # site folders (backed up first; a site's own scripts stay). A site that is not ready is left as it is, and told.
    sudo python3 "$TARGET/bin/harbor" migrate || echo "Not every site was migrated: sudo harbor migrate --dry-run tells why." >&2
    echo "VNS updated in $TARGET"
}

main "$@"
exit $?
