"""
The macOS integration: what replaces the registry, Win32 and Explorer.

Two kinds of test here. The Quick Action is files on disk, so it is built in a
temporary home and read back. The clipboard and the convert route touch the
real system, so they only run on a Mac - and the clipboard test reads the
pasteboard back, for the reason CLAUDE.md gives: asserting that a copy was
*called* reported eleven successful copies on Windows while none had happened.
"""

import os
import plistlib
import shutil
import sys

import pytest

import core
import macos

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "..", "docs", "sample_render.exr")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway home, so nothing lands in the real ~/Library/Services."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(macos, "refresh_services", lambda: None)
    return tmp_path


# ---------------------------------------------------------------------------
# Bundle detection
# ---------------------------------------------------------------------------

@mac_only
def test_bundle_path_finds_the_app():
    exe = "/Applications/EXR to sRGB.app/Contents/MacOS/EXR to sRGB"
    assert macos.bundle_path(exe) == "/Applications/EXR to sRGB.app"


@mac_only
def test_bundle_path_is_none_for_an_interpreter():
    """A python running the script is not something Launch Services can pick."""
    assert macos.bundle_path("/usr/local/bin/python3") is None


@mac_only
def test_source_run_is_not_a_bundle(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert macos.bundle_path() is None


# ---------------------------------------------------------------------------
# Quick Action
# ---------------------------------------------------------------------------

ARGV = ["/Applications/EXR to sRGB.app/Contents/MacOS/EXR to sRGB"]


def _installed(home):
    root = os.path.join(str(home), "Library", "Services", macos.QUICK_ACTION_FILE,
                        "Contents")
    with open(os.path.join(root, "Info.plist"), "rb") as fh:
        info = plistlib.load(fh)
    with open(os.path.join(root, "document.wflow"), "rb") as fh:
        wf = plistlib.load(fh)
    return info, wf


@mac_only
def test_quick_action_installs_and_removes(home):
    assert not macos.quick_action_state()
    assert macos.set_quick_action(True, ARGV) is True
    assert macos.quick_action_state()
    assert macos.set_quick_action(False, ARGV) is False
    assert not macos.quick_action_state()


@mac_only
def test_quick_action_is_scoped_to_exr_in_finder(home):
    """Without these two it would sit in the menu of every file on the Mac."""
    macos.set_quick_action(True, ARGV)
    info, _ = _installed(home)
    svc = info["NSServices"][0]
    assert svc["NSSendFileTypes"] == ["com.ilm.openexr-image"]
    assert svc["NSRequiredContext"] == {"NSApplicationIdentifier": "com.apple.finder"}
    assert svc["NSMenuItem"]["default"] == macos.QUICK_ACTION_LABEL
    assert svc["NSMessage"] == "runWorkflowAsService"


@mac_only
def test_quick_action_passes_files_as_arguments(home):
    macos.set_quick_action(True, ARGV)
    _, wf = _installed(home)
    params = wf["actions"][0]["action"]["ActionParameters"]
    assert params["inputMethod"] == 1, "input must arrive as \"$@\", not stdin"
    assert wf["workflowMetaData"]["serviceInputTypeIdentifier"] == \
        "com.apple.Automator.fileSystemObject"


@mac_only
def test_quick_action_quotes_a_path_with_spaces(home):
    """The bundle is 'EXR to sRGB.app' - unquoted, zsh would run 'EXR'."""
    import shlex
    script = macos.quick_action_script(ARGV)
    words = shlex.split(script)
    assert words[:3] == ["exec", ARGV[0], "--convert-menu"]
    assert words[3] == "$@"


@mac_only
def test_repair_repoints_a_moved_app(home):
    macos.set_quick_action(True, ARGV)
    moved = ["/Users/someone/Applications/EXR to sRGB.app/Contents/MacOS/EXR to sRGB"]
    assert macos.repair_quick_action(moved) is True
    assert moved[0] in macos.installed_script()
    assert macos.repair_quick_action(moved) is False, "second pass is a no-op"


@mac_only
def test_repair_never_installs(home):
    """Like repair_association: it only fixes what the user already turned on."""
    assert macos.repair_quick_action(ARGV) is False
    assert not macos.quick_action_state()


# ---------------------------------------------------------------------------
# Things that need a real Mac
# ---------------------------------------------------------------------------

@mac_only
def test_prefs_live_in_application_support():
    import exr2srgb as app
    assert app.prefs_path().endswith(
        os.path.join("Library", "Application Support", "EXRtoSRGB", "prefs.json"))


@mac_only
def test_clipboard_round_trip():
    before = macos.get_clipboard()
    try:
        text = "#1f20f1 · ünïcode · 0.18"
        assert macos.set_clipboard(text)
        assert macos.get_clipboard() == text
    finally:
        if before is not None:
            macos.set_clipboard(before)


@mac_only
def test_exr_uti_is_what_launch_services_assigns():
    from UniformTypeIdentifiers import UTType
    assert UTType.typeWithFilenameExtension_("exr").identifier() == macos.EXR_UTI


@mac_only
def test_convert_menu_converts_with_the_right_click_settings(tmp_path, monkeypatch):
    """
    The Quick Action is a fourth route to the same pixels. It must write what
    the Windows verbs write for the same preset - same settings blob, same
    file - or a Mac convert and a PC convert of one render would differ.
    """
    import exr2srgb as app
    src = tmp_path / "shot.0001.exr"
    shutil.copy(SAMPLE, src)
    seen = {}
    monkeypatch.setattr(macos, "choose", lambda t, p, labels, default=None:
                        seen.setdefault("pick", labels[0]))
    monkeypatch.setattr(macos, "notify", lambda t, m: seen.setdefault("note", m))
    monkeypatch.setattr(macos, "alert", lambda t, m: seen.setdefault("alert", m))

    assert app.convert_menu([str(src), str(tmp_path / "notes.txt")]) == 0
    out = tmp_path / "shot.0001_srgb.png"
    assert out.exists()
    assert "alert" not in seen
    assert "Saved 1 file" in seen["note"]

    # identical to the single-file shell route for the same preset
    ref = tmp_path / "ref"
    ref.mkdir()
    shutil.copy(SAMPLE, ref / "shot.0001.exr")
    _k, _label, fmt, bits, transfer = app.CONVERT_VERBS[0]
    ref_out = app.convert_cli(str(ref / "shot.0001.exr"), fmt, bits, transfer)
    import OpenImageIO as oiio
    a = oiio.ImageBuf(str(out)).get_pixels(oiio.UINT16)
    b = oiio.ImageBuf(ref_out).get_pixels(oiio.UINT16)
    assert (a == b).all()


@mac_only
def test_convert_menu_cancel_writes_nothing(tmp_path, monkeypatch):
    import exr2srgb as app
    src = tmp_path / "a.exr"
    shutil.copy(SAMPLE, src)
    monkeypatch.setattr(macos, "choose", lambda *a, **k: None)
    assert app.convert_menu([str(src)]) == 0
    assert sorted(os.listdir(tmp_path)) == ["a.exr"]


@mac_only
def test_update_is_never_offered_as_an_install_off_windows(monkeypatch):
    """The asset is an Inno Setup .exe; on a Mac the button is the release page."""
    import io
    import json
    import urllib.request
    import exr2srgb as app
    body = json.dumps({"tag_name": "v99.0.0", "html_url": app.RELEASES_PAGE,
                       "assets": [{"name": "EXRtoSRGB_Setup_v99.0.0.exe",
                                   "browser_download_url": app.ASSET_PREFIX + "x.exe",
                                   "size": 1, "digest": "sha256:00"}]})

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: R(body.encode()))
    info = app.check_for_update()
    assert info["available"] is True
    assert info["installable"] is False


def test_core_has_no_mac_imports():
    """macos.py is shell integration; pixels never depend on it."""
    src = open(core.__file__, encoding="utf-8").read()
    assert "import macos" not in src


# ---------------------------------------------------------------------------
# The default-app prompt
# ---------------------------------------------------------------------------

class _Err:
    """Just enough NSError for _declined: a code and an underlying error."""

    def __init__(self, code, under=None):
        self._code, self._under = code, under

    def code(self):
        return self._code

    def userInfo(self):
        return {"NSUnderlyingError": self._under} if self._under else {}


def test_a_declined_prompt_is_recognised_through_the_wrapper():
    """
    Measured on macOS 27: saying no comes back as NSCocoaErrorDomain 256,
    "The file couldn't be opened", with userCanceledErr underneath. Read at
    the top level only, that is an error message for a user's decision.
    """
    assert macos._declined(_Err(256, _Err(-128)))
    assert macos._declined(_Err(-128))
    assert not macos._declined(_Err(256, _Err(-43)))
    assert not macos._declined(None)
