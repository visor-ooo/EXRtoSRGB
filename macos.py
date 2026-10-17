# -*- coding: utf-8 -*-
"""
macOS integration - what exr2srgb.py does through the registry and Win32 on
Windows, done the way macOS expects it.

Imported only on darwin, and lazily, so nothing here is ever loaded on Windows
and the Windows code paths stay exactly as they were. Like core.py it has no
pywebview import at module level: install_app_events() reaches into pywebview's
Cocoa backend only when it is called, which is after the GUI has been chosen.

The mapping, one Windows mechanism to one macOS one:

    HKCU prefs dir           ~/Library/Application Support/EXRtoSRGB
    Win32 clipboard          NSPasteboard
    explorer /select         open -R
    MessageBoxW              osascript `display alert`
    .exr ProgID + UserChoice Launch Services default handler for the UTI
    Explorer verb submenu    a Quick Action in ~/Library/Services
    --view argv per process  application:openFiles: into the running process
"""

import os
import plistlib
import shlex
import subprocess
import sys
import threading
import uuid

# The UTI Launch Services assigns to .exr. Declared by macOS itself, so the
# app only *claims* it in Info.plist (CFBundleDocumentTypes) and never has to
# export a type of its own.
EXR_UTI = "com.ilm.openexr-image"
BUNDLE_ID = "ooo.visor.EXRtoSRGB"
PREVIEW = "/System/Applications/Preview.app"

# Quick Action. The label is what Finder shows under Quick Actions; the
# ellipsis says a choice follows, which it does.
QUICK_ACTION_LABEL = "Convert to sRGB…"
QUICK_ACTION_FILE = "Convert to sRGB (EXR to sRGB).workflow"
PBS = "/System/Library/CoreServices/pbs"


# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------

def support_dir():
    return os.path.join(os.path.expanduser("~"), "Library",
                        "Application Support", "EXRtoSRGB")


def bundle_path(executable=None):
    """
    The .app this process runs from, or None when running from source.

    Only a bundle can be a default handler or show up in "Open With": Launch
    Services knows applications by bundle, and a python interpreter running a
    script is not one. That is why both integration toggles report
    unsupported from a checkout instead of registering the interpreter -
    the macOS version of the Applications\\python.exe trap on Windows.
    """
    if executable is None and not getattr(sys, "frozen", False):
        return None
    exe = os.path.realpath(executable or sys.executable)
    marker = ".app" + os.sep + "Contents" + os.sep + "MacOS" + os.sep
    i = exe.find(marker)
    return exe[:i + 4] if i >= 0 else None


# ----------------------------------------------------------------------------
# Clipboard, Finder, dialogs
# ----------------------------------------------------------------------------

def set_clipboard(text):
    """
    Put text on the general pasteboard.

    Same reason as the Win32 version: the UI is a file:// page, so
    navigator.clipboard is unavailable and fails by rejecting a promise.
    """
    import AppKit
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    return bool(pb.setString_forType_(str(text), AppKit.NSPasteboardTypeString))


def get_clipboard():
    """Read it back - the only test of a copy that means anything."""
    import AppKit
    pb = AppKit.NSPasteboard.generalPasteboard()
    return pb.stringForType_(AppKit.NSPasteboardTypeString)


def reveal(path):
    """Select the file in a Finder window."""
    if not os.path.exists(path):
        return False
    subprocess.Popen(["open", "-R", path])
    return True


def _osascript(lines, args=(), timeout=None):
    """
    Run AppleScript with untrusted strings passed as argv, never interpolated.

    File and object names come from the DCC and are arbitrary Unicode;
    splicing them into script source is how a quote in a filename becomes a
    syntax error, or worse.
    """
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    cmd += [str(a) for a in args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def alert(title, message):
    """The MessageBoxW of a headless convert: no console to print to."""
    try:
        _osascript(["on run argv",
                    "display alert (item 1 of argv) message (item 2 of argv) "
                    "as critical",
                    "end run"], [title, message], timeout=600)
    except Exception:                               # noqa: BLE001
        pass


def notify(title, message):
    try:
        _osascript(["on run argv",
                    "display notification (item 2 of argv) "
                    "with title (item 1 of argv)",
                    "end run"], [title, message], timeout=20)
    except Exception:                               # noqa: BLE001
        pass


def choose(title, prompt, labels, default=None):
    """
    A one-pick list dialog. Returns the label, or None if cancelled.

    `choose from list` rather than a window of our own: the Quick Action runs
    a headless process, and the system dialog appears in a fraction of the
    time it takes a webview to start.
    """
    script = [
        "on run argv",
        "set t to item 1 of argv",
        "set p to item 2 of argv",
        "set d to item 3 of argv",
        "set opts to items 4 thru -1 of argv",
        "activate",
        "set r to choose from list opts with title t with prompt p "
        "default items {d} OK button name \"Convert\"",
        "if r is false then return \"\"",
        "return item 1 of r",
        "end run",
    ]
    r = _osascript(script, [title, prompt, default or labels[0]] + list(labels))
    pick = (r.stdout or "").strip()
    return pick if pick in labels else None


def set_dock_icon(icns):
    """
    Give a source run the aperture instead of Python's rocket.

    A bundle gets its icon from Info.plist; this only matters from a checkout,
    the same job webview.start(icon=...) does for a Windows source run.
    """
    try:
        import AppKit
        img = AppKit.NSImage.alloc().initWithContentsOfFile_(icns)
        if img:
            AppKit.NSApplication.sharedApplication() \
                .setApplicationIconImage_(img)
    except Exception:                               # noqa: BLE001
        pass


# ----------------------------------------------------------------------------
# Default handler for .exr
#
# Unlike Windows there is a supported API for this, and it is the whole
# mechanism: Launch Services stores the choice per user and applies it at
# once. Ground truth is still read back from Launch Services rather than
# assumed from the call succeeding, for the same reason association_state on
# Windows asks the shell: a toggle that reads its own write agrees with itself
# whatever the OS does.
#
# **macOS asks the user first.** Every change - including handing the type
# back to Preview - puts up a system prompt, and the completion handler does
# not fire until it is answered. Measured on macOS 27: an accepted prompt took
# 18.6s, which is a person reading a dialog, not a slow API. So the wait is
# long, and a declined prompt comes back as userCanceledErr (-128) wrapped in
# NSCocoaErrorDomain 256 "The file couldn't be opened" - which reads like a
# failure and is actually the user's answer. It is raised as Declined so the
# caller can say "kept X" instead of reporting an error.
# ----------------------------------------------------------------------------

def _ws():
    import AppKit
    return AppKit.NSWorkspace.sharedWorkspace()


def _uttype():
    from UniformTypeIdentifiers import UTType
    return UTType.typeWithIdentifier_(EXR_UTI)


def default_handler():
    """Path of the app a double-click on an .exr opens, or None."""
    try:
        url = _ws().URLForApplicationToOpenContentType_(_uttype())
        return os.path.realpath(str(url.path())).rstrip("/") if url else None
    except Exception:                               # noqa: BLE001
        return None


def association_state(app=None):
    """(we_are_default, current_handler_path)."""
    app = app or bundle_path()
    current = default_handler()
    if not app or not current:
        return False, current
    return os.path.realpath(app).rstrip("/") == current, current


class Declined(Exception):
    """The user answered macOS's confirmation prompt with no."""


USER_CANCELED = -128


def _declined(err):
    """True when an NSError is, at any depth, userCanceledErr."""
    while err is not None:
        if err.code() == USER_CANCELED:
            return True
        err = err.userInfo().get("NSUnderlyingError") if err.userInfo() else None
    return False


def set_default_handler(app, timeout=600.0):
    """
    Make `app` the default for .exr. Returns the error text, or "" on success.
    Raises Declined if the user said no to the system prompt.

    Asynchronous in AppKit; waited on here because every caller wants to
    report the outcome. Called from a bridge worker thread, never the main
    one, so the completion handler is free to arrive on any queue. The
    timeout is long because the wait is on a person - see above.
    """
    import Foundation
    done = threading.Event()
    result = {"error": "timed out waiting for an answer to the macOS prompt",
              "declined": False}

    def finished(err):
        result["declined"] = _declined(err)
        result["error"] = str(err.localizedDescription()) if err else ""
        done.set()

    url = Foundation.NSURL.fileURLWithPath_(app)
    _ws().setDefaultApplicationAtURL_toOpenContentType_completionHandler_(
        url, _uttype(), finished)
    done.wait(timeout)
    if result["declined"]:
        raise Declined()
    return result["error"]


def set_association(enable, previous=None):
    """
    Claim or release .exr. Returns the handler to remember for a later release.

    Releasing has no "unset" on macOS - some app is always the default - so
    the handler we displaced is remembered on the way in and handed back on
    the way out, falling back to Preview, which ships with every Mac and
    opens EXR. Like the Windows unregister, it only acts while the claim is
    still ours, so it cannot stomp a choice the user made since.

    Either direction can raise Declined; the remembered handler is unchanged
    then, because nothing was.
    """
    app = bundle_path()
    if not app:
        raise RuntimeError("only the built app can open .exr files - "
                           "run the .app rather than the script")
    ours, current = association_state(app)
    if enable:
        if ours:
            return previous
        err = set_default_handler(app)
        if err:
            raise RuntimeError(err)
        return current
    if not ours:
        return previous
    target = previous if previous and os.path.isdir(previous) and \
        os.path.realpath(previous) != os.path.realpath(app) else PREVIEW
    err = set_default_handler(target)
    if err:
        raise RuntimeError(err)
    return None


# ----------------------------------------------------------------------------
# Quick Action - the right-click convert
#
# Finder's context menu takes Quick Actions: Automator workflows in
# ~/Library/Services, filtered to Finder and to the EXR type so they appear
# only where they apply. Per user, no permissions, and removable by deleting
# one folder - the same properties the HKCU verbs have.
#
# One action with a chooser rather than one per preset. Quick Actions cannot
# nest, so five entries would sit flat in every .exr's menu; the chooser is
# built from CONVERT_VERBS, so the presets still have one source.
#
# The workflow runs the app headless with --convert-menu. It records an
# absolute path, so like the Windows verbs it is repaired at startup when the
# app has moved.
# ----------------------------------------------------------------------------

def services_dir():
    return os.path.join(os.path.expanduser("~"), "Library", "Services")


def quick_action_path():
    return os.path.join(services_dir(), QUICK_ACTION_FILE)


def quick_action_script(argv):
    """The shell line the workflow runs: our command, then the selection."""
    return "exec %s --convert-menu \"$@\"" % " ".join(
        shlex.quote(a) for a in argv)


def _workflow_plist(script):
    def uid():
        return str(uuid.uuid4()).upper()

    def arg(i, name, default):
        return {"default value": default, "name": name, "required": "0",
                "type": "0", "uuid": str(i)}

    action = {
        "AMAccepts": {"Container": "List", "Optional": True,
                      "Types": ["com.apple.cocoa.string"]},
        "AMActionVersion": "2.0.3",
        "AMApplication": ["Automator"],
        "AMParameterProperties": {k: {} for k in (
            "COMMAND_STRING", "CheckedForUserDefaultShell", "inputMethod",
            "shell", "source")},
        "AMProvides": {"Container": "List",
                       "Types": ["com.apple.cocoa.string"]},
        "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
        "ActionName": "Run Shell Script",
        "ActionParameters": {
            "COMMAND_STRING": script,
            "CheckedForUserDefaultShell": True,
            # 1 = pass input as arguments, so "$@" is the selected files
            "inputMethod": 1,
            "shell": "/bin/zsh",
            "source": "",
        },
        "BundleIdentifier": "com.apple.RunShellScript",
        "CFBundleVersion": "2.0.3",
        "CanShowSelectedItemsWhenRun": False,
        "CanShowWhenRun": True,
        "Category": ["AMCategoryUtilities"],
        "Class Name": "RunShellScriptAction",
        "InputUUID": uid(),
        "Keywords": ["Shell", "Script", "Command", "Run", "Unix"],
        "OutputUUID": uid(),
        "UUID": uid(),
        "UnlocalizedApplications": ["Automator"],
        "arguments": {
            "0": arg(0, "inputMethod", 0),
            "1": arg(1, "CheckedForUserDefaultShell", False),
            "2": arg(2, "source", ""),
            "3": arg(3, "COMMAND_STRING", ""),
            "4": arg(4, "shell", "/bin/sh"),
        },
        "conversionLabel": 0,
        "isViewVisible": 1,
        "location": "309.000000:305.000000",
        "nibPath": "/System/Library/Automator/Run Shell Script.action"
                   "/Contents/Resources/Base.lproj/main.nib",
    }
    return {
        "AMApplicationBuild": "534",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [{"action": action, "isViewVisible": 1}],
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 15,
            "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def _workflow_info():
    return {
        "NSServices": [{
            "NSBackgroundColorName": "background",
            "NSIconName": "NSActionTemplate",
            "NSMenuItem": {"default": QUICK_ACTION_LABEL},
            "NSMessage": "runWorkflowAsService",
            # Finder only, .exr only: without these it would sit in the
            # context menu of every file on the machine.
            "NSRequiredContext": {"NSApplicationIdentifier": "com.apple.finder"},
            "NSSendFileTypes": [EXR_UTI],
        }],
    }


def quick_action_state():
    return os.path.isdir(quick_action_path())


def installed_script():
    """The command line the installed workflow runs, or None."""
    try:
        with open(os.path.join(quick_action_path(), "Contents",
                               "document.wflow"), "rb") as fh:
            wf = plistlib.load(fh)
        return wf["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
    except (OSError, KeyError, IndexError, ValueError, plistlib.InvalidFileException):
        return None


def refresh_services():
    """Make Finder see the change now rather than at next login."""
    try:
        subprocess.run([PBS, "-update"], timeout=20,
                       capture_output=True)
    except Exception:                               # noqa: BLE001
        pass


def set_quick_action(enable, argv):
    """Install or remove the Quick Action. Returns the new state."""
    import shutil
    path = quick_action_path()
    if not enable:
        if os.path.isdir(path):
            shutil.rmtree(path)
        refresh_services()
        return False
    contents = os.path.join(path, "Contents")
    os.makedirs(contents, exist_ok=True)
    with open(os.path.join(contents, "document.wflow"), "wb") as fh:
        plistlib.dump(_workflow_plist(quick_action_script(argv)), fh)
    with open(os.path.join(contents, "Info.plist"), "wb") as fh:
        plistlib.dump(_workflow_info(), fh)
    refresh_services()
    return True


def repair_quick_action(argv):
    """
    Re-point an installed Quick Action at this copy of the app.

    Moving the .app, or running a newer one from somewhere else, leaves the
    workflow calling a path that is gone - and Finder runs it anyway and
    fails silently. Only touches an action that is already installed.
    """
    current = installed_script()
    if current is None:
        return False
    wanted = quick_action_script(argv)
    if current == wanted:
        return False
    set_quick_action(True, argv)
    return True


# ----------------------------------------------------------------------------
# Finder events
#
# On Windows every double-click is a new process with the path in argv. On
# macOS Launch Services sends the path to the *running* app as an Apple Event
# instead, and a second process is never started. So the viewer is opened
# in-process from application:openFiles:, and clicking the Dock icon of an app
# that is only showing viewers has to bring the converter up itself
# (applicationShouldHandleReopen:hasVisibleWindows:). pywebview's delegate
# implements neither, so they are added to it as a category.
#
# The first event of a launch is turned into argv by PyInstaller's
# argv_emulation, so `main()` sees a cold-start double-click exactly as it
# does on Windows; these handle everything after that.
# ----------------------------------------------------------------------------

_handlers = {"open": None, "reopen": None}
_installed = False


def install_app_events(on_open_files, on_reopen):
    global _installed
    _handlers["open"] = on_open_files
    _handlers["reopen"] = on_reopen
    if _installed:
        return
    import objc
    from webview.platforms.cocoa import BrowserView

    def _spawn(fn, *args):
        # Off the main thread: creating a pywebview window from inside an
        # AppKit callback waits on the very run loop it is blocking.
        threading.Thread(target=fn, args=args, daemon=True).start()

    # pyobjc requires a category to carry the name of the class it extends;
    # these methods land on pywebview's own delegate, not a subclass.
    class AppDelegate(objc.Category(BrowserView.AppDelegate)):
        @objc.typedSelector(b"v@:@@")
        def application_openFiles_(self, app, filenames):
            paths = [str(p) for p in filenames]
            if _handlers["open"]:
                _spawn(_handlers["open"], paths)
            app.replyToOpenOrPrint_(0)  # NSApplicationDelegateReplySuccess

        @objc.typedSelector(b"Z@:@Z")
        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            if _handlers["reopen"]:
                _spawn(_handlers["reopen"])
            return True

    _installed = True
