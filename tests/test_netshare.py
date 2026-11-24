#!/usr/bin/env python3
"""Netshare + archive-input suite for PS2 Native Recomp Tools.

Covers the 2026-11-22 retrofit: network-share references and settings
persistence (passwords never stored), GVFS mount-path lookup, .7z
input through the bundled 7zz binary, probe regressions for .zip and
plain .iso, and the Add flow's archive staging semantics (stage ->
commit into work/<slug>/, cancel deletes) driven through the real
App under a virtual display.

Like the other suites it runs against a COPY of the app in
/tmp/app-test so the shipped folder stays pristine. Run with:

    xvfb-run -a python3 tests/test_netshare.py

(the GUI staging drive is skipped, with a note, when no DISPLAY is
available).
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import zipfile

APP_DIR = "/tmp/app-test"
spec = importlib.util.spec_from_file_location(
    "gui", os.path.join(APP_DIR, "ps2_native_gui.py"))
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)

passed = failed = 0
failures = []


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("PASS  %s" % name)
    else:
        failed += 1
        failures.append(name)
        print("FAIL  %s  %s" % (name, detail))


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


TMP = tempfile.mkdtemp(prefix="ps2gui-netshare-")

# ---------------------------------------------------------------------------
# Fixtures: a synthetic PS2 ISO (the tool's own mkiso.py, same recipe as
# the unit suite), a .zip wrapping it, and .7z archives built with the
# bundled 7zz itself.
# ---------------------------------------------------------------------------
boot = os.path.join(TMP, "SLUS_999.99")
with open(boot, "wb") as fh:
    fh.write(b"\x7fELF" + bytes(4096))
iso = os.path.join(TMP, "synth.iso")
mkiso = os.path.join(gui.TOOLS_SRC, "scripts", "mkiso.py")
rc = subprocess.run([sys.executable, "-I", mkiso, iso, boot]).returncode
assert rc == 0 and os.path.isfile(iso), "mkiso fixture failed"
iso_bytes = open(iso, "rb").read()

zip_path = os.path.join(TMP, "Fixture Game.zip")
with zipfile.ZipFile(zip_path, "w") as zf:
    zf.writestr("Fixture Game.iso", iso_bytes)

tool = gui.find_7z_tool()
stage = os.path.join(TMP, "stage7z")
os.makedirs(os.path.join(stage, "games", "deep"))
with open(os.path.join(stage, "games", "deep", "Fixture Game.iso"),
          "wb") as fh:
    fh.write(iso_bytes)
with open(os.path.join(stage, "readme.txt"), "w") as fh:
    fh.write("decoy, not an iso\n")
sevenz_path = os.path.join(TMP, "Fixture Game.7z")
subprocess.run([tool, "a", "-t7z", sevenz_path, "games", "readme.txt"],
               cwd=stage, check=True, capture_output=True)

stage2 = os.path.join(TMP, "stage7z-noiso")
os.makedirs(stage2)
with open(os.path.join(stage2, "data.bin"), "wb") as fh:
    fh.write(bytes(2048))
noiso_7z = os.path.join(TMP, "No Iso.7z")
subprocess.run([tool, "a", "-t7z", noiso_7z, "data.bin"],
               cwd=stage2, check=True, capture_output=True)

garbage_7z = os.path.join(TMP, "Garbage.7z")
with open(garbage_7z, "wb") as fh:
    fh.write(b"this is not a 7z archive at all" * 32)

# ---------------------------------------------------------------------------
# 1-8. parse_share_ref: every spelling the dialog accepts.
# ---------------------------------------------------------------------------
ref = gui.parse_share_ref("smb://192.168.0.102/PS2 ARCHIVE")
check("parse smb://server/share",
      ref == {"server": "192.168.0.102", "share": "PS2 ARCHIVE",
              "subpath": ""}, repr(ref))
ref = gui.parse_share_ref("smb://server/share/sub/dir")
check("parse smb:// with subpath",
      ref == {"server": "server", "share": "share",
              "subpath": "sub/dir"}, repr(ref))
ref = gui.parse_share_ref("\\\\SERVER\\share\\games")
check("parse UNC \\\\server\\share\\sub",
      ref == {"server": "SERVER", "share": "share",
              "subpath": "games"}, repr(ref))
ref = gui.parse_share_ref("//server/share")
check("parse //server/share",
      ref == {"server": "server", "share": "share", "subpath": ""},
      repr(ref))
ref = gui.parse_share_ref("server/share")
check("parse bare server/share",
      ref == {"server": "server", "share": "share", "subpath": ""},
      repr(ref))
check("parse bare server alone is not enough",
      gui.parse_share_ref("server") is None
      and gui.parse_share_ref("smb://server") is None)
check("parse empty / blank is None",
      gui.parse_share_ref("") is None
      and gui.parse_share_ref("   ") is None
      and gui.parse_share_ref(None) is None)
ref = gui.parse_share_ref("smb://server/PS2%20ARCHIVE")
check("parse URL-escaped share name",
      ref is not None and ref["share"] == "PS2 ARCHIVE", repr(ref))

# ---------------------------------------------------------------------------
# 9-11. UNC helpers and the smb:// URL builder.
# ---------------------------------------------------------------------------
check("is_unc_path",
      gui.is_unc_path("\\\\SERVER\\share")
      and gui.is_unc_path("//SERVER/share")
      and not gui.is_unc_path("/home/duane/game.iso")
      and not gui.is_unc_path("C:\\games"))
check("unc_root",
      gui.unc_root("SERVER", "share") == "\\\\SERVER\\share",
      gui.unc_root("SERVER", "share"))
check("smb_url quotes share, adds user",
      gui.smb_url("192.168.0.102", "PS2 ARCHIVE")
      == "smb://192.168.0.102/PS2%20ARCHIVE"
      and gui.smb_url("srv", "share", "duane")
      == "smb://duane@srv/share",
      gui.smb_url("192.168.0.102", "PS2 ARCHIVE"))

# ---------------------------------------------------------------------------
# 12-15. Settings: remember_share ordering, dedupe, cap - and the file on
# disk never carries a password.
# ---------------------------------------------------------------------------
real_settings_file = gui.SETTINGS_FILE
gui.SETTINGS_FILE = os.path.join(TMP, "settings.json")
try:
    settings = {}
    gui.remember_share(settings, "srv1", "PS2 ARCHIVE", "duane")
    gui.remember_share(settings, "srv2", "GAMES", "")
    recents = gui.recent_shares(settings)
    check("remember_share: most recent first",
          [r["server"] for r in recents] == ["srv2", "srv1"],
          repr(recents))
    gui.remember_share(settings, "SRV1", "ps2 archive", "duane")
    recents = gui.recent_shares(settings)
    check("remember_share: re-remember dedupes (case-insensitive)",
          len(recents) == 2 and recents[0]["server"] == "SRV1",
          repr(recents))
    for i in range(9):
        gui.remember_share(settings, "srv%d" % i, "S%d" % i, "")
    check("remember_share: capped at 8, last_server tracked",
          len(gui.recent_shares(settings)) == 8
          and settings.get("last_server") == "srv8",
          repr(settings.get("last_server")))
    with open(gui.SETTINGS_FILE, "r", encoding="utf-8") as fh:
        on_disk = fh.read()
    loaded = gui.load_settings()
    check("settings persist to disk and reload; no password anywhere",
          loaded.get("last_server") == "srv8"
          and len(gui.recent_shares(loaded)) == 8
          and "password" not in on_disk.lower()
          and set(json.loads(on_disk)["recent_shares"][0].keys())
          == {"server", "share", "username"},
          on_disk[:200])
finally:
    gui.SETTINGS_FILE = real_settings_file
check("recent_shares tolerates junk settings",
      gui.recent_shares({}) == []
      and gui.recent_shares({"recent_shares": "junk"}) == [])

# ---------------------------------------------------------------------------
# 16-18. Mount-path logic against a staged GVFS-shaped mount point (the
# exact POSIX shape the app consumes): an already-mounted share is found
# and mount_share returns it without invoking gio.
# ---------------------------------------------------------------------------
real_xdg = os.environ.get("XDG_RUNTIME_DIR")
xdg = os.path.join(TMP, "xdg-runtime")
mount_dir = os.path.join(
    xdg, "gvfs", "smb-share:server=127.0.0.3,share=PS2TEST")
os.makedirs(mount_dir)
os.environ["XDG_RUNTIME_DIR"] = xdg
try:
    check("find_share_mount finds the staged GVFS mount",
          gui.find_share_mount("127.0.0.3", "PS2TEST") == mount_dir
          and gui.find_share_mount("127.0.0.3", "ps2test") == mount_dir
          and gui.find_share_mount("127.0.0.3", "OTHER") is None,
          repr(gui.list_smb_mounts()))
    path, err = gui.mount_share("127.0.0.3", "PS2TEST")
    check("mount_share: already-mounted share returns its path",
          path == mount_dir and err == "", repr((path, err)))
    check("describe_path labels share paths, not local ones",
          gui.describe_path(os.path.join(mount_dir, "game.7z"))
          == "network share 127.0.0.3/PS2TEST"
          and gui.describe_path("/home/duane/game.iso") == "",
          gui.describe_path(os.path.join(mount_dir, "game.7z")))
finally:
    if real_xdg is None:
        del os.environ["XDG_RUNTIME_DIR"]
    else:
        os.environ["XDG_RUNTIME_DIR"] = real_xdg
path, err = gui.mount_share("", "")
check("mount_share: blank server/share is a plain error",
      path == "" and "required" in err, repr((path, err)))

# ---------------------------------------------------------------------------
# 19-24. The bundled 7zz: located first, really runs, member selection and
# extraction semantics.
# ---------------------------------------------------------------------------
check("find_7z_tool prefers the bundled 7zz",
      tool == os.path.join(gui.SEVENZIP_DIR, "7zz")
      and os.access(tool, os.X_OK), repr(tool))
proc = subprocess.run([tool, "i"], capture_output=True, text=True)
check("bundled 7zz executes (7zz i)",
      proc.returncode == 0 and "7-Zip" in proc.stdout,
      proc.stdout[:80] + proc.stderr[:80])
member = gui._7z_iso_member(tool, sevenz_path)
check("_7z_iso_member finds the nested ISO, skips the decoy",
      member == "games/deep/Fixture Game.iso", repr(member))
check("_7z_iso_member: no ISO inside -> None",
      gui._7z_iso_member(tool, noiso_7z) is None)
try:
    gui._7z_iso_member(tool, garbage_7z)
    check("_7z_iso_member: garbage archive -> ProbeError", False,
          "no raise")
except gui.ProbeError:
    check("_7z_iso_member: garbage archive -> ProbeError", True)

before = sha256_of_file(sevenz_path)
extract_dir = os.path.join(TMP, "extract")
out = gui.extract_iso_from_7z(sevenz_path, extract_dir)
check("extract_iso_from_7z: flattened ISO, bytes intact, source untouched",
      out == os.path.join(extract_dir, "Fixture Game.iso")
      and open(out, "rb").read() == iso_bytes
      and sha256_of_file(sevenz_path) == before
      and os.path.dirname(out) == extract_dir, repr(out))
try:
    gui.extract_iso_from_7z(noiso_7z, os.path.join(TMP, "extract2"))
    check("extract_iso_from_7z: no ISO -> ProbeError", False, "no raise")
except gui.ProbeError:
    check("extract_iso_from_7z: no ISO -> ProbeError", True)

# ---------------------------------------------------------------------------
# 25-29. probe_image: the .7z end-to-end, plus .zip / .iso regressions.
# ---------------------------------------------------------------------------
probe = gui.probe_image(sevenz_path, os.path.join(TMP, "probe-7z"))
check("probe_image(.7z): boot + serial from the ISO inside",
      probe["boot_file"] == "SLUS_999.99"
      and probe["serial"] == "SLUS-99999"
      and os.path.isfile(probe["iso_path"])
      and probe["iso_path"].startswith(os.path.join(TMP, "probe-7z")),
      repr(probe))
probe = gui.probe_image(iso, os.path.join(TMP, "probe-iso"))
check("probe_image(.iso) regression: source used in place",
      probe["boot_file"] == "SLUS_999.99"
      and probe["iso_path"] == iso, repr(probe))
probe = gui.probe_image(zip_path, os.path.join(TMP, "probe-zip"))
check("probe_image(.zip) regression: ISO unpacked to probe dir",
      probe["boot_file"] == "SLUS_999.99"
      and probe["serial"] == "SLUS-99999"
      and os.path.isfile(probe["iso_path"])
      and probe["iso_path"].startswith(os.path.join(TMP, "probe-zip")),
      repr(probe))
for name, bad in (("garbage .7z", garbage_7z),
                  ("missing file", os.path.join(TMP, "nope.7z")),
                  ("wrong extension", os.path.join(TMP, "x.bin"))):
    with open(os.path.join(TMP, "x.bin"), "wb") as fh:
        fh.write(b"x")
    try:
        gui.probe_image(bad, os.path.join(TMP, "probe-bad"))
        check("probe_image rejects %s" % name, False, "no raise")
    except gui.ProbeError:
        check("probe_image rejects %s" % name, True)

# ---------------------------------------------------------------------------
# 30-35. Staging semantics through the real App: _probe_and_add stages the
# extracted ISO under work/_archive_stage/; Add commits it into
# work/<slug>/<stem>.iso and the queue record names that local file;
# closing the dialog without adding deletes the staged copy.
# ---------------------------------------------------------------------------
if gui.tk is None or not os.environ.get("DISPLAY"):
    check("GUI staging drive (skipped: no display - run under xvfb-run)",
          True, "skipped")
else:
    errors = []
    gui.messagebox = types.SimpleNamespace(
        showerror=lambda *a, **k: errors.append(a),
        showinfo=lambda *a, **k: None,
        askyesno=lambda *a, **k: True)
    # The app copy's work/queue state starts empty by construction.
    for stale in (gui.WORK_DIR, gui.QUEUE_FILE):
        if os.path.isdir(stale):
            shutil.rmtree(stale)
        elif os.path.isfile(stale):
            os.remove(stale)
    root = gui.tk.Tk()
    app = gui.App(root)

    def pump_until(pred, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            root.update()
            if pred():
                return True
            time.sleep(0.05)
        return False

    def add_dialog():
        for w in root.winfo_children():
            if isinstance(w, gui.tk.Toplevel) \
                    and w.title() == "Add game":
                return w
        return None

    def staged_isos():
        found = []
        stage_root = os.path.join(gui.WORK_DIR, "_archive_stage")
        for base, _dirs, files in os.walk(stage_root):
            found += [os.path.join(base, f) for f in files
                      if f == "disc.iso"]
        return found

    app._probe_and_add(sevenz_path)
    ok = pump_until(lambda: add_dialog() is not None and staged_isos())
    check("staging: probe of a .7z stages its ISO in local scratch",
          ok and len(staged_isos()) == 1,
          repr(staged_isos()))
    dlg = add_dialog()
    button = None
    stack = list(dlg.winfo_children()) if dlg else []
    while stack:
        w = stack.pop()
        stack += list(w.winfo_children())
        if isinstance(w, gui.ttk.Button) \
                and str(w.cget("text")) == "Add to Queue":
            button = w
    assert button is not None, "Add to Queue button not found"
    button.invoke()
    pump_until(lambda: len(app.games) == 1, timeout=10)
    game = app.games[0] if app.games else {}
    expect_src = os.path.join(gui.WORK_DIR, game.get("slug", "?"),
                              "Fixture Game.iso")
    check("commit: queue record names work/<slug>/<stem>.iso",
          game.get("source_path") == expect_src
          and game.get("serial") == "SLUS-99999",
          repr(game.get("source_path")))
    check("commit: staged ISO moved, bytes intact, stage dir cleaned",
          os.path.isfile(expect_src)
          and open(expect_src, "rb").read() == iso_bytes
          and staged_isos() == [], repr(staged_isos()))
    with open(gui.QUEUE_FILE, "r", encoding="utf-8") as fh:
        persisted = json.load(fh)
    check("commit: queue.json persists the local ISO path, not the .7z",
          len(persisted) == 1
          and persisted[0]["source_path"] == expect_src,
          repr(persisted))

    sevenz2 = os.path.join(TMP, "Second Game.7z")
    shutil.copy(sevenz_path, sevenz2)
    app._probe_and_add(sevenz2)
    ok = pump_until(lambda: add_dialog() is not None and staged_isos())
    dlg = add_dialog()
    check("cancel: second .7z stages again",
          ok and len(staged_isos()) == 1, repr(staged_isos()))
    if dlg is not None:
        cmd = dlg.protocol("WM_DELETE_WINDOW")
        dlg.tk.call(cmd)
    pump_until(lambda: add_dialog() is None, timeout=10)
    check("cancel: closing the dialog deletes the staged ISO, adds nothing",
          len(app.games) == 1 and staged_isos() == [],
          "games=%d staged=%r" % (len(app.games), staged_isos()))
    check("no error dialogs were shown during the drive",
          errors == [], repr(errors))
    root.destroy()

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed: %s" % ", ".join(failures))
sys.exit(1 if failed else 0)
