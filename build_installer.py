#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the Windows installer.

    python build_installer.py

Wraps Inno Setup rather than a .bat because everything this needs - reading
VERSION out of the source, locating ISCC, checking the payload exists - is
awkward in cmd and cost a broken build once already: a python one-liner inside a
`for /f` dies on its own parentheses.

Expects EXRtoSRGB.exe to be built already (build_exe.bat). Produces
EXRtoSRGB_Setup_v<version>.exe next to it.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Inno Setup installs per-user by default, which is where winget puts it; the
# Program Files locations are there for a machine-wide install.
ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                 "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def version():
    with open(os.path.join(HERE, "exr2srgb.py"), encoding="utf-8") as fh:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    if not m:
        sys.exit("could not read VERSION from exr2srgb.py")
    return m.group(1)


def find_iscc():
    for path in ISCC_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    import shutil
    found = shutil.which("ISCC")
    if found:
        return found
    sys.exit("Inno Setup not found. Install it with:\n"
             "    winget install --id JRSoftware.InnoSetup")


def main():
    ver = version()
    exe = os.path.join(HERE, "EXRtoSRGB.exe")
    if not os.path.isfile(exe):
        sys.exit("EXRtoSRGB.exe is not here - run build_exe.bat first.")

    iscc = find_iscc()
    out = "EXRtoSRGB_Setup_v%s.exe" % ver
    print("Building %s" % out)
    print("  from   %s (%.1f MB)" % (os.path.basename(exe),
                                     os.path.getsize(exe) / 1e6))
    print("  using  %s\n" % iscc)

    # Compile outside the repo, then move the finished file in.
    #
    # The repo lives in Dropbox since 2026-10-16, and Inno builds the setup
    # program by writing the file and then patching its resources in place.
    # Dropbox picks the new 45 MB file up mid-write and holds it, so the patch
    # fails: "Resource update error: EndUpdateResource failed, try excluding
    # the Output folder from your antivirus software (110)". Reproducible on
    # every run, and it is not antivirus. Building into %TEMP% keeps the sync
    # client off the file until it is finished - the same reason build_exe.bat
    # sends PyInstaller's scratch there.
    scratch = tempfile.mkdtemp(prefix="EXRtoSRGB-iscc-")
    try:
        r = subprocess.run(
            [iscc, "/DAppVersion=%s" % ver, "/O%s" % scratch,
             os.path.join(HERE, "installer.iss")],
            cwd=HERE)
        if r.returncode != 0:
            sys.exit("\nBUILD FAILED - Inno Setup returned %d" % r.returncode)

        built = os.path.join(scratch, out)
        if not os.path.isfile(built):
            sys.exit("\nBUILD FAILED - Inno Setup reported success but %s is "
                     "not in %s." % (out, scratch))

        produced = os.path.join(HERE, out)
        if os.path.exists(produced):
            os.remove(produced)
        shutil.move(built, produced)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    print("\nDone.  %s  (%.1f MB)" % (out, os.path.getsize(produced) / 1e6))


if __name__ == "__main__":
    main()
