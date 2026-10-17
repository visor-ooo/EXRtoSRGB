# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller config for EXR -> sRGB on macOS. Build with ./build_mac.sh.

The Windows spec's three load-bearing parts apply here too - collect_all for
OpenImageIO and PyOpenColorIO (their dylibs), ui/ as data, pywebview's own
hooks - plus three that are macOS-only:

  * onedir + BUNDLE, not onefile. A one-file .app unpacks itself to a temp
    folder on every launch, which is slow and which Gatekeeper and the
    signing rules both treat badly. A bundle is already a folder.
  * argv_emulation=True. Launch Services does not put a double-clicked file
    in argv; it sends an Apple Event. The bootloader turns the launch event
    into argv, so main() sees a cold-start double-click exactly as it does on
    Windows. Events after launch are handled in macos.install_app_events.
  * CFBundleDocumentTypes. Without it Launch Services will not list the app
    under "Open With" or accept it as the default for .exr - the macOS
    counterpart of the OpenWithProgids / Capabilities keys on Windows.
"""
import re

from PyInstaller.utils.hooks import collect_all

with open('exr2srgb.py', encoding='utf-8') as fh:
    VERSION = re.search(r'^VERSION = "([^"]+)"', fh.read(), re.M).group(1)

# The bundle shows in Finder and /Applications under its folder name, and that
# name is the plain-ASCII one, like FRIENDLY_NAME in the registry: it is typed
# in Terminal and quoted in the Quick Action. The arrow is for what the user
# reads - the menu bar and Dock carry CFBundleName.
BUNDLE_NAME = 'EXR to sRGB.app'
EXE_NAME = 'EXRtoSRGB'

datas = [('ui', 'ui'), ('app.icns', '.')]
binaries = []
# cli is imported inside main(); macos and the pyobjc frameworks it uses are
# imported inside functions. Static analysis follows those, but naming them
# keeps a lazy import from becoming a ModuleNotFoundError only the exe sees.
hiddenimports = ['webview.platforms.cocoa', 'cli', 'macos',
                 'AppKit', 'Foundation', 'WebKit', 'UniformTypeIdentifiers',
                 'objc', 'PyObjCTools.AppHelper']

for pkg in ('OpenImageIO', 'PyOpenColorIO', 'webview'):
    d, b, h = collect_all(pkg)
    # pywebview ships the WebView2 and .NET DLLs for Windows under
    # webview/lib; on a Mac they are dead weight.
    datas += [x for x in d if not x[1].startswith('webview/lib')]
    binaries += b
    hiddenimports += [m for m in h if not m.startswith((
        'webview.platforms.winforms', 'webview.platforms.edgechromium',
        'webview.platforms.mshtml', 'webview.platforms.cef',
        'webview.platforms.gtk', 'webview.platforms.qt',
        'webview.platforms.android'))]


a = Analysis(
    ['exr2srgb.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PySide6', 'PyQt5', 'PyQt6', 'clr',
              'clr_loader', 'pythonnet', 'gi', 'PIL', 'pytest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # UPX breaks code signatures on macOS
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,              # the host's: OIIO wheels are per-arch
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='EXRtoSRGB',
)
app = BUNDLE(
    coll,
    name=BUNDLE_NAME,
    icon='app.icns',
    bundle_identifier='ooo.visor.EXRtoSRGB',
    version=VERSION,
    info_plist={
        'CFBundleName': 'EXR → sRGB',
        'CFBundleDisplayName': 'EXR → sRGB',
        'CFBundleShortVersionString': VERSION,
        'CFBundleVersion': VERSION,
        'LSApplicationCategoryType': 'public.app-category.graphics-design',
        # setDefaultApplicationAtURL:toOpenContentType: is macOS 12+.
        'LSMinimumSystemVersion': '12.0',
        'NSHighResolutionCapable': True,
        'NSHumanReadableCopyright': 'VISOR · MIT License',
        'CFBundleDocumentTypes': [{
            'CFBundleTypeName': 'OpenEXR image',
            # Viewer, not Editor: the app never writes back to an .exr.
            'CFBundleTypeRole': 'Viewer',
            # Alternate: listed under Open With, never the default unless
            # the user ticks the toggle. Owner would let Launch Services hand
            # us .exr on install, which is the silent takeover the Windows
            # build refuses to do as well.
            'LSHandlerRank': 'Alternate',
            'LSItemContentTypes': ['com.ilm.openexr-image'],
        }],
    },
)
