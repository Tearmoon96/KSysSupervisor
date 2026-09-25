#!/bin/sh
# Build everything a KSysSupervisor release publishes, into release/.
#
#   release/KSysSupervisor-<version>/                 the Python version, unpacked
#   release/KSysSupervisor-<version>.tar.gz           ...and packed, with install.sh
#   release/KSysSupervisor-<version>-x86_64.AppImage  the AppImage
#   release/SHA256SUMS                                checksums of the two downloads
#
# The three files at the top level are the release's assets, and the in-app
# updater looks for them by exactly these names - renaming one breaks updates.
#
# Only runtime files go in: the list below is an allowlist, so a new
# development file stays out unless it is added here on purpose.
#
#   ./packaging/make-release.sh               tests, then build
#   ./packaging/make-release.sh --skip-tests  build only
#   ./packaging/make-release.sh --no-appimage Python version only

set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT="$ROOT/release"

RUN_TESTS=1
APPIMAGE=1
for arg in "$@"; do
    case "$arg" in
        --skip-tests)  RUN_TESTS=0 ;;
        --no-appimage) APPIMAGE=0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\033[1;34m::\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$1" >&2; exit 1; }

# ---- version ----------------------------------------------------------------

VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/ksyssupervisor/__init__.py")
[ -n "$VERSION" ] || die "Could not read APP_VERSION."
echo "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
    || die "APP_VERSION '$VERSION' is not MAJOR.MINOR.PATCH."
grep -q "^## $VERSION\$" "$ROOT/CHANGELOG.md" \
    || die "CHANGELOG.md has no '## $VERSION' section."
info "KSysSupervisor $VERSION"

NAME="KSysSupervisor-$VERSION"
DIR="$OUT/$NAME"

# ---- tests ------------------------------------------------------------------

if [ "$RUN_TESTS" = 1 ]; then
    info "Running the test suite..."
    (cd "$ROOT" && python3 -m unittest discover -s tests >/dev/null 2>&1) \
        || die "Tests failed. Run 'python3 -m unittest discover -s tests' to see why."
fi

# ---- the Python version -----------------------------------------------------

info "Assembling $NAME/..."
rm -rf "$DIR" "$OUT/$NAME.tar.gz" "$OUT/$NAME-x86_64.AppImage" "$OUT/SHA256SUMS"
mkdir -p "$DIR/helpers" "$DIR/packaging" "$DIR/Icons"

for file in KSysSupervisor.py requirements.txt install.sh uninstall.sh \
            README.md LICENSE CHANGELOG.md; do
    cp "$ROOT/$file" "$DIR/" || die "Missing $file."
done
cp -r "$ROOT/ksyssupervisor" "$DIR/"
cp "$ROOT/helpers/ksyssupervisor-fanhelper" "$DIR/helpers/"
cp "$ROOT/packaging/org.ksyssupervisor.fancontrol.policy" "$DIR/packaging/"
cp "$ROOT/Icons/iconblue.svg" "$DIR/Icons/"

find "$DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$DIR" -name '*.py[co]' -type f -delete
chmod 755 "$DIR/KSysSupervisor.py" "$DIR/install.sh" "$DIR/uninstall.sh" \
          "$DIR/helpers/ksyssupervisor-fanhelper"

# Words that must not appear in anything published, one extended regex per
# line, kept in an optional local file that is itself never published. Checked
# over the release and over every file git would publish.
FORBIDDEN="$ROOT/.release-forbidden"
if [ -f "$FORBIDDEN" ]; then
    FOUND=$( { grep -rilE -f "$FORBIDDEN" "$DIR";
               cd "$ROOT" && git ls-files -co --exclude-standard -z 2>/dev/null \
                   | xargs -0 -r grep -ilE -f "$FORBIDDEN" --; } 2>/dev/null || true)
    if [ -n "$FOUND" ]; then
        echo "$FOUND" >&2
        die "The files above contain words listed in .release-forbidden."
    fi
fi

# The unpacked copy has to run: importing the package from it proves every
# module it needs made it through the allowlist.
# PYTHONDONTWRITEBYTECODE: the check must not leave __pycache__ behind in the
# folder that is packed next.
(cd "$DIR" && PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python3 -c "
import sys; sys.path.insert(0, '.')
import ksyssupervisor.window, ksyssupervisor.fanwindow, ksyssupervisor.stresswindow
import ksyssupervisor.updater
from ksyssupervisor import fanctl
assert fanctl.bundled_helper()[0], 'the fan helper is missing'
") || die "The assembled release does not import."

info "Packing $NAME.tar.gz..."
tar -C "$OUT" --owner=0 --group=0 --numeric-owner --sort=name \
    -czf "$OUT/$NAME.tar.gz" "$NAME"

# ---- the AppImage -----------------------------------------------------------

ASSETS="$NAME.tar.gz"
if [ "$APPIMAGE" = 1 ]; then
    info "Building the AppImage..."
    "$ROOT/packaging/build-appimage.sh" || die "The AppImage build failed."
    cp "$ROOT/dist/$NAME-x86_64.AppImage" "$OUT/" \
        || die "build-appimage.sh did not produce $NAME-x86_64.AppImage."
    ASSETS="$ASSETS $NAME-x86_64.AppImage"
fi

# ---- checksums --------------------------------------------------------------

info "Writing SHA256SUMS..."
# shellcheck disable=SC2086 # ASSETS is a list of plain file names
(cd "$OUT" && sha256sum $ASSETS > SHA256SUMS)

echo
info "Release $VERSION is in $OUT:"
(cd "$OUT" && ls -lh $ASSETS SHA256SUMS | awk '{print "   ", $5, $NF}')
echo "    $NAME/  (the same files as the .tar.gz, unpacked)"
echo
echo "Publish: tag v$VERSION, then create a GitHub release with the files above."
