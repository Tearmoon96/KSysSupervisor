#!/bin/sh
# Build a self-contained KSysSupervisor AppImage.
#
# The bundle carries its own CPython, PyQt6 and psutil, so it runs on a machine
# that has neither installed. It deliberately does NOT carry libGL, libX11 or
# the graphics drivers: those have to be the host's, or the GPU stress test
# would run on a software rasteriser and the monitor would read the wrong card.
#
# Base interpreter: python-appimage's manylinux build. The wheels installed on
# top come from this machine's pip, so the result needs whatever glibc those
# wheels were built for - currently 2.28, i.e. anything from 2018 onwards.
#
#   ./packaging/build-appimage.sh            build, pruning unused Qt modules
#   ./packaging/build-appimage.sh --full     keep every Qt module (much larger)
#   ./packaging/build-appimage.sh --clean    discard the build tree first
#
# Downloads are cached in build/appimage/cache and reused.

set -eu

PY_SERIES=3.13
PY_FULL=3.13.15
PY_TAG=cp313
ARCH=x86_64

BASE_URL="https://github.com/niess/python-appimage/releases/download/python${PY_SERIES}/python${PY_FULL}-${PY_TAG}-${PY_TAG}-manylinux2014_${ARCH}.AppImage"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUILD="$ROOT/build/appimage"
CACHE="$BUILD/cache"
APPDIR="$BUILD/AppDir"
OUTDIR="$ROOT/dist"

PRUNE=1
for arg in "$@"; do
    case "$arg" in
        --full)  PRUNE=0 ;;
        --clean) rm -rf "$APPDIR" ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\033[1;34m::\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31m!!\033[0m %s\n' "$1" >&2; exit 1; }

VERSION=$(sed -n 's/^APP_VERSION = "\(.*\)"/\1/p' "$ROOT/ksyssupervisor/__init__.py")
[ -n "$VERSION" ] || die "Could not read APP_VERSION."
info "KSysSupervisor $VERSION"

mkdir -p "$CACHE" "$OUTDIR"

fetch() {  # url destination
    [ -s "$2" ] && return 0
    info "Downloading $(basename "$2")..."
    curl -fL --retry 3 -o "$2.part" "$1" || die "Download failed: $1"
    mv "$2.part" "$2"
}

BASE="$CACHE/python-base.AppImage"
TOOL="$CACHE/appimagetool.AppImage"
fetch "$BASE_URL" "$BASE"
fetch "$TOOL_URL" "$TOOL"
chmod +x "$BASE" "$TOOL"

# ---- the interpreter --------------------------------------------------------

if [ ! -d "$APPDIR" ]; then
    info "Unpacking the base interpreter..."
    rm -rf "$BUILD/squashfs-root"
    (cd "$BUILD" && "$BASE" --appimage-extract >/dev/null) \
        || die "Could not unpack the base AppImage."
    mv "$BUILD/squashfs-root" "$APPDIR"
    # Its own launcher and metadata describe a bare Python, not this app.
    rm -f "$APPDIR"/AppRun "$APPDIR"/*.desktop "$APPDIR"/*.png "$APPDIR"/.DirIcon
    rm -rf "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo"
fi

# The real interpreter, not usr/bin/python3.13 - that is a bash wrapper which
# needs APPDIR already exported and only forwards to this one anyway.
PYHOME="$APPDIR/opt/python$PY_SERIES"
PYBIN="$PYHOME/bin/python$PY_SERIES"
[ -x "$PYBIN" ] || die "No interpreter at $PYBIN."

# ---- the dependencies -------------------------------------------------------

info "Installing PyQt6 and psutil into the bundle..."
# --no-user, and an empty PYTHONPATH: the host's own site-packages must not
# leak in, or the bundle silently depends on a machine it will not run on.
env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 \
    "$PYBIN" -m pip install --no-cache-dir --upgrade --no-warn-script-location \
    -r "$ROOT/requirements.txt" >/dev/null || die "pip install failed."

SITE=$("$PYBIN" -c 'import site; print(site.getsitepackages()[0])')

# ---- the application --------------------------------------------------------

info "Copying the application..."
SHARE="$APPDIR/usr/share/ksyssupervisor"
rm -rf "$SHARE"
mkdir -p "$SHARE"
cp -r "$ROOT/ksyssupervisor" "$SHARE/"
cp "$ROOT/KSysSupervisor.py" "$SHARE/"
cp "$ROOT/README.md" "$ROOT/LICENSE" "$ROOT/CHANGELOG.md" "$SHARE/"
# Shipped so that --install-fan-helper has something to install: an AppImage
# user has no checkout to take them from. Named one by one: the rest of
# packaging/ is build tooling.
mkdir -p "$SHARE/helpers" "$SHARE/packaging"
cp "$ROOT/helpers/ksyssupervisor-fanhelper" "$SHARE/helpers/"
cp "$ROOT/packaging/org.ksyssupervisor.fancontrol.policy" "$SHARE/packaging/"
chmod 755 "$SHARE/helpers/ksyssupervisor-fanhelper"
mkdir -p "$SHARE/Icons"
cp "$ROOT/Icons/iconblue.svg" "$SHARE/Icons/"
find "$SHARE" -name __pycache__ -type d -prune -exec rm -rf {} +

# ---- trimming ---------------------------------------------------------------

if [ "$PRUNE" = 1 ]; then
    info "Removing Qt modules the application does not use..."
    QT="$SITE/PyQt6/Qt6"
    # The app uses QtCore, QtGui, QtWidgets, QtOpenGL, QtOpenGLWidgets, QtSvg
    # (the icon) and QtDBus (portals, indirectly). Everything below is reachable
    # from none of them; the smoke test at the end is what proves it.
    for mod in Qml Quick Quick3D QuickWidgets QuickTest QuickControls2 \
               QuickParticles QuickShapes QuickTemplates2 QuickLayouts \
               QuickDialogs2 QuickDialogs2QuickImpl QuickDialogs2Utils \
               QuickEffects QuickVectorImage QuickVectorImageGenerator \
               Multimedia MultimediaWidgets MultimediaQuick SpatialAudio \
               Pdf PdfQuick PdfWidgets Bluetooth Nfc Positioning \
               PositioningQuick Sensors SensorsQuick SerialPort SerialBus \
               WebChannel WebChannelQuick WebSockets WebView Sql Test \
               Designer DesignerComponents Help Charts ChartsQml \
               DataVisualization DataVisualizationQml \
               3DCore 3DRender 3DInput 3DLogic 3DAnimation 3DExtras \
               3DQuick 3DQuickScene2D 3DQuickExtras 3DQuickRender \
               3DQuickInput 3DQuickAnimation Scxml StateMachine \
               RemoteObjects TextToSpeech ShaderTools LabsAnimation \
               LabsFolderListModel LabsQmlModels LabsSettings \
               LabsSharedImage LabsWavefrontMesh LabsPlatform \
               ; do
        rm -f "$QT/lib/libQt6${mod}.so"* 2>/dev/null || true
        rm -f "$SITE/PyQt6/Qt${mod}."*.so "$SITE/PyQt6/Qt${mod}.pyi" 2>/dev/null || true
    done
    rm -rf "$QT/qml" "$QT/plugins/multimedia" "$QT/plugins/sqldrivers" \
           "$QT/plugins/designer" "$QT/plugins/qmltooling" \
           "$QT/plugins/position" "$QT/plugins/sensors" \
           "$QT/plugins/webview" "$QT/plugins/texttospeech" \
           "$QT/plugins/renderers" "$QT/plugins/geometryloaders" \
           "$QT/plugins/sceneparsers" "$QT/libexec" 2>/dev/null || true
    # Translations for a UI that has none of its own.
    rm -rf "$QT/translations"
    # Development leftovers of no use at runtime.
    find "$SITE/PyQt6" -name '*.pyi' -delete 2>/dev/null || true
    rm -rf "$SITE/PyQt6/bindings" "$SITE/PyQt6/Qt6/include" 2>/dev/null || true
    find "$APPDIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    find "$PYHOME/lib/python$PY_SERIES" -maxdepth 2 -type d \
         \( -name test -o -name tests -o -name idlelib -o -name turtledemo \
            -o -name tkinter \) -prune -exec rm -rf {} + 2>/dev/null || true
    # Tk: the application has no use for it and it is not small. pip is left
    # alone on purpose - removing it makes the AppDir unusable for the next
    # incremental build, which is a worse trade than the space it saves.
    rm -rf "$APPDIR/usr/share/tcltk" 2>/dev/null || true
fi

# ---- launcher and metadata --------------------------------------------------

info "Writing the launcher..."
cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE=\$(dirname "\$(readlink -f "\$0")")
export APPDIR="\$HERE"

# The interpreter works out its own prefix from its path, so PYTHONHOME is not
# set - it is cleared, along with PYTHONPATH and the user site directory,
# because a bundle that picks up the host's packages is not a bundle.
unset PYTHONHOME PYTHONPATH
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
export SSL_CERT_FILE="\$HERE/opt/_internal/certs.pem"

# System OpenGL and graphics drivers stay the host's: bundling them would put
# the GPU stress test on a software rasteriser.
exec "\$HERE/opt/python$PY_SERIES/bin/python$PY_SERIES" \\
     "\$HERE/usr/share/ksyssupervisor/KSysSupervisor.py" "\$@"
EOF
chmod 755 "$APPDIR/AppRun"

cat > "$APPDIR/ksyssupervisor.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=KSysSupervisor
GenericName=Hardware Monitor
Comment=Temperatures, fans, clocks and power, with fan control and stress tests
Exec=AppRun %U
Icon=ksyssupervisor
Terminal=false
Categories=System;Monitor;
Keywords=hardware;sensors;temperature;fan;cpu;gpu;stress;
StartupWMClass=KSysSupervisor
EOF

mkdir -p "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$APPDIR/ksyssupervisor.desktop" "$APPDIR/usr/share/applications/"
cp "$ROOT/Icons/iconblue.svg" \
   "$APPDIR/usr/share/icons/hicolor/scalable/apps/ksyssupervisor.svg"
cp "$ROOT/Icons/iconblue.svg" "$APPDIR/ksyssupervisor.svg"
ln -sf ksyssupervisor.svg "$APPDIR/.DirIcon"

# ---- smoke test -------------------------------------------------------------

info "Checking the bundle can build its windows..."
env -u PYTHONPATH -u PYTHONHOME PYTHONNOUSERSITE=1 \
    QT_QPA_PLATFORM=offscreen \
    "$PYBIN" - "$SHARE" <<'EOF' || die "The bundle failed its smoke test."
import sys
sys.path.insert(0, sys.argv[1])
import psutil                                       # noqa: F401
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon
from PyQt6.QtOpenGLWidgets import QOpenGLWidget     # noqa: F401
from ksyssupervisor.window import KSysSupervisor
from ksyssupervisor.stresswindow import StressWindow
from ksyssupervisor.fanwindow import FanControlWindow  # noqa: F401
from ksyssupervisor import fanctl, installation, updater  # noqa: F401

app = QApplication(sys.argv[:1])
icon = QIcon(sys.argv[1] + "/Icons/iconblue.svg")
assert not icon.isNull(), "the SVG icon plugin was pruned away"
window = KSysSupervisor(dep_report=([], []))
window.close()
stress = StressWindow()
stress._closed = True
stress.close()
assert fanctl.bundled_helper()[0] is not None, "the fan helper was not bundled"
print("smoke test ok")
# The sensor worker's threads are still running; this is a check, not a session.
import os
sys.stdout.flush()
os._exit(0)
EOF

# ---- pack -------------------------------------------------------------------

OUT="$OUTDIR/KSysSupervisor-$VERSION-$ARCH.AppImage"
info "Packing $(basename "$OUT")..."
rm -f "$OUT"
# appimagetool is itself an AppImage; where FUSE is unavailable (a container,
# a flatpak sandbox) it can still run by extracting itself first.
ARCH=$ARCH "$TOOL" --no-appstream "$APPDIR" "$OUT" >/dev/null 2>&1 \
    || ARCH=$ARCH "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT" \
    || die "appimagetool failed."
chmod 755 "$OUT"

info "Done: $OUT ($(du -h "$OUT" | cut -f1))"
