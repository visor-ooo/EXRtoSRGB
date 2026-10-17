#!/bin/zsh
# ============================================================
#  Build "EXR to sRGB.app" and a .dmg  (run on a Mac, in this folder)
#
#  Output goes to dist/, which Dropbox is told to ignore: a 100+ MB bundle
#  of dylibs syncing to the Windows machine is the opposite of useful, and
#  the .app, like the .exe, ships as a GitHub Release asset, never in git.
#
#  The build is for this Mac's architecture. OpenImageIO's wheels are per
#  architecture, so an Apple Silicon build does not run on an Intel Mac -
#  build there for that.
# ============================================================
set -euo pipefail
cd "${0:A:h}"

APP="dist/EXR to sRGB.app"
VERSION=$(sed -nE 's/^VERSION = "([^"]+)"/\1/p' exr2srgb.py)
ARCH=$(uname -m)
DMG="dist/EXRtoSRGB_v${VERSION}_macOS_${ARCH}.dmg"

# 1) a venv that Dropbox does not sync - see "Inside Dropbox" in CLAUDE.md.
#    Mark the folder before anything is written into it.
if [[ ! -d .venv ]]; then
  mkdir .venv && xattr -w com.dropbox.ignored 1 .venv
  if command -v uv >/dev/null; then
    uv venv --python 3.12 .venv --allow-existing
  else
    python3 -m venv .venv
  fi
fi
if command -v uv >/dev/null; then
  uv pip install --python .venv/bin/python -q OpenImageIO OpenColorIO pywebview pyinstaller
else
  .venv/bin/python -m pip install -q OpenImageIO OpenColorIO pywebview pyinstaller
fi

[[ -d dist ]] || { mkdir dist && xattr -w com.dropbox.ignored 1 dist; }

# 2) build from the spec. Scratch goes to $TMPDIR, as on Windows to %TEMP%.
.venv/bin/python -m PyInstaller --noconfirm --clean \
  --distpath dist --workpath "${TMPDIR:-/tmp}/EXRtoSRGB-build" \
  EXRtoSRGB-mac.spec

# 3) sign the bundle as a whole, ad hoc. PyInstaller signs each binary, but
#    the bundle's own signature has to cover the final Info.plist and
#    resources, or Apple Silicon refuses to launch it as "damaged".
#    Ad hoc is not notarised: a copy downloaded from GitHub still needs
#    right-click > Open the first time. A copy built here does not.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"

# 4) a disk image with an Applications shortcut, for the release page
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "EXR to sRGB ${VERSION}" -srcfolder "$STAGE" \
  -ov -format UDZO "$DMG"
rm -rf "$STAGE"

# 5) report what actually happened
echo
echo "Built  $APP  ($(du -sh "$APP" | cut -f1))"
echo "Image  $DMG  ($(du -h "$DMG" | cut -f1))"
# --diag imports the whole app headless, so it is the cheapest proof that
# nothing the frozen build needs was left out of the bundle.
"$APP/Contents/MacOS/EXRtoSRGB" --diag
