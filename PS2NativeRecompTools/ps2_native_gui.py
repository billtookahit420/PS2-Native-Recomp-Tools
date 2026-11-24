#!/usr/bin/env python3
"""
PS2 Native Recomp Tools - a queue GUI for exoteric33's ps2-native-recomp.

Wraps the workflow the fleet proved by hand on 2026-11-19 (Ghostbusters,
Code Veronica X): queue a disc image you own, convert it with the pinned
upstream tool, review the captured frames and record an honest verdict,
then package only what the verdict proves.

Upstream: https://github.com/exoteric33/ps2-native-recomp (MIT), built on
ps2-web-recomp, pinned at commit 9b0eef16ecd5df327eb23eb5adaf844251c2f29f
and shipped in tools/ next to this file.

Stdlib only. The pipeline functions in this module are GUI-free and can be
driven headlessly (import the module and call them); the Tk UI at the
bottom is a shell over them.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
import zlib

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
TOOLS_SRC = os.path.join(APP_DIR, "tools", "ps2-native-recomp")
QUEUE_FILE = os.path.join(APP_DIR, "queue.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
WORK_DIR = os.path.join(APP_DIR, "work")
OUTPUT_DIR = os.path.join(APP_DIR, "output")
SHIM_SRC = os.path.join(APP_DIR, "glibc-shim.c")
SEVENZIP_DIR = os.path.join(APP_DIR, "tools", "7z")

UPSTREAM_PIN = "9b0eef16ecd5df327eb23eb5adaf844251c2f29f"
UPSTREAM_PIN_SHORT = "9b0eef1"
DEFAULT_FRAMES = 10800

GLIBC_FLOOR = (2, 39)
GLIBCXX_FLOOR = (3, 4, 31)
# Symbols the bundled glibc-shim.c can provide (fleet-proven on GB + CVX).
SHIM_SYMBOLS = {"asinf", "acosf", "atan2f", "sinhf", "coshf"}

STATUS_QUEUED = "Queued"
STATUS_CONVERTING = "Converting"
STATUS_VERIFYING = "Verifying"
STATUS_NEEDS_REVIEW = "Needs Review"
STATUS_PACKAGED = "Packaged"
STATUS_FAILED = "Failed"

STALL_STAGES = ["No boot", "Logos only", "Title screen", "Menu reached",
                "Other (note below)"]

CONTROLS_TEXT = """\
- D-pad: arrow keys | Left stick: WASD | Right stick: IJKL
- Cross: Space or Enter | Circle: E
- Square: left mouse button | Triangle: right mouse button
- L1: Shift | R1: F | L2: Q | R2: R | L3: X | R3: C
- Start: Esc | Select: Tab
- F5 quick save | F8 quick load | F11 fullscreen | F2 picture style
- Any game controller SDL knows should work too.
- PS2_SCALE=2 ./Play.sh doubles the resolution on a strong GPU."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _version_tuple(text):
    return tuple(int(p) for p in text.split("."))


def file_crc32(path):
    """Standard CRC-32 of a whole file, as %08X (the redump-style value)."""
    crc = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1 << 22)
            if not block:
                break
            crc = zlib.crc32(block, crc)
    return "%08X" % (crc & 0xFFFFFFFF)


def _fmt_duration(seconds):
    if not seconds:
        return "no time limit"
    seconds = int(seconds)
    if seconds % 3600 == 0:
        return "%d h" % (seconds // 3600)
    if seconds >= 3600:
        return "%.1f h" % (seconds / 3600.0)
    if seconds % 60 == 0:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(1 << 22)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def normalize_mtimes(root, when=None):
    """Stamp every file and directory under root with mtime `when`
    (default: now).

    The app ships inside a zip whose entries carry the build machine's
    local timestamps; extracted in another timezone, files can land
    future-dated, and future-dated build inputs are one way a build
    can be sent chasing its own tail. Stamping a tree with the local
    now removes that variable. (Field evidence later showed the
    setup-loop failure had a different cause - see BUILD-NOTES.md -
    but the guard is harmless and stays.) Symlinks are stamped, never
    followed, so nothing outside the tree is touched.
    """
    if when is None:
        when = time.time()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                os.utime(os.path.join(dirpath, name), (when, when),
                         follow_symlinks=False)
            except OSError:
                pass
        try:
            os.utime(dirpath, (when, when))
        except OSError:
            pass


def _tree_has_future_mtime(root, now=None):
    """True if any file under root is dated after `now` (default: now)."""
    if now is None:
        now = time.time()
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                if os.path.getmtime(os.path.join(dirpath, name)) > now:
                    return True
            except OSError:
                continue
    return False


def normalize_bundled_tools_if_needed():
    """First-launch guard for the shipped tools/ tree: if the zip
    extraction left any of its files future-dated, stamp the tree back
    to now so every per-game copy starts from clean timestamps."""
    if os.path.isdir(TOOLS_SRC) and _tree_has_future_mtime(TOOLS_SRC):
        normalize_mtimes(TOOLS_SRC)


def derive_slug(source_name, serial=""):
    """Suggest a port.sh-safe slug from an image file name.

    Takes the part before the first ' - ' or '(' so 'Tekken 4 (USA).zip'
    becomes 'tekken4' and 'Ghostbusters - The Video Game (USA).zip'
    becomes 'ghostbusters'. Falls back to the serial.
    """
    stem = os.path.splitext(os.path.basename(source_name))[0]
    head = re.split(r"\s+-\s+|\s*\(", stem)[0]
    slug = re.sub(r"[^a-z0-9]+", "", head.lower())
    if not slug:
        slug = re.sub(r"[^a-z0-9]+", "", stem.lower())
    if not slug and serial:
        slug = re.sub(r"[^a-z0-9]+", "", serial.lower())
    return slug or "game"


def serial_display(boot_file):
    """SLUS_218.82 -> SLUS-21882 (the form printed on the box)."""
    if "_" in boot_file:
        prefix, rest = boot_file.split("_", 1)
        return "%s-%s" % (prefix, rest.replace(".", ""))
    return boot_file


def parse_boot2(cnf_text):
    """Extract the boot program name from SYSTEM.CNF text.

    Mirrors scripts/port.sh: the BOOT2 value without 'cdrom0:', without
    leading slashes, without the ';1' version suffix.
    """
    for line in cnf_text.replace("\r", "\n").split("\n"):
        if line.strip().upper().startswith("BOOT2"):
            if "=" not in line:
                continue
            value = line.split("=", 1)[1].strip().strip('"')
            if value.lower().startswith("cdrom0:"):
                value = value[len("cdrom0:"):]
            value = value.lstrip("\\/")
            value = value.split(";")[0].strip()
            return value
    return ""


# --------------------------------------------------------------------------
# Compiler detection
# --------------------------------------------------------------------------

def detect_compiler():
    """Find a usable clang++.

    Order: $PS2NATIVE_CXX, $CXX, clang++ on PATH, versioned clang++-NN.
    Returns dict(path, version_str, major) or None. The tool needs clang
    17+ (its C++ uses <format> and clang vector builtins; GCC cannot
    build it).
    """
    candidates = []
    for env_name in ("PS2NATIVE_CXX", "CXX"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(value)
    on_path = shutil.which("clang++")
    if on_path:
        candidates.append(on_path)
    for major in range(30, 9, -1):
        found = shutil.which("clang++-%d" % major)
        if found:
            candidates.append(found)
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        try:
            out = subprocess.run([cand, "--version"], capture_output=True,
                                 text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            continue
        match = re.search(r"clang version (\d+)\.(\d+)\.(\d+)", out.stdout)
        if match:
            return {"path": cand,
                    "version": "%s.%s.%s" % match.groups(),
                    "major": int(match.group(1))}
    return None


def compiler_ok(comp):
    return comp is not None and comp["major"] >= 17


# --------------------------------------------------------------------------
# Disc probing (uses the shipped tool's own scripts/disc.py)
# --------------------------------------------------------------------------

class ProbeError(Exception):
    pass


def _run_quiet(cmd, timeout=1200):
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().split("\n")
        raise ProbeError(tail[-1] if tail else
                         "command failed: %s" % " ".join(cmd[:3]))
    return proc


def find_7z_tool():
    """Locate a 7-Zip command-line extractor, or None.

    The package bundles the official standalone 7zz under tools/7z/
    (next to the bundled upstream tool, with its license), so the
    packaged app needs nothing installed for .7z input; a system
    7z/7zz/7za on PATH is the fallback (and the route on Windows,
    where no binary is bundled)."""
    names = ("7zz.exe", "7z.exe") if IS_WINDOWS else ("7zz", "7z")
    for name in names:
        cand = os.path.join(SEVENZIP_DIR, name)
        if os.path.isfile(cand) and (IS_WINDOWS
                                     or os.access(cand, os.X_OK)):
            return cand
    for name in ("7z", "7zz", "7za", "7zr"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _7z_iso_member(tool, archive_path):
    """The first .iso member inside a .7z archive, or None."""
    proc = subprocess.run([tool, "l", "-slt", "--", archive_path],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise ProbeError("7-Zip could not read %s: %s"
                         % (os.path.basename(archive_path),
                            " ".join(proc.stderr.split())[:200]))
    blocks = []
    current = {}
    for line in proc.stdout.splitlines() + [""]:
        if " = " in line:
            key, value = line.split(" = ", 1)
            current[key.strip()] = value.strip()
        elif current:
            blocks.append(current)
            current = {}
    # The first block describes the archive itself, not a member.
    for block in blocks[1:]:
        member = block.get("Path", "")
        if block.get("Folder") != "+" \
                and member.lower().endswith(".iso"):
            return member
    return None


def extract_iso_from_7z(archive_path, dest_dir):
    """Extract the .iso inside a .7z archive into dest_dir.

    dest_dir is always LOCAL scratch (the probe folder): the source
    archive is only ever read - never written, never extracted back
    onto the share or folder it came from. Returns the extracted
    ISO's path. Raises ProbeError with a plain reason."""
    os.makedirs(dest_dir, exist_ok=True)
    tool = find_7z_tool()
    if tool is not None:
        member = _7z_iso_member(tool, archive_path)
        if member is None:
            raise ProbeError("no .iso file inside %s"
                             % os.path.basename(archive_path))
        proc = subprocess.run([tool, "e", "-y", "-o" + dest_dir, "--",
                               archive_path, member],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise ProbeError("7-Zip could not extract %s from %s: %s"
                             % (member, os.path.basename(archive_path),
                                " ".join(proc.stderr.split())[:200]))
        out = os.path.join(dest_dir, os.path.basename(member))
        if not os.path.isfile(out):
            raise ProbeError("7-Zip reported success but %s is missing"
                             % member)
        return out
    try:
        import py7zr
    except ImportError:
        raise ProbeError(
            "this .7z archive needs the 7-Zip tool, and none was "
            "found - the package bundles one under tools/7z, so the "
            "app folder may be incomplete; on Windows, install 7-Zip")
    with py7zr.SevenZipFile(archive_path, "r") as zf:
        names = [n for n in zf.getnames()
                 if n.lower().endswith(".iso")]
        if not names:
            raise ProbeError("no .iso file inside %s"
                             % os.path.basename(archive_path))
        zf.extract(path=dest_dir, targets=names[:1])
    out = os.path.join(dest_dir, names[0])
    if not os.path.isfile(out):
        raise ProbeError("py7zr reported success but %s is missing"
                         % names[0])
    return out


def probe_image(source_path, probe_dir):
    """Read boot file + serial from a .zip, .7z or .iso the user owns.

    Archives are unpacked into probe_dir (local scratch) to get the
    plain ISO; the source archive itself is never modified. Returns
    dict(boot_file, serial, iso_path) where iso_path is the plain
    ISO to CRC and, for a zip, to hand to port.sh's staging. Raises
    ProbeError with a plain reason when the image is unusable.
    """
    disc_py = os.path.join(TOOLS_SRC, "scripts", "disc.py")
    if not os.path.isfile(disc_py):
        raise ProbeError("the bundled tool is missing scripts/disc.py")
    if not os.path.isfile(source_path):
        raise ProbeError("file not found: %s" % source_path)
    os.makedirs(probe_dir, exist_ok=True)
    lower = source_path.lower()
    if lower.endswith(".zip"):
        iso_path = os.path.join(probe_dir, "disc.iso")
        _run_quiet([sys.executable, "-I", disc_py, "iso",
                    source_path, iso_path])
    elif lower.endswith(".7z"):
        iso_path = extract_iso_from_7z(source_path, probe_dir)
    elif lower.endswith(".iso"):
        iso_path = source_path
    else:
        raise ProbeError("pick a .zip, .7z or .iso disc image")
    disc_out = os.path.join(probe_dir, "disc")
    _run_quiet([sys.executable, "-I", disc_py, "extract",
                iso_path, disc_out, "SYSTEM.CNF"])
    cnf_path = os.path.join(disc_out, "SYSTEM.CNF")
    if not os.path.isfile(cnf_path):
        raise ProbeError("no SYSTEM.CNF on the disc - not a PS2 game disc?")
    with open(cnf_path, "rb") as fh:
        cnf_text = fh.read().decode("utf-8", "replace")
    boot_file = parse_boot2(cnf_text)
    if not boot_file:
        raise ProbeError("SYSTEM.CNF names no BOOT2 program")
    if re.search(r"[*?\[\]/\\]", boot_file):
        raise ProbeError("SYSTEM.CNF names an implausible boot program: "
                         "%s" % boot_file)
    _run_quiet([sys.executable, "-I", disc_py, "extract",
                iso_path, disc_out, boot_file])
    if not os.path.isfile(os.path.join(disc_out, boot_file)):
        raise ProbeError("boot program %s not found on the disc"
                         % boot_file)
    return {"boot_file": boot_file,
            "serial": serial_display(boot_file),
            "iso_path": iso_path}


def elf_crc(elf_path):
    """The PCSX2-style game CRC of the boot ELF, via the tool's pnach.py.

    This is the value the fleet recorded per title (Ghostbusters
    7030A01C, CVX 24036809); port.sh prints it as the pnach suffix.
    """
    pnach_py = os.path.join(TOOLS_SRC, "scripts", "pnach.py")
    proc = subprocess.run([sys.executable, "-I", pnach_py, "crc", elf_path],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return ""
    value = proc.stdout.strip().split("\n")[-1].strip()
    return value.upper() if re.fullmatch(r"[0-9A-Fa-f]{8}", value) else ""


# --------------------------------------------------------------------------
# Queue store
# --------------------------------------------------------------------------

def load_queue():
    if os.path.isfile(QUEUE_FILE):
        try:
            with open(QUEUE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return data
        except (OSError, ValueError):
            pass
    return []


def save_queue(games):
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(games, fh, indent=2)
    os.replace(tmp, QUEUE_FILE)


def recover_queue(games):
    """Reset titles that were in flight when the app last closed.

    A game persisted as Converting/Verifying was interrupted mid-run
    (crash, power loss, the app was closed). Its work tree may hold a
    partial build, but its queue state must not stay stuck: it goes
    back to Queued so the next Convert run picks it up again. Returns
    how many games were recovered.
    """
    recovered = 0
    for game in games:
        if game.get("status") in (STATUS_CONVERTING, STATUS_VERIFYING):
            game["status"] = STATUS_QUEUED
            game["reason"] = ("recovered after an app restart - it was "
                              "interrupted mid-conversion")
            recovered += 1
    return recovered


def new_game_record(title, slug, source_path, probe, disc_crc, game_crc):
    return {
        "title": title,
        "slug": slug,
        "source_path": source_path,
        "boot_file": probe["boot_file"],
        "serial": probe["serial"],
        "disc_crc": disc_crc,      # CRC-32 of the ISO image
        "game_crc": game_crc,      # PCSX2-style CRC of the boot ELF
        "status": STATUS_QUEUED,
        "reason": "",
        "frames": [],              # milestone frame paths (PNG)
        "verdict": None,           # {reach, stall_stage, stall_note, saving}
        "package": None,           # {zip, size, sha256, shim}
        "vu_note": "",
        "upstream": UPSTREAM_PIN,
    }


def game_workdir(game):
    return os.path.join(WORK_DIR, game["slug"])


def game_tool_dir(game):
    return os.path.join(game_workdir(game), "tool")


# --------------------------------------------------------------------------
# Network shares
#
# Duane's whole PS2 library lives on his home server (SMB shares), so
# the Add picker must reach network shares, not just local disks. Same
# mechanism as the rest of the converter family:
#
# - Windows: shares are plain UNC paths (\\SERVER\share\...). The
#   native file dialog browses them and every path the pipeline
#   touches is passed as a normal path argument, so a UNC path flows
#   through unchanged. mount_share() just builds and validates the
#   UNC root.
# - Linux: a share becomes a real path by mounting it with GVFS
#   (gio mount smb://server/share), which lands it under
#   $XDG_RUNTIME_DIR/gvfs/smb-share:server=...,share=... - a plain
#   POSIX directory the pipeline can read like any local folder. The
#   app mounts the share itself, then points the ordinary picker at
#   the mounted path; it never relies on the desktop file dialog
#   happening to show network places.
#
# Passwords are used for the mount attempt only - they are never
# stored. Settings remember server, share and username, never the
# password. All of this is GUI-free and headless-testable, like the
# rest of the pipeline.
# --------------------------------------------------------------------------

IS_WINDOWS = sys.platform.startswith("win")


def load_settings():
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def save_settings(settings):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, SETTINGS_FILE)


def recent_shares(settings):
    recents = settings.get("recent_shares")
    return recents if isinstance(recents, list) else []


def remember_share(settings, server, share, username=""):
    """Record a successfully connected share (most recent first,
    capped at 8). Stores server/share/username only - never a
    password."""
    entry = {"server": server, "share": share,
             "username": username or ""}
    recents = [e for e in recent_shares(settings)
               if not (str(e.get("server", "")).lower() == server.lower()
                       and str(e.get("share", "")).lower()
                       == share.lower())]
    recents.insert(0, entry)
    settings["recent_shares"] = recents[:8]
    settings["last_server"] = server
    save_settings(settings)


def parse_share_ref(text):
    """Parse a share reference into {server, share, subpath} or None.

    Accepts smb://server/share[/sub/path], \\\\server\\share[\\sub],
    //server/share and bare server/share[/sub]. A bare server with
    no share is not enough and returns None."""
    s = (text or "").strip()
    if not s:
        return None
    if s.lower().startswith("smb://"):
        s = s[6:]
    s = s.replace("\\", "/").lstrip("/")
    parts = [p for p in s.split("/") if p]
    if len(parts) < 2:
        return None
    server = urllib.parse.unquote(parts[0])
    share = urllib.parse.unquote(parts[1])
    if not server or not share:
        return None
    return {"server": server, "share": share,
            "subpath": "/".join(parts[2:])}


def is_unc_path(p):
    """True for a Windows UNC path (\\\\SERVER\\share or //SERVER/share)."""
    s = str(p).replace("\\", "/")
    return s.startswith("//") and len(s.strip("/")) > 0


def unc_root(server, share):
    """\\\\server\\share for the Windows route."""
    return "\\\\" + server.strip("\\/") + "\\" + share.strip("\\/")


def gvfs_dir():
    """The per-user GVFS mount root, or None when there is none."""
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    cands = []
    if xdg:
        cands.append(os.path.join(xdg, "gvfs"))
    cands.append(os.path.join("/run/user/%d" % os.getuid(), "gvfs"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0] if cands else None


_GVFS_SMB_KEYS = ("server", "share", "user", "domain")


def _parse_gvfs_smb_name(name):
    """{server, share} from a GVFS smb mount directory name,
    or None when the name is not a complete smb-share spec.

    Names look like
        smb-share:server=HOST,share=SHARE[,user=USER][,domain=DOM]
    The trailing user/domain parts are present whenever the
    mount was created with credentials - e.g. by a file
    manager from a smb://user@host/share URL. A 'share=(.+)$'
    grab swallows them into the share name, so the mount
    parses as share 'SHARE,user=USER' and can never be
    matched by its real share name again: every lookup layer
    (find_share_mount, the gio-list retry, the share-only
    scan) reads this parse, so an already-mounted share
    stayed unresolvable and 'Location is already mounted'
    surfaced as a fatal error (Duane's 2026-11-24 follow-up
    field report - his PS2 ARCHIVE mount carries a user=
    part). Split the spec into key=value parts instead; a
    part with no known key continues the previous value (a
    literal comma inside a share name)."""
    if not name.startswith("smb-share:"):
        return None
    fields = {}
    key = None
    for chunk in name[len("smb-share:"):].split(","):
        head, sep, val = chunk.partition("=")
        if sep and head in _GVFS_SMB_KEYS:
            key = head
            fields[key] = val
        elif key is not None:
            fields[key] += "," + chunk
    server = fields.get("server")
    share = fields.get("share")
    if not server or not share:
        return None
    return {"server": urllib.parse.unquote(server),
            "share": urllib.parse.unquote(share)}


def list_smb_mounts():
    """Currently mounted GVFS smb shares: [{server, share, path}]
    with path a real local directory."""
    out = []
    base = gvfs_dir()
    if base is None or not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        m = _parse_gvfs_smb_name(name)
        full = os.path.join(base, name)
        if m and os.path.isdir(full):
            out.append({
                "server": m["server"],
                "share": m["share"],
                "path": full,
            })
    return out


def find_share_mount(server, share):
    """The local path of an already-mounted share, or None."""
    for m in list_smb_mounts():
        if (m["server"].lower() == server.lower()
                and m["share"].lower() == share.lower()):
            return m["path"]
    return None


def smb_url(server, share, username=""):
    user = "%s@" % urllib.parse.quote(username, safe="") if username \
        else ""
    return "smb://%s%s/%s" % (
        user, urllib.parse.quote(server, safe=".-"),
        urllib.parse.quote(share, safe=""))


def gio_available():
    return shutil.which("gio") is not None


def _run_gio_pty(argv, shown, label, username, password, workgroup,
                 timeout=60, log=None):
    """Run a gio command, answering its credential prompts.

    gio asks for credentials on its stdin when a command needs
    them - for SMB the order is "Domain [WORKGROUP]:" first, then
    "Password:", under a header line that itself contains the word
    "password" ("Enter password for share ..."). So the whole
    buffer must NOT be keyword-searched: only the pending prompt -
    the text after the last newline, since prompts are printed
    without one - says what is actually being asked. We drive gio
    through a pty so the prompts behave exactly as they do in a
    terminal, and feed the answers the user typed into the dialog.
    Returns (rc, output).

    `shown` is the command as printed in the trace; `label` is
    its short name ("gio mount", "gio list", ...) used in the
    outcome lines. `log`, when given, is called with one short
    line per step of the attempt (gio's prompts, the answers'
    KINDS - never their values - and the outcome), so a failed
    attempt leaves a readable trace in the app's log instead of
    failing silently behind a dialog."""
    import pty

    def note(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    note(shown)
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, stdin=slave,
                            stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    buf = b""
    heard = []          # prompt kinds already answered, for the trace
    answered = 0
    deadline = time.time() + timeout
    os.set_blocking(master, False)
    while True:
        if proc.poll() is not None:
            try:
                while True:
                    chunk = os.read(master, 4096)
                    if not chunk:
                        break
                    buf += chunk
            except OSError:
                pass
            break
        if time.time() > deadline:
            proc.kill()
            os.close(master)
            note("%s timed out after %ds (answered: %s)"
                 % (label, timeout, ", ".join(heard) or "none"))
            return 124, (buf.decode("utf-8", "replace")
                         + "\n(%s timed out)" % label)
        try:
            chunk = os.read(master, 4096)
            if chunk:
                buf += chunk
        except BlockingIOError:
            time.sleep(0.05)
            continue
        except OSError:
            break
        text = buf.decode("utf-8", "replace")
        pending = text.replace("\r", "\n").rsplit("\n", 1)[-1]
        answer = None
        kind = None
        if "omain" in pending:
            answer = workgroup or "WORKGROUP"
            kind = "workgroup"
        elif "assword" in pending:
            answer = password
            kind = "password"
        elif "sername" in pending or pending.rstrip().endswith("ser:"):
            answer = username
            kind = "username"
        if answer is not None and answered < 8:
            note("gio asked for %s; answering" % kind)
            heard.append(kind)
            os.write(master, (answer + "\n").encode())
            answered += 1
            buf = b""
    rc = proc.wait()
    try:
        os.close(master)
    except OSError:
        pass
    out = buf.decode("utf-8", "replace")
    # gio echoes the typed answers back through the pty; never let
    # an answer (above all the password) ride into a log line or an
    # error message built from this output.
    clean = out
    for secret in (password, username, workgroup):
        if secret:
            clean = clean.replace(secret, "***")
    tail = " ".join(clean.split())
    note("%s exited %d%s"
         % (label, rc, (": " + tail[:200]) if tail else ""))
    return rc, out


def _gio_mount(url, username, password, workgroup, timeout=60,
               log=None):
    """Run `gio mount url`, answering its credential prompts
    (see _run_gio_pty). Returns (rc, output)."""
    return _run_gio_pty(["gio", "mount", url], "gio mount %s" % url,
                        "gio mount", username, password, workgroup,
                        timeout, log)


def _is_already_mounted(out):
    """True when gio's output says the location is already
    mounted. That is a SUCCESS state (the share is already
    available through GVFS), not a failure. gio's wording
    has varied across glib versions ("Location is already
    mounted", "The specified location is already mounted",
    differing case), so match loosely and case-insensitively."""
    return bool(re.search(r"already\s+mounted", out or "",
                          re.IGNORECASE))


def _gio_list_smb_uris():
    """SMB URIs of the mounts GVFS currently knows about,
    parsed from `gio mount -l`. Best-effort: any failure
    (no gio, timeout, odd output) just yields an empty
    list."""
    try:
        proc = subprocess.run(
            ["gio", "mount", "-l"], capture_output=True,
            text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    return re.findall(r"smb://\S+", proc.stdout or "")


def _parse_smb_uri(uri):
    """(host, share) from an smb:// URI, percent-decoded,
    or (None, None) when it does not parse."""
    try:
        parsed = urllib.parse.urlparse(uri)
        share = urllib.parse.unquote(
            parsed.path.strip("/").split("/")[0])
        return parsed.hostname, share or None
    except ValueError:
        return None, None


def _resolve_existing_mount(server, share):
    """Local path of a share GVFS says is already mounted,
    even when find_share_mount()'s strict server+share match
    misses it -- e.g. the existing mount was created under
    a different spelling of the server (an alias or a
    hostname where an address was typed this time, or the
    other way round), so its gvfs directory is named for
    THAT server. Layers, strictest first:

    1. find_share_mount() with the server/share as given.
    2. The mounts gio itself lists (`gio mount -l`): for
       each listed SMB mount whose share matches, retry
       find_share_mount() with the host that mount was really
       created with.
    3. A share-only scan of the gvfs directory: if exactly
       one SMB mount carries this share name, it is the
       mount, whatever server name it was created under."""
    found = find_share_mount(server, share)
    if found:
        return found
    want_share = share.strip().lower()
    for uri in _gio_list_smb_uris():
        host, uri_share = _parse_smb_uri(uri)
        if host and uri_share \
                and uri_share.strip().lower() == want_share:
            found = find_share_mount(host, share)
            if found:
                return found
    matches = [m["path"] for m in list_smb_mounts()
               if m["share"].strip().lower() == want_share]
    if len(matches) == 1:
        return matches[0]
    return None


def mount_share(server, share, username="", password="",
                workgroup="", timeout=60, log=None):
    """Make server/share usable as a local path.

    Returns (path, error): exactly one of them is non-empty. On
    Windows the UNC root itself is the path (no mounting step); on
    Linux the GVFS mount path. A share that is already mounted is
    returned as-is, so reconnecting is free. `log` (optional)
    receives a step-by-step trace of the attempt - prompts and
    outcomes, never credential values - so a failed connect is
    diagnosable from the app's own log pane."""
    def note(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    server, share = (server or "").strip(), (share or "").strip()
    if not server or not share:
        return "", "server and share are both required"
    if IS_WINDOWS:
        root = unc_root(server, share)
        if os.path.isdir(root):
            return root, ""
        return "", ("cannot reach %s - check the server is on and "
                    "the share name is right" % root)
    note("network: connecting to %s/%s" % (server, share))
    existing = find_share_mount(server, share)
    if existing:
        note("network: already mounted at %s" % existing)
        return existing, ""
    note("network: no existing mount found under %s"
         % (gvfs_dir() or "(no GVFS folder)"))
    if not gio_available():
        return "", ("gio is not installed - network mounting needs "
                    "the gvfs packages (gvfs, gvfs-backends)")
    url = smb_url(server, share, username)
    try:
        rc, out = _gio_mount(url, username, password, workgroup,
                             timeout, log=note)
    except Exception as exc:  # pty/gio spawn problems
        return "", "mount failed: %s" % exc
    if _is_already_mounted(out):
        # "Location is already mounted" is success, not
        # failure: the share is already available through
        # GVFS (mounted earlier -- by this app, a file
        # manager, or a previous session). Resolve the
        # existing mount and browse it. find_share_mount()
        # above can miss it when the mount was created under
        # a different spelling of the server, so use the
        # layered resolver rather than the strict match.
        resolved = _resolve_existing_mount(server, share)
        if resolved:
            note("network: gio said already mounted; resolved "
                 "to %s" % resolved)
            return resolved, ""
        note("network: gio said already mounted, but no matching "
             "mount could be resolved")
        # Unresolvable: fall through to the error/poll
        # paths below, unchanged.
    if rc != 0:
        msg = " ".join(out.split()) or "gio mount exited %d" % rc
        for secret in (password, username, workgroup):
            if secret:
                msg = msg.replace(secret, "***")
        return "", "could not mount %s: %s" % (url, msg)
    # gio can take a moment to publish the mount point.
    for _ in range(50):
        path = find_share_mount(server, share)
        if path:
            note("network: mounted at %s" % path)
            return path, ""
        time.sleep(0.2)
    return "", ("gio reports %s mounted, but no mount point appeared "
                "under the GVFS folder" % url)


# ----------------------------------------------------------------------
# Direct share access: the GVFS-mount-free fallback.
#
# mount_share() above makes a share usable by resolving its GVFS
# FUSE mount DIRECTORY to a local path. On Duane's Kubuntu that
# layer is what keeps failing (2026-11-24: three fix rounds, a
# real-GVFS repro on node 2 passing every scenario, the rig still
# dead - his file manager is Dolphin/KIO, which never creates a
# GVFS mount at all; his read: "Probably is the whole gvfs").
# The fallback below never resolves a mount directory. The
# share is mounted with GVFS the usual way (`gio mount` - that
# part demonstrably works on Duane's rig: gio itself answered
# "already mounted"), but instead of looking the mount up as a
# directory under the GVFS folder, GVFS serves the smb:// URI
# directly to `gio list` / `gio copy`. (Verified against the
# real server on node 2: `gio list` does NOT automount - an
# unmounted share answers "The specified location is not
# mounted" - so the gio mount step comes first; what is
# skipped is only the mount-DIRECTORY resolution.) If even
# that fails and an smbclient binary happens to be installed,
# it is the last resort - strictly opportunistic; the app
# never requires it.
# ----------------------------------------------------------------------

def _scrub_secrets(text, password, username, workgroup):
    """text with every credential value replaced by ***."""
    shown = text or ""
    for secret in (password, username, workgroup):
        if secret:
            shown = shown.replace(secret, "***")
    return shown


def smb_child_uri(base_uri, name):
    """base_uri + one path segment, percent-encoded."""
    return (base_uri.rstrip("/") + "/"
            + urllib.parse.quote(name, safe=""))


def smb_uri_for(server, share, username="", subpath=""):
    """The smb:// URI for server/share[/subpath]."""
    uri = smb_url(server, share, username)
    for part in [p for p in (subpath or "").split("/") if p]:
        uri = smb_child_uri(uri, part)
    return uri


def _gio_ensure_uri_mounted(uri, username, password, workgroup,
                            timeout=60, log=None):
    """Establish the GVFS mount behind an smb:// URI with
    `gio mount`, WITHOUT resolving any mount directory.
    "Already mounted" is success - the mount is exactly what
    `gio list` / `gio copy` need. Returns (ok, error)."""
    def note(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    try:
        rc, out = _gio_mount(uri, username, password, workgroup,
                             timeout, log)
    except Exception as exc:  # pty/gio spawn problems
        return False, "mount failed: %s" % exc
    if rc == 0 or _is_already_mounted(out):
        note("network: GVFS mount in place for gio list/copy")
        return True, ""
    msg = _scrub_secrets(" ".join(out.split())
                         or "gio mount exited %d" % rc,
                         password, username, workgroup)
    return False, "could not mount %s: %s" % (uri, msg)


def _gio_list_uri(uri, username, password, workgroup, timeout=60,
                  log=None):
    """List one smb:// URI with `gio list`. Returns
    (entries, error); entries are {"name", "is_dir", "size"}
    in gio's order. No GVFS mount directory is resolved -
    GVFS serves the URI itself (the share must be mounted
    with GVFS first; see _gio_ensure_uri_mounted)."""
    def note(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    if not gio_available():
        return [], ("gio is not installed - network access needs "
                    "the gvfs packages (gvfs, gvfs-backends)")
    try:
        rc, out = _run_gio_pty(
            ["gio", "list", "-a", "standard::type,standard::size",
             uri],
            "gio list %s" % uri, "gio list",
            username, password, workgroup, timeout, log)
    except Exception as exc:  # pty/gio spawn problems
        return [], "gio list failed: %s" % exc
    if rc != 0:
        msg = _scrub_secrets(" ".join(out.split())
                             or "gio list exited %d" % rc,
                             password, username, workgroup)
        return [], "could not list %s: %s" % (uri, msg)
    entries = []
    for line in out.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        name, is_dir, size = line, False, None
        parts = line.rsplit("\t", 2)
        if len(parts) == 3 and parts[2].startswith("("):
            name = parts[0]
            try:
                size = int(parts[1])
            except ValueError:
                size = None
            is_dir = parts[2].strip("() ").lower() == "directory"
        entries.append({"name": name, "is_dir": is_dir,
                        "size": size})
    note("network: gio list served %d entries" % len(entries))
    return entries, ""


def _gio_copy_uri(uri, dest_path, username, password, workgroup,
                  timeout=3600, log=None):
    """Fetch one smb:// file to a LOCAL path with `gio copy`.
    Returns (ok, error). The destination is always local
    scratch - the share is only ever read. The share must be
    mounted with GVFS first (see _gio_ensure_uri_mounted)."""
    if not gio_available():
        return False, ("gio is not installed - network access "
                       "needs the gvfs packages (gvfs, "
                       "gvfs-backends)")
    try:
        rc, out = _run_gio_pty(["gio", "copy", uri, dest_path],
                               "gio copy %s" % uri, "gio copy",
                               username, password, workgroup,
                               timeout, log)
    except Exception as exc:  # pty/gio spawn problems
        return False, "gio copy failed: %s" % exc
    if rc != 0:
        msg = _scrub_secrets(" ".join(out.split())
                             or "gio copy exited %d" % rc,
                             password, username, workgroup)
        return False, "could not fetch %s: %s" % (uri, msg)
    if not os.path.isfile(dest_path):
        return False, ("gio copy reported success but %s is "
                       "missing" % dest_path)
    return True, ""


def smbclient_available():
    return shutil.which("smbclient") is not None


def _smbclient_run(server, share, command, username, password,
                   workgroup, timeout=120):
    """One `smbclient -c` command against //server/share.

    Credentials travel in a 0600 temp auth file - never on
    argv (visible in ps) and never into the returned output,
    which is scrubbed before it can reach a log or an error.
    Returns (rc, output)."""
    auth = None
    try:
        fd, auth = tempfile.mkstemp(prefix="ps2gui-smb-",
                                    suffix=".auth")
        with os.fdopen(fd, "w") as fh:
            fh.write("username = %s\n" % (username or ""))
            fh.write("password = %s\n" % (password or ""))
            if workgroup:
                fh.write("workgroup = %s\n" % workgroup)
        os.chmod(auth, 0o600)
        proc = subprocess.run(
            ["smbclient", "//%s/%s" % (server, share), "-A", auth,
             "-c", command],
            capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, _scrub_secrets(
            out, password, username, workgroup)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "smbclient failed: %s" % exc
    finally:
        if auth:
            try:
                os.remove(auth)
            except OSError:
                pass


def _smbclient_list(server, share, subpath, username, password,
                    workgroup, timeout=120, log=None):
    """List //server/share[/subpath] with smbclient. Returns
    (entries, error), entries shaped like _gio_list_uri's."""
    def note(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    if not smbclient_available():
        return [], "smbclient is not installed"
    command = "ls"
    if subpath:
        command = 'cd "%s"; ls' % subpath.replace('"', "")
    note("smbclient ls //%s/%s%s" % (
        server, share, ("/" + subpath) if subpath else ""))
    rc, out = _smbclient_run(server, share, command, username,
                             password, workgroup, timeout)
    if rc != 0:
        msg = " ".join(out.split()) or "smbclient exited %d" % rc
        return [], ("could not list //%s/%s: %s"
                    % (server, share, msg[:300]))
    entries = []
    for line in out.splitlines():
        m = re.match(r"^\s+(.+?)\s+([A-Z]+)\s+(\d+)\s+\w{3}\s",
                     line)
        if not m:
            continue
        name = m.group(1).strip()
        if name in (".", ".."):
            continue
        entries.append({"name": name,
                        "is_dir": "D" in m.group(2),
                        "size": int(m.group(3))})
    note("network: smbclient list served %d entries" % len(entries))
    return entries, ""


def _smbclient_get(server, share, subpath, name, dest_path,
                   username, password, workgroup, timeout=3600,
                   log=None):
    """Fetch one file with smbclient into a LOCAL path.
    Returns (ok, error)."""
    def note(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    if not smbclient_available():
        return False, "smbclient is not installed"
    if '"' in name:
        return False, "implausible file name: %s" % name
    command = 'get "%s" "%s"' % (name, dest_path)
    if subpath:
        command = 'cd "%s"; %s' % (subpath.replace('"', ""),
                                   command)
    note("smbclient get //%s/%s/%s" % (server, share, name))
    rc, out = _smbclient_run(server, share, command, username,
                             password, workgroup, timeout)
    if rc != 0:
        msg = " ".join(out.split()) or "smbclient exited %d" % rc
        return False, ("could not fetch %s: %s" % (name, msg[:300]))
    if not os.path.isfile(dest_path):
        return False, ("smbclient reported success but %s is "
                       "missing" % dest_path)
    return True, ""


def describe_path(p):
    """Short human label for a path: which share it sits on, if any."""
    s = str(p)
    if is_unc_path(s):
        parts = [x for x in s.replace("\\", "/").split("/") if x]
        if len(parts) >= 2:
            return "network share \\\\%s\\%s" % (parts[0], parts[1])
        return "network share"
    base = gvfs_dir()
    if base is not None and s.startswith(base + os.sep):
        for m in list_smb_mounts():
            if s == m["path"] or s.startswith(m["path"] + os.sep):
                return "network share %s/%s" % (m["server"], m["share"])
    return ""


# --------------------------------------------------------------------------
# Streaming subprocess runner (shared by conversion and packaging steps)
# --------------------------------------------------------------------------

def _kill_group(proc):
    """SIGTERM then SIGKILL the child's whole process group."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait(timeout=10)


def run_streaming(cmd, cwd, env, log_cb, log_file=None, cancel_event=None,
                  deadline=None):
    """Run cmd, streaming merged stdout/stderr line by line to log_cb.

    Returns the exit code, -15 when cancelled, or -16 when the deadline
    (a time.monotonic() timestamp) passes. Cancel and timeout both kill
    the child's whole process group (the tool's own processes only - the
    child is started in its own session), so a hung title can never
    wedge the queue behind it.
    """
    if log_cb is None:
        log_cb = lambda line: None
    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True, bufsize=1,
                            start_new_session=True)
    log_fh = open(log_file, "a", encoding="utf-8") if log_file else None
    try:
        import select
        fd = proc.stdout.fileno()
        buf = ""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _kill_group(proc)
                return -15
            if deadline is not None and time.monotonic() > deadline:
                _kill_group(proc)
                return -16
            ready, _, _ = select.select([fd], [], [], 0.2)
            if ready:
                chunk = os.read(fd, 65536).decode("utf-8", "replace")
                if not chunk:
                    break
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    log_cb(line)
                    if log_fh:
                        log_fh.write(line + "\n")
                        log_fh.flush()
            elif proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    for line in rest.split("\n"):
                        log_cb(line)
                        if log_fh:
                            log_fh.write(line + "\n")
                break
        if buf:
            log_cb(buf)
            if log_fh:
                log_fh.write(buf + "\n")
        return proc.wait(timeout=30)
    finally:
        if log_fh:
            log_fh.close()


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

def copy_tool_tree(dest):
    """Copy the shipped pinned tool into a per-game work tree.

    The .git dir and any build output are left out; the copy is the
    game's own, so one title's failed run can never mix with another's.
    copytree preserves the shipped mtimes, which can be future-dated
    (see normalize_mtimes), so the copy is re-stamped to now before
    anything builds from it.
    """
    def ignore(_dir, names):
        return [n for n in names
                if n in (".git", "build", "games", "generated")]
    shutil.copytree(TOOLS_SRC, dest, ignore=ignore)
    normalize_mtimes(dest)


def settle_build_ninja(build_dir):
    """Stamp a freshly configured build.ninja to the current time.

    On the owner's rig every CMake configure of the tool left the
    generated CMakeFiles/<ver>/CMakeCXXCompiler.cmake a hair newer
    than build.ninja (ninja -d explain showed ~1 ms), so ninja's
    regenerate rule fired, the re-configure reproduced the same
    ordering, and the build died with "manifest 'build.ninja' still
    dirty after 100 tries". That file is a configure output, not a
    source file, so no source timestamp explains it. Stamping
    build.ninja to now - after configure, before ninja - makes the
    manifest the newest file in the tree, so ninja goes straight to
    building. Returns True when a build.ninja was stamped.
    """
    manifest = os.path.join(build_dir, "build.ninja")
    if not os.path.isfile(manifest):
        return False
    now = time.time()
    try:
        os.utime(manifest, (now, now))
    except OSError:
        return False
    return True


def _run_tools_setup(tool, env, log_cb, log_file, cancel_event,
                     deadline):
    """Build the tool's translator binaries (eerecomp + vurecomp).

    Mirrors scripts/setup.sh --native-only step for step - fetch
    wgpu-native if its library is missing, source scripts/cxx.sh with
    cxx_tools=1 so the compiler choice and flags are exactly the
    tool's own (and land in the same build/cxx_flags cache port.sh
    reuses), CMake-configure build/tools, then ninja the two
    targets - except the app drives configure and build as separate
    steps so it can settle the manifest in between (see
    settle_build_ninja), and the configure carries
    -DCMAKE_SUPPRESS_REGENERATION=ON: a cache variable, so the
    generated build.ninja has no regenerate rule to loop on, and the
    suppression also covers port.sh's later plain
    `ninja -C build/tools` refresh, which never re-runs CMake itself.
    Configure and build run under the caller's env, so the CXX the
    app detected is the CXX the configure sees - one compiler choice,
    stable across both steps.

    Returns (rc, step): rc is run_streaming's code (0, a child exit
    code, -15 cancelled, -16 deadline) and step names where it
    stopped on failure.
    """
    tools_build = os.path.join(tool, "build", "tools")
    wgpu_lib = os.path.join(tool, "third_party", "wgpu-native", "lib",
                            "libwgpu_native.a")
    if not os.path.isfile(wgpu_lib):
        rc = run_streaming(["bash", "scripts/fetch_wgpu.sh"], cwd=tool,
                           env=env, log_cb=log_cb, log_file=log_file,
                           cancel_event=cancel_event, deadline=deadline)
        if rc != 0:
            return rc, "fetching wgpu-native"
    configure = (
        'ROOT="$1"; cxx_tools=1; . "$ROOT/scripts/cxx.sh"; '
        'exec cmake -S "$ROOT" -B "$ROOT/build/tools" -G Ninja '
        '-DCMAKE_BUILD_TYPE=Release -DCMAKE_SUPPRESS_REGENERATION=ON '
        '"${cxx_flags[@]}"')
    rc = run_streaming(["bash", "-c", configure, "ps2gui-setup", tool],
                       cwd=tool, env=env, log_cb=log_cb,
                       log_file=log_file, cancel_event=cancel_event,
                       deadline=deadline)
    if rc != 0:
        return rc, "configuring the translator tools"
    settle_build_ninja(tools_build)
    rc = run_streaming(["ninja", "-C", tools_build,
                        "eerecomp", "vurecomp"],
                       cwd=tool, env=env, log_cb=log_cb,
                       log_file=log_file, cancel_event=cancel_event,
                       deadline=deadline)
    if rc != 0:
        return rc, "building the translator tools"
    return 0, ""


def run_conversion(game, comp, log_cb, cancel_event=None,
                   frames=DEFAULT_FRAMES, timeout_seconds=4 * 3600):
    """Run setup (once per work tree) + port.sh for one game.

    Returns (ok, reason). On success the game's frames have been
    collected into game["frames"] by collect_frames(). The whole title
    (setup + conversion) shares one deadline: a title that hangs is
    killed at the deadline and reported failed so the queue moves on.
    timeout_seconds=None disables the deadline.
    """
    workdir = game_workdir(game)
    tool = game_tool_dir(game)
    os.makedirs(workdir, exist_ok=True)
    log_file = os.path.join(workdir, "run.log")
    env = dict(os.environ)
    env["CXX"] = comp["path"]
    deadline = None
    if timeout_seconds:
        deadline = time.monotonic() + timeout_seconds
    timeout_note = "timed out after %s and was killed - see the log" \
        % _fmt_duration(timeout_seconds)
    if not os.path.isdir(tool):
        log_cb("Copying the pinned tool into the work tree...")
        copy_tool_tree(tool)
    eerecomp = os.path.join(tool, "build", "tools", "eerecomp")
    vurecomp = os.path.join(tool, "build", "tools", "vurecomp")
    if not (os.path.exists(eerecomp) and os.path.exists(vurecomp)):
        log_cb("== setup (first game in this work tree)")
        rc, step = _run_tools_setup(tool, env, log_cb, log_file,
                                    cancel_event, deadline)
        if rc == -15:
            return False, "cancelled during setup"
        if rc == -16:
            return False, timeout_note + " (during setup)"
        if rc != 0:
            return False, ("setup failed while %s (exit %d) - see "
                           "the log" % (step, rc))
    log_cb("== port.sh %s --native-only --frames %d"
           % (game["slug"], frames))
    rc = run_streaming(["bash", "scripts/port.sh", game["slug"],
                        game["source_path"], "--native-only",
                        "--frames", str(frames)],
                       cwd=tool, env=env, log_cb=log_cb,
                       log_file=log_file, cancel_event=cancel_event,
                       deadline=deadline)
    if rc == -15:
        return False, "cancelled during conversion"
    if rc == -16:
        return False, timeout_note
    if rc != 0:
        step = _last_step_marker(log_file)
        return False, ("port.sh failed at %s (exit %d) - see the log"
                       % (step, rc))
    # Record any VU interpreter fallback the run mentioned (README caveat).
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            log_text = fh.read()
    except OSError:
        log_text = ""
    for line in log_text.split("\n"):
        low = line.lower()
        if ("vu" in low and ("interpret" in low or "fallback" in low
                             or "falls back" in low)):
            game["vu_note"] = line.strip()
            break
    collect_frames(game)
    if not game["frames"]:
        return False, ("conversion finished but captured no frames - "
                       "nothing to review, treating as failed")
    return True, ""


def _last_step_marker(log_file):
    step = "an unknown step"
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("== ["):
                    step = line.strip()
    except OSError:
        pass
    return step


# --------------------------------------------------------------------------
# Conversion progress (GUI-free): parse the streamed log into an honest
# progress state - pipeline stage, frame counts, liveness.
# --------------------------------------------------------------------------

PROGRESS_STALL_SECONDS = 60

_STEP_RE = re.compile(r"^== \[(\d+)/(\d+)\]\s*(.*?)\s*$")
_NINJA_RE = re.compile(r"^\[(\d+)/(\d+)\]\s")
_ROUND_START_RE = re.compile(r"^\[port\] testing round (\d+) of at most (\d+)")
_FRAME_FILE_RE = re.compile(r"frame_(\d+)\.ppm$")


def _fmt_clock(seconds):
    """Elapsed-time format for the progress readout: 0:07, 12:34, 1:02:03."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def max_frame_in_dir(shots_dir, since=None):
    """Highest frame number among frame_<n>.ppm captures in shots_dir.

    The explore run writes a capture every 300 frames, so the highest
    number present is how far the current run has got. `since` (a
    wall-clock time) restricts the scan to files written at or after
    it, which is what separates this round's captures from a previous
    round's leftovers in the same folder (later rounds overwrite the
    same file names). Returns None when nothing qualifies, or when
    the folder does not exist.
    """
    best = None
    try:
        names = os.listdir(shots_dir)
    except OSError:
        return None
    for name in names:
        match = _FRAME_FILE_RE.match(name)
        if not match:
            continue
        if since is not None:
            try:
                if os.path.getmtime(os.path.join(shots_dir, name)) < since:
                    continue
            except OSError:
                continue
        num = int(match.group(1))
        if best is None or num > best:
            best = num
    return best


class ProgressTracker:
    """Turns one title's streamed log into progress state for the UI.

    Fed line by line on the UI thread, through the same queue the log
    itself travels, and polled by the UI's timer for frame captures
    and stall timing - so it needs no locks. All parsing is
    defensive: an unrecognized line is ignored, never an error. Two
    clocks are injected for testability: `clock` (monotonic; elapsed
    and stall timing) and `wall_clock` (capture file mtimes are wall
    time).

    Honesty rules: the reported fraction is the pipeline stage
    fraction - (step - 1) / steps, refined inside the exploring step
    by the current round's captured frames - and nothing finer is
    claimed. A phase with no measurable signal reports fraction None
    and the UI shows a marquee instead of a stuck percentage. During
    the tools setup, ninja's own [k/n] build lines are the measure;
    ninja lines elsewhere are ignored, because later steps invoke
    ninja repeatedly with unrelated totals.
    """

    def __init__(self, frames_total=DEFAULT_FRAMES, frame_probe=None,
                 clock=time.monotonic, wall_clock=time.time):
        self.frames_total = frames_total or 0
        # callable(since_wall) -> highest frame number or None
        self.frame_probe = frame_probe
        self._clock = clock
        self._wall = wall_clock
        self.active = False
        self.slug = None
        self.title = ""
        self._reset()

    def _reset(self):
        self.phase = "idle"  # idle|prep|setup|port|step|done
        self.step = 0
        self.step_total = 0
        self.step_name = ""
        self.setup_done = 0
        self.setup_total = 0
        self.round = 0
        self.rounds_max = 0
        self.frames_done = 0
        self._round_start_wall = None
        self._started = None
        self._last_activity = None
        self._finished_elapsed = None

    def start(self, slug, title, frames_total=None):
        self._reset()
        self.active = True
        self.slug = slug
        self.title = title
        if frames_total:
            self.frames_total = frames_total
        self._started = self._clock()
        self._last_activity = self._started
        self.phase = "prep"

    def finish(self):
        if not self.active:
            return
        if self._started is not None:
            self._finished_elapsed = self._clock() - self._started
        self.active = False
        self.phase = "done"

    def feed_line(self, line):
        """Record one streamed line. Any line is output: it refreshes
        the liveness timestamp even when it carries no progress."""
        if not self.active:
            return
        self._last_activity = self._clock()
        try:
            self._parse(line)
        except Exception:
            pass  # a weird line must never break the worker or the UI

    def _parse(self, line):
        if line.startswith("== setup"):
            self.phase = "setup"
            return
        if line.startswith("== port.sh"):
            self.phase = "port"
            return
        match = _STEP_RE.match(line)
        if match:
            self.phase = "step"
            self.step = int(match.group(1))
            self.step_total = int(match.group(2)) or self.step_total
            self.step_name = match.group(3)
            return
        match = _ROUND_START_RE.match(line)
        if match:
            self.round = int(match.group(1))
            self.rounds_max = int(match.group(2))
            self.frames_done = 0
            self._round_start_wall = self._wall()
            return
        if self.phase == "setup":
            match = _NINJA_RE.match(line)
            if match:
                self.setup_done = int(match.group(1))
                self.setup_total = int(match.group(2))

    def poll_frames(self):
        """Fold the newest explore capture into the state.

        Called by the UI timer, not from the log path: during an
        explore round the log itself is silent (ps2run's output goes
        to the game's explore.log), and the frame captures are the
        only sign of life. A frame count that advances counts as
        output for the stall indicator, for the same reason.
        """
        if not self.active or self.frame_probe is None:
            return
        try:
            num = self.frame_probe(self._round_start_wall)
        except Exception:
            return
        if num is None:
            return
        if num > self.frames_done:
            self.frames_done = num
            self._last_activity = self._clock()

    def snapshot(self):
        """Current display state. fraction is None when the current
        phase has no measurable progress (the UI shows a marquee)."""
        now = self._clock()
        if self._started is None:
            elapsed = 0
        elif self.active:
            elapsed = now - self._started
        else:
            elapsed = self._finished_elapsed or 0
        idle = 0
        if self._last_activity is not None and self.active:
            idle = max(0.0, now - self._last_activity)
        fraction = None
        stage = ""
        if self.phase == "prep":
            stage = "Preparing the work tree"
        elif self.phase == "setup":
            stage = "Setting up the translator tools"
            if self.setup_total > 0:
                fraction = min(1.0, self.setup_done / self.setup_total)
                stage += " - %d/%d" % (self.setup_done, self.setup_total)
        elif self.phase == "port":
            stage = "Starting the conversion"
        elif self.phase == "step" and self.step_total:
            stage = "Step %d of %d - %s" % (self.step, self.step_total,
                                            self.step_name)
            within = 0.0
            if self.step == 4:  # exploring: refine with captured frames
                if self.round:
                    stage += " - round %d of up to %d" \
                        % (self.round, self.rounds_max)
                if self.frames_total > 0:
                    within = min(1.0,
                                 self.frames_done / self.frames_total)
                if self.frames_done > 0:
                    stage += " - frame %s of %s" \
                        % (format(self.frames_done, ","),
                           format(self.frames_total, ","))
            fraction = min(1.0, (self.step - 1 + within)
                           / self.step_total)
        elif self.phase == "done":
            stage = "Done"
            fraction = 1.0
        return {"active": self.active, "phase": self.phase,
                "stage_text": stage, "fraction": fraction,
                "elapsed": elapsed, "idle": idle,
                "stalled": bool(self.active
                                and idle >= PROGRESS_STALL_SECONDS),
                "step": self.step, "step_total": self.step_total,
                "round": self.round,
                "frames_done": self.frames_done,
                "frames_total": self.frames_total}


# --------------------------------------------------------------------------
# Frames: PPM reading, PNG writing (stdlib), milestone picking
# --------------------------------------------------------------------------

def read_ppm(path):
    """Read a binary PPM (P6). Returns (width, height, rgb_bytes)."""
    with open(path, "rb") as fh:
        data = fh.read()
    tokens = []
    idx = 0
    while len(tokens) < 4:
        while idx < len(data) and data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b"#":
            while idx < len(data) and data[idx:idx + 1] != b"\n":
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx:idx + 1].isspace():
            idx += 1
        tokens.append(data[start:idx])
    idx += 1  # single whitespace after maxval
    if tokens[0] != b"P6":
        raise ValueError("not a P6 PPM: %s" % path)
    width, height, maxval = int(tokens[1]), int(tokens[2]), int(tokens[3])
    raw = data[idx:idx + width * height * 3] if maxval <= 255 \
        else data[idx:idx + width * height * 6]
    if maxval > 255:
        raw = raw[1::2][:width * height * 3]
    return width, height, raw


def write_png(path, width, height, rgb):
    """Minimal PNG encoder (zlib from the stdlib)."""
    def chunk(kind, payload):
        out = len(payload).to_bytes(4, "big") + kind + payload
        out += zlib.crc32(kind + payload).to_bytes(4, "big")
        return out
    stride = width * 3
    scan = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride]
                    for y in range(height))
    ihdr = (width.to_bytes(4, "big") + height.to_bytes(4, "big")
            + bytes([8, 2, 0, 0, 0]))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(scan, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def collect_frames(game, max_milestones=6):
    """Pick up to max_milestones frames across the run's captured set.

    Converts the picks to PNG under work/<slug>/frames/ (Tk reads PNG
    natively; the run's own frames are PPM) and stores them on the game.
    """
    shots = os.path.join(game_tool_dir(game), "games", game["slug"],
                         "shots")
    found = []
    if os.path.isdir(shots):
        for name in os.listdir(shots):
            match = re.fullmatch(r"frame_(\d+)\.ppm", name)
            if match:
                found.append((int(match.group(1)),
                              os.path.join(shots, name)))
    found.sort()
    game["frames"] = []
    if not found:
        return game["frames"]
    if len(found) <= max_milestones:
        picks = found
    else:
        idxs = sorted({round(i * (len(found) - 1) / (max_milestones - 1))
                       for i in range(max_milestones)})
        picks = [found[i] for i in idxs]
    out_dir = os.path.join(game_workdir(game), "frames")
    os.makedirs(out_dir, exist_ok=True)
    for num, src in picks:
        dest = os.path.join(out_dir, "frame_%05d.png" % num)
        try:
            width, height, rgb = read_ppm(src)
            write_png(dest, width, height, rgb)
            game["frames"].append(dest)
        except (OSError, ValueError):
            continue
    return game["frames"]


# --------------------------------------------------------------------------
# glibc shim: scan symbol versions, relink with the bundled shim if needed
# --------------------------------------------------------------------------

def scan_binary_versions(path):
    """Map each versioned dynamic symbol to its required version.

    Returns (max_glibc_tuple, max_glibcxx_tuple, {symbol: (kind, tuple)}).
    """
    proc = subprocess.run(["readelf", "--dyn-syms", "-W", path],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise OSError("readelf failed on %s" % path)
    symbols = {}
    max_glibc = (0,)
    max_glibcxx = (0,)
    for line in proc.stdout.split("\n"):
        match = re.search(r"(\S+?)@(GLIBC|GLIBCXX)_([0-9]+(?:\.[0-9]+)*)"
                          r"(?:\s|\(|$)", line)
        if not match:
            continue
        name, kind, ver = match.group(1), match.group(2), \
            _version_tuple(match.group(3))
        name = name.split("@")[0]
        key = (kind, ver)
        if name not in symbols or ver > symbols[name][1]:
            symbols[name] = key
        if kind == "GLIBC" and ver > max_glibc:
            max_glibc = ver
        if kind == "GLIBCXX" and ver > max_glibcxx:
            max_glibcxx = ver
    return max_glibc, max_glibcxx, symbols


def _fmt_ver(tup):
    return ".".join(str(p) for p in tup)


def attempt_shim(game, comp, log_cb):
    """Bring ps2run's symbol floor down to glibc 2.39 / GLIBCXX 3.4.31.

    Returns (status, detail, binary_path):
      "not-needed" - binary already at or under the floor.
      "applied"    - relinked with the bundled shim and verified: the
                     rescan is under the floor AND a 600-frame headless
                     smoke run renders frame-identical to the original.
      "failed"     - anything else; binary_path is the ORIGINAL binary
                     and the caller must ship it with a loud warning.
    """
    tool = game_tool_dir(game)
    build = os.path.join(tool, "build", "native-" + game["slug"])
    binary = os.path.join(build, "ps2run")
    if not os.path.isfile(binary):
        return "failed", "built ps2run not found in %s" % build, binary
    try:
        max_glibc, max_glibcxx, symbols = scan_binary_versions(binary)
    except OSError as exc:
        return "failed", "could not scan the binary: %s" % exc, binary
    log_cb("Binary floor: GLIBC %s, GLIBCXX %s (target GLIBC %s, "
           "GLIBCXX %s)" % (_fmt_ver(max_glibc), _fmt_ver(max_glibcxx),
                            _fmt_ver(GLIBC_FLOOR),
                            _fmt_ver(GLIBCXX_FLOOR)))
    if max_glibc <= GLIBC_FLOOR and max_glibcxx <= GLIBCXX_FLOOR:
        return "not-needed", "binary already at or under the floor", binary
    over_glibcxx = sorted(s for s, (k, v) in symbols.items()
                          if k == "GLIBCXX" and v > GLIBCXX_FLOOR)
    if over_glibcxx:
        return ("failed",
                "needs GLIBCXX symbols the shim cannot provide: %s"
                % ", ".join(over_glibcxx), binary)
    over_glibc = {s: v for s, (k, v) in symbols.items()
                  if k == "GLIBC" and v > GLIBC_FLOOR}
    uncovered = sorted(set(over_glibc) - SHIM_SYMBOLS)
    if uncovered:
        return ("failed",
                "needs glibc symbols the shim cannot provide: %s"
                % ", ".join(uncovered), binary)
    log_cb("Shim can cover: %s - relinking..."
           % ", ".join(sorted(over_glibc)))
    # Compile the shim.
    shim_obj = os.path.join(build, "glibc-shim.o")
    cc = shutil.which("clang") or comp["path"]
    cmd = [cc, "-O2", "-c", SHIM_SRC, "-o", shim_obj]
    if cc.endswith("++"):
        cmd = [cc, "-x", "c", "-O2", "-c", SHIM_SRC, "-o", shim_obj]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        return ("failed", "shim compile failed: %s"
                % (proc.stderr or "").strip().split("\n")[-1], binary)
    # Recover the build's own link line and re-run it with the shim added.
    proc = subprocess.run(["ninja", "-t", "commands", "ps2run"],
                          cwd=build, capture_output=True, text=True,
                          timeout=60)
    lines = [ln for ln in proc.stdout.split("\n") if " -o " in ln]
    if proc.returncode != 0 or not lines:
        return ("failed", "could not recover the ps2run link command",
                binary)
    link = lines[-1].strip()
    link, nsubs = re.subn(r"-o\s+ps2run(?=\s|$)", "-o ps2run-shimmed",
                          link)
    if nsubs != 1:
        return ("failed", "link command did not name '-o ps2run'", binary)
    link = link.replace("-o ps2run-shimmed",
                        "%s -o ps2run-shimmed" % shim_obj, 1)
    proc = subprocess.run(link, cwd=build, shell=True,
                          capture_output=True, text=True, timeout=900)
    shimmed = os.path.join(build, "ps2run-shimmed")
    if proc.returncode != 0 or not os.path.isfile(shimmed):
        return ("failed", "relink failed: %s"
                % (proc.stderr or "").strip().split("\n")[-1], binary)
    try:
        new_glibc, new_glibcxx, _ = scan_binary_versions(shimmed)
    except OSError as exc:
        return "failed", "could not rescan the shimmed binary: %s" % exc, \
            binary
    if new_glibc > GLIBC_FLOOR or new_glibcxx > GLIBCXX_FLOOR:
        return ("failed",
                "shimmed binary still over the floor (GLIBC %s, "
                "GLIBCXX %s)" % (_fmt_ver(new_glibc),
                                 _fmt_ver(new_glibcxx)), binary)
    log_cb("Relinked floor: GLIBC %s, GLIBCXX %s - running the "
           "frame-identical smoke test..." % (_fmt_ver(new_glibc),
                                              _fmt_ver(new_glibcxx)))
    ok, detail = _smoke_compare(game, binary, shimmed, log_cb)
    if not ok:
        return ("failed",
                "shim smoke test did not pass (%s); shipping the "
                "original binary" % detail, binary)
    return "applied", "relinked with glibc-shim.c; floor GLIBC %s / " \
        "GLIBCXX %s; smoke frames identical" % (_fmt_ver(new_glibc),
                                                _fmt_ver(new_glibcxx)), \
        shimmed


def _smoke_compare(game, orig_binary, shimmed_binary, log_cb):
    """Run both binaries headless for 600 frames; frames must match."""
    tool = game_tool_dir(game)
    slug = game["slug"]
    game_dir = os.path.join(tool, "games", slug)
    elf = os.path.join(game_dir, "disc", game["boot_file"])
    iso = os.path.join(game_dir, "disc.iso")
    smoke_root = os.path.join(game_workdir(game), "smoke")
    results = {}
    for label, binary in (("orig", orig_binary), ("shim", shimmed_binary)):
        out_dir = os.path.join(smoke_root, label)
        frames_dir = os.path.join(out_dir, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        card = os.path.join(out_dir, "card.ps2")
        if not os.path.isfile(card):
            make_memcard(card)
        env = dict(os.environ)
        env["PS2_GPU_LOW_POWER"] = "1"
        cmd = [binary, elf, "--disc", iso, "--memcard", card,
               "--frames", "600", "--frames-dir", frames_dir,
               "--frame-every", "300",
               "--press", "300:start", "--press", "450:cross"]
        proc = subprocess.run(cmd, cwd=tool, env=env,
                              capture_output=True, text=True,
                              timeout=900)
        frames = {}
        for num in (300, 600):
            path = os.path.join(frames_dir, "frame_%05d.ppm" % num)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    frames[num] = fh.read()
        results[label] = frames
        if proc.returncode != 0 and not frames:
            return False, "%s run exited %d with no frames" \
                % (label, proc.returncode)
    for num in (300, 600):
        if num not in results["orig"] or num not in results["shim"]:
            return False, "frame %d missing from a smoke run" % num
        if results["orig"][num] != results["shim"][num]:
            return False, "frame %d differs between binaries" % num
    log_cb("Smoke test: frames 300 and 600 identical between the "
           "original and shimmed binaries.")
    return True, "identical"


# --------------------------------------------------------------------------
# Memory card
# --------------------------------------------------------------------------

def make_memcard(path):
    """Create a pre-formatted, ECC-checked card with the tool's script."""
    mcformat = os.path.join(TOOLS_SRC, "scripts", "mcformat.py")
    if os.path.exists(path):
        os.remove(path)
    proc = subprocess.run([sys.executable, "-I", mcformat, path],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 or not os.path.isfile(path):
        raise OSError("mcformat.py failed: %s"
                      % (proc.stderr or proc.stdout or "").strip())
    proc = subprocess.run([sys.executable, "-I", mcformat, "check", path],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise OSError("mcformat.py check failed on the new card")
    return path


# --------------------------------------------------------------------------
# Package assembly
# --------------------------------------------------------------------------

def generate_play_sh(game):
    """The proven launcher, byte-for-byte the fleet template."""
    slug = game["slug"]
    return """#!/bin/bash
# {title} (PS2, US {serial}) - ps2-native-recomp stopgap build.
# Run this from anywhere; it switches to its own folder first.
cd "$(dirname "$0")"
export PS2_PRESENT_MODE=fifo
# Code the build has not seen yet is translated on the fly, which needs clang++ 17+.
# If one is installed, point the runner at it. The front end and the opening of the
# game run fine without it.
if command -v clang++ >/dev/null 2>&1; then
  export CXX="$(command -v clang++)"
fi
exec ./ps2run games/{slug}/disc/{boot} \\
  --disc games/{slug}/disc.iso \\
  --memcard games/{slug}/memcard.ps2 \\
  --window --game-dir games/{slug} "$@"
""".format(title=game["title"], serial=game["serial"], slug=slug,
           boot=game["boot_file"])


def generate_readme(game, shim_status, shim_detail):
    """README drafted ONLY from the game's recorded facts + verdict."""
    slug = game["slug"]
    verdict = game.get("verdict") or {}
    lines = []
    lines.append("%s (PS2, US %s) - ps2-native-recomp build"
                 % (game["title"], game["serial"]))
    lines.append("=" * 72)
    lines.append("Converted with PS2 Native Recomp Tools using "
                 "ps2-native-recomp by")
    lines.append("exoteric33 (MIT), built on ps2-web-recomp. Upstream "
                 "commit %s." % UPSTREAM_PIN_SHORT)
    lines.append("Disc image: %s" % os.path.basename(game["source_path"]))
    lines.append("Boot program: %s   Disc CRC32: %s   Game CRC: %s"
                 % (game["boot_file"], game["disc_crc"] or "not computed",
                    game["game_crc"] or "not computed"))
    lines.append("")
    lines.append("HOW TO PLAY")
    lines.append("-----------")
    lines.append("1. Unzip anywhere.")
    lines.append("2. Run ./Play.sh from a terminal. The game boots "
                 "straight in - no BIOS")
    lines.append("   needed. A pre-formatted memory card is included at")
    lines.append("   games/%s/memcard.ps2 - your saves live in it. "
                 "Back it up to" % slug)
    lines.append("   keep your progress, and do not delete it.")
    lines.append("")
    lines.append("REQUIREMENTS")
    lines.append("------------")
    lines.append("- Linux x86-64 with an AVX2 CPU")
    lines.append("- SDL2 runtime (libsdl2-2.0-0 - usually already "
                 "installed)")
    lines.append("- A Vulkan driver (your normal NVIDIA/AMD/Intel "
                 "driver is fine)")
    lines.append("- clang++ 17 or newer installed (sudo apt install "
                 "clang): code the")
    lines.append("  conversion's explore run never reached is translated "
                 "on the fly the")
    lines.append("  first time you reach it, which needs the compiler "
                 "on the PLAYING")
    lines.append("  machine. Without it the game can stop at the first "
                 "unreached area.")
    lines.append("  Translation is cached to disk, so each area only "
                 "pays once.")
    if shim_status == "applied":
        lines.append("- The runner was relinked with a small shim so it "
                     "needs nothing")
        lines.append("  newer than glibc 2.39 / GLIBCXX 3.4.31 "
                     "(Ubuntu/Kubuntu 24.04).")
    elif shim_status == "not-needed":
        lines.append("- The runner needs nothing newer than glibc 2.39 "
                     "/ GLIBCXX 3.4.31")
        lines.append("  (Ubuntu/Kubuntu 24.04).")
    else:
        lines.append("")
        lines.append("*** WARNING: the runner in this package was NOT "
                     "relinked to the")
        lines.append("*** usual glibc 2.39 floor (%s)." % shim_detail)
        lines.append("*** It targets the build machine's glibc and may "
                     "refuse to start")
        lines.append("*** on an older system. This is stated plainly "
                     "rather than hidden.")
    lines.append("")
    lines.append("CONTROLS (defaults - F1 in game shows the key card; "
                 "keys can be changed)")
    lines.append("-" * 72)
    lines.append(CONTROLS_TEXT)
    lines.append("")
    lines.append("VERIFIED (recorded by the reviewer from the "
                 "conversion run's frames)")
    lines.append("-" * 72)
    if verdict.get("reach") == "gameplay":
        lines.append("- Reach: GAMEPLAY reached, seen in the run's "
                     "captured frames and")
        lines.append("  confirmed by the reviewer's verdict in PS2 "
                     "Native Recomp Tools.")
    else:
        lines.append("- Reach: no gameplay verdict was recorded for "
                     "this package.")
    if verdict.get("saving") == "proven":
        lines.append("- Saving: save + load PROVEN by the reviewer on "
                     "real hardware.")
    else:
        lines.append("- Saving: UNTESTED. No save was written during "
                     "testing. The game")
        lines.append("  should save to the included pre-formatted card; "
                     "until you have")
        lines.append("  saved and loaded once yourself, treat saving "
                     "as unproven. The")
        lines.append("  runtime also has an F5 quick save.")
    lines.append("")
    lines.append("CAVEATS")
    lines.append("-------")
    lines.append("- Performance on a real GPU was not measured by this "
                 "tool; headless")
    lines.append("  conversion runs do not predict your machine's "
                 "speed.")
    lines.append("- First-visit warm-up: new areas translate on first "
                 "reach (see the")
    lines.append("  clang++ requirement above) and may hitch once; "
                 "later visits are")
    lines.append("  cached and smooth.")
    if game.get("vu_note"):
        lines.append("- VU note from the conversion run: %s"
                     % game["vu_note"])
    if shim_status == "failed":
        lines.append("- The runtime binary could not be shimmed to the "
                     "glibc 2.39 floor;")
        lines.append("  see the WARNING under Requirements.")
    lines.append("")
    lines.append("Files: ps2run (the runner, with the translated game "
                 "code inside),")
    lines.append("games/%s/disc.iso (your disc image), "
                 "games/%s/memcard.ps2 (the save card)."
                 % (slug, slug))
    lines.append("")
    lines.append("ps2-native-recomp by exoteric33 (MIT), built on "
                 "ps2-web-recomp.")
    lines.append("")
    return "\n".join(lines)


def packaging_gate(game, busy):
    """THE packaging gate: one evaluation for the label AND the button.

    Returns (enabled, message). The Package button is enabled exactly
    when this returns enabled=True, and the text beside the button is
    exactly this message, so the two can never disagree (the field bug
    this replaces: the label said 'unlocked' unconditionally while the
    button secretly also required 'not busy').

    The rule itself is the documented one, unchanged: packaging needs
    a recorded 'gameplay' verdict - build_package() enforces the same
    rule and refuses anything else. A saving verdict is NOT required;
    saving defaults to 'untested' in the package README. While the
    app is busy (a conversion run, an add probe, packaging itself)
    the button stays disabled even with a gameplay verdict, and the
    message says that instead of claiming the game is unlocked.
    """
    verdict = game.get("verdict") or {}
    reach = verdict.get("reach")
    if reach == "gameplay":
        if busy:
            return (False,
                    "Verdict recorded: gameplay reached. Packaging "
                    "is unlocked, but the app is busy right now - "
                    "the button frees up when the current job "
                    "finishes.")
        return (True,
                "Verdict recorded: gameplay reached. Packaging "
                "is unlocked.")
    if reach == "stalled":
        return (False,
                "Not packaged: verdict says this game stalled at "
                "'%s'. Stalled games are not packaged."
                % verdict.get("stall_stage", "?"))
    return (False, "Packaging is locked until a verdict is recorded.")


def build_package(game, comp, log_cb):
    """Assemble <slug>-ps2-native/ and zip it into OUTPUT_DIR.

    Hard gate: only a recorded 'gameplay' verdict may package. Returns
    dict(zip, size, sha256, shim) and stores it on the game.
    """
    verdict = game.get("verdict") or {}
    if verdict.get("reach") != "gameplay":
        raise ValueError("packaging is locked: no gameplay verdict "
                         "recorded for this game")
    slug = game["slug"]
    tool = game_tool_dir(game)
    src_games = os.path.join(tool, "games", slug)
    workdir = game_workdir(game)
    shim_status, shim_detail, binary = attempt_shim(game, comp, log_cb)
    log_cb("Shim: %s - %s" % (shim_status, shim_detail))
    pkg_root = os.path.join(workdir, "package")
    pkg_name = "%s-ps2-native" % slug
    pkg_dir = os.path.join(pkg_root, pkg_name)
    if os.path.isdir(pkg_dir):
        shutil.rmtree(pkg_dir)
    dest_games = os.path.join(pkg_dir, "games", slug)
    os.makedirs(dest_games)
    # Runner.
    dest_runner = os.path.join(pkg_dir, "ps2run")
    shutil.copyfile(binary, dest_runner)
    os.chmod(dest_runner, 0o755)
    # Play.sh - the proven template.
    play_path = os.path.join(pkg_dir, "Play.sh")
    with open(play_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(generate_play_sh(game))
    os.chmod(play_path, 0o755)
    # Disc image + boot files.
    src_iso = os.path.join(src_games, "disc.iso")
    if not os.path.isfile(src_iso):
        raise OSError("converted disc.iso not found in %s" % src_games)
    shutil.copyfile(src_iso, os.path.join(dest_games, "disc.iso"))
    src_disc = os.path.join(src_games, "disc")
    shutil.copytree(src_disc, os.path.join(dest_games, "disc"))
    # VU recordings, when the run produced any.
    for vu in ("vu1", "vu0"):
        src_vu = os.path.join(src_games, vu)
        if os.path.isdir(src_vu):
            files = [f for f in os.listdir(src_vu) if f.endswith(".bin")]
            if files:
                dest_vu = os.path.join(dest_games, vu)
                os.makedirs(dest_vu)
                for name in files:
                    shutil.copyfile(os.path.join(src_vu, name),
                                    os.path.join(dest_vu, name))
    # Fresh pre-formatted memory card. Never a .lock file, ever.
    make_memcard(os.path.join(dest_games, "memcard.ps2"))
    for root, _dirs, files in os.walk(pkg_dir):
        for name in files:
            if name.endswith(".lock"):
                raise OSError("a lock file slipped into the package: %s"
                              % os.path.join(root, name))
    # README from the record only.
    with open(os.path.join(pkg_dir, "README.txt"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(generate_readme(game, shim_status, shim_detail))
    # Zip + verify.
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    zip_path = os.path.join(OUTPUT_DIR, "%s.zip" % pkg_name)
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=5) as zf:
        for root, _dirs, files in os.walk(pkg_dir):
            for name in sorted(files):
                full = os.path.join(root, name)
                arc = os.path.relpath(full, pkg_root)
                zf.write(full, arc)
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise OSError("zip verification failed at %s" % bad)
    info = {"zip": zip_path,
            "size": os.path.getsize(zip_path),
            "sha256": sha256_of(zip_path),
            "shim": shim_status}
    game["package"] = info
    log_cb("Package: %s (%d bytes, sha256 %s)"
           % (zip_path, info["size"], info["sha256"]))
    return info


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
    from queue import Queue as _Queue
except ImportError:  # python3-tk not installed
    tk = None


class App:
    def __init__(self, root):
        self.root = root
        root.title("PS2 Native Recomp Tools")
        root.geometry("1180x740")
        self.games = load_queue()
        self.settings = load_settings()
        self.recovered = recover_queue(self.games)
        if self.recovered:
            save_queue(self.games)
        self.comp = detect_compiler()
        self.busy = False
        self.cancel_event = threading.Event()
        self.ui_queue = _Queue()
        self.log_lines = {}       # slug -> [lines]
        self.frame_images = []    # PhotoImage refs (avoid GC)
        self.selected = None      # index into self.games

        # --- compiler / preflight bar -----------------------------------
        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        if compiler_ok(self.comp):
            text = ("Compiler: clang++ %s at %s (17+ required - OK)"
                    % (self.comp["version"], self.comp["path"]))
            ttk.Label(top, text=text).pack(side="left")
        elif self.comp is not None:
            text = ("clang++ %s found at %s, but the tool needs clang++ "
                    "17 or newer.\nConvert is disabled. Install a newer "
                    "one with:  sudo apt install clang"
                    % (self.comp["version"], self.comp["path"]))
            ttk.Label(top, text=text, foreground="red").pack(side="left")
        else:
            text = ("No clang++ found on this machine. The converter "
                    "needs clang++ 17 or\nnewer, and Convert is "
                    "disabled until it is installed:\n"
                    "    sudo apt install clang\n"
                    "Converted games also need clang++ 17+ on the "
                    "machine that PLAYS them,\nfor on-the-fly "
                    "translation of code the conversion never reached.")
            ttk.Label(top, text=text, foreground="red").pack(side="left")
        ttk.Label(top, text="  Frames per explore run:").pack(side="left")
        self.frames_var = tk.StringVar(value=str(DEFAULT_FRAMES))
        ttk.Entry(top, textvariable=self.frames_var, width=7).pack(
            side="left")
        ttk.Label(top, text="  Per-title timeout (hours):").pack(
            side="left")
        self.timeout_var = tk.StringVar(value="4")
        ttk.Entry(top, textvariable=self.timeout_var, width=5).pack(
            side="left")

        # --- main split --------------------------------------------------
        body = ttk.Frame(root, padding=(8, 0, 8, 8))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Queue").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=46, height=24,
                                  exportselection=False)
        self.listbox.pack(fill="y", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=4)
        ttk.Button(btns, text="Add Game...", command=self.add_game)\
            .grid(row=0, column=0, sticky="ew")
        ttk.Button(btns, text="Remove", command=self.remove_game)\
            .grid(row=0, column=1, sticky="ew")
        ttk.Button(btns, text="Redo", command=self.redo_game)\
            .grid(row=1, column=0, sticky="ew")
        self.convert_btn = ttk.Button(
            btns, text="Convert Queue", command=self.convert_queue)
        self.convert_btn.grid(row=1, column=1, sticky="ew")
        self.cancel_btn = ttk.Button(btns, text="Cancel",
                                     command=self.cancel,
                                     state="disabled")
        self.cancel_btn.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(btns, text="Open Log File",
                   command=self.open_log).grid(row=3, column=0,
                                               columnspan=2,
                                               sticky="ew")
        ttk.Button(btns, text="Network Share...",
                   command=self.add_game_network).grid(
                       row=4, column=0, columnspan=2, sticky="ew")
        if not compiler_ok(self.comp):
            self.convert_btn.configure(state="disabled")

        # --- progress readout: queue position, bar, stage, liveness -----
        prog = ttk.Frame(left)
        prog.pack(fill="x", pady=(8, 0))
        self.progress_queue_label = ttk.Label(prog, text="",
                                              wraplength=330)
        self.progress_queue_label.pack(anchor="w")
        self.progress_bar = ttk.Progressbar(prog, mode="indeterminate",
                                            maximum=100)
        self.progress_bar.pack(fill="x", pady=(2, 2))
        self._bar_mode = ""  # neither yet; first tick starts the marquee
        self.progress_stage_label = ttk.Label(prog, text="",
                                              wraplength=330)
        self.progress_stage_label.pack(anchor="w")
        self.progress_stall_label = ttk.Label(prog, text="",
                                              wraplength=330)
        self.progress_stall_label.pack(anchor="w")
        self._font_normal = tkfont.nametofont("TkDefaultFont")
        self._font_bold = self._font_normal.copy()
        self._font_bold.configure(weight="bold")
        self.progress = ProgressTracker()
        self.root.after(500, self._progress_tick)

        self.summary_label = ttk.Label(left, text="", wraplength=330)
        self.summary_label.pack(anchor="w", pady=(6, 0))
        if self.recovered:
            self.summary_label.configure(
                text="Recovered %d title(s) that were interrupted "
                     "mid-conversion; they are Queued again."
                     % self.recovered)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        # Log tab
        log_tab = ttk.Frame(self.notebook)
        self.notebook.add(log_tab, text="Log")
        self.log_text = tk.Text(log_tab, wrap="word", height=10)
        self.log_text.pack(fill="both", expand=True)
        # Review tab
        review = ttk.Frame(self.notebook)
        self.notebook.add(review, text="Frame Review")
        self.frames_frame = ttk.Frame(review)
        self.frames_frame.pack(fill="x", pady=4)
        self.frame_labels = []
        for i in range(6):
            lbl = ttk.Label(self.frames_frame, text="(no frame)",
                            relief="groove", width=38)
            lbl.grid(row=i // 3, column=i % 3, padx=3, pady=3,
                     sticky="nsew")
            self.frame_labels.append(lbl)
        verdict_box = ttk.LabelFrame(review, text="Reviewer verdict "
                                     "(packaging is locked until one "
                                     "is recorded)")
        verdict_box.pack(fill="x", pady=6)
        self.reach_var = tk.StringVar(value="gameplay")
        ttk.Radiobutton(verdict_box, text="Gameplay reached",
                        variable=self.reach_var, value="gameplay")\
            .grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(verdict_box, text="Stalled",
                        variable=self.reach_var, value="stalled")\
            .grid(row=0, column=1, sticky="w")
        ttk.Label(verdict_box, text="Stalled at:").grid(
            row=0, column=2, sticky="e")
        self.stall_stage_var = tk.StringVar(value=STALL_STAGES[2])
        ttk.Combobox(verdict_box, textvariable=self.stall_stage_var,
                     values=STALL_STAGES, state="readonly", width=18)\
            .grid(row=0, column=3, sticky="w")
        ttk.Label(verdict_box, text="Note:").grid(row=1, column=0,
                                                  sticky="e")
        self.stall_note_var = tk.StringVar()
        ttk.Entry(verdict_box, textvariable=self.stall_note_var,
                  width=60).grid(row=1, column=1, columnspan=3,
                                 sticky="w")
        self.saving_var = tk.StringVar(value="untested")
        ttk.Radiobutton(verdict_box, text="Saving untested",
                        variable=self.saving_var, value="untested")\
            .grid(row=2, column=0, sticky="w")
        ttk.Radiobutton(
            verdict_box,
            text="Saving proven (I saved and loaded it myself)",
            variable=self.saving_var, value="proven")\
            .grid(row=2, column=1, columnspan=3, sticky="w")
        actions = ttk.Frame(review)
        actions.pack(fill="x", pady=4)
        ttk.Button(actions, text="Save Verdict",
                   command=self.save_verdict).pack(side="left")
        self.package_btn = ttk.Button(actions, text="Package",
                                      command=self.package_game,
                                      state="disabled")
        self.package_btn.pack(side="left", padx=6)
        self.lock_label = ttk.Label(actions, text="")
        self.lock_label.pack(side="left", padx=6)
        self.pkg_label = ttk.Label(review, text="")
        self.pkg_label.pack(anchor="w")

        self.refresh_list()
        if self.games:
            # Evaluate the gate once at launch (after queue recovery):
            # put the first game on screen with its verdict state -
            # label and Package button set by the same single
            # evaluation as everywhere else - instead of a blank pane.
            self.selected = 0
            self.listbox.selection_set(0)
            self._show_frames(self.games[0])
            self._show_verdict(self.games[0])
        self.root.after(120, self._drain)
        if os.environ.get("PS2GUI_SMOKE") == "1":
            print("UI constructed OK; compiler: %s"
                  % (self.comp if self.comp else "none"))
            self.root.after(1500, self.root.destroy)

    # -- plumbing ---------------------------------------------------------
    def post(self, fn, *args):
        self.ui_queue.put((fn, args))

    def _drain(self):
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except Exception:
            pass
        self.root.after(120, self._drain)

    def persist(self):
        save_queue(self.games)

    def log(self, slug, line):
        self.post(self._log_main, slug, line)

    def _log_main(self, slug, line):
        self.log_lines.setdefault(slug, []).append(line)
        if self.progress.active and slug == self.progress.slug:
            self.progress.feed_line(line)
        if self.selected is not None and \
                self.games[self.selected]["slug"] == slug:
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")

    # -- progress readout ---------------------------------------------------
    def _progress_begin(self, game, index, total, frames):
        shots = os.path.join(game_tool_dir(game), "games",
                             game["slug"], "shots")
        self.progress.frame_probe = \
            lambda since, d=shots: max_frame_in_dir(d, since)
        self.progress.start(game["slug"], game["title"],
                            frames_total=frames)
        self.progress_queue_label.configure(
            text="Title %d of %d - %s" % (index, total, game["title"]))
        self._progress_tick(now=True)

    def _progress_end(self):
        self.progress.finish()
        try:
            self.progress_bar.stop()
        except tk.TclError:
            pass
        self.progress_bar.configure(mode="determinate", value=0)
        self._bar_mode = "determinate"
        self.progress_stage_label.configure(text="")
        self.progress_stall_label.configure(
            text="", foreground="", font=self._font_normal)

    def _progress_tick(self, now=False):
        """Refresh the progress readout from the tracker (UI thread).

        Runs every 500 ms while the app lives; also called directly
        with now=True when a title starts, so the readout paints
        immediately instead of up to half a second later.
        """
        try:
            if self.progress.active:
                self.progress.poll_frames()
                snap = self.progress.snapshot()
                frac = snap["fraction"]
                if frac is None:
                    if self._bar_mode != "indeterminate":
                        self.progress_bar.configure(
                            mode="indeterminate")
                        self._bar_mode = "indeterminate"
                        self.progress_bar.start(50)
                    pct = ""
                else:
                    if self._bar_mode != "determinate":
                        self.progress_bar.stop()
                        self.progress_bar.configure(mode="determinate")
                        self._bar_mode = "determinate"
                    self.progress_bar.configure(
                        value=int(round(frac * 100)))
                    pct = " - %d%%" % int(round(frac * 100))
                self.progress_stage_label.configure(
                    text="%s%s - elapsed %s"
                         % (snap["stage_text"], pct,
                            _fmt_clock(snap["elapsed"])))
                idle = snap["idle"]
                if idle < 2:
                    stall_text = "output just now"
                else:
                    stall_text = "last output %s ago" % _fmt_clock(idle)
                if snap["stalled"]:
                    self.progress_stall_label.configure(
                        text=stall_text + " - nothing for over a "
                             "minute; it may be stuck, or in a stage "
                             "that runs quiet",
                        foreground="red", font=self._font_bold)
                else:
                    self.progress_stall_label.configure(
                        text=stall_text, foreground="",
                        font=self._font_normal)
        except Exception:
            pass  # the readout must never take the app down
        if not now:
            try:
                self.root.after(500, self._progress_tick)
            except Exception:
                pass

    # -- list / selection ---------------------------------------------------
    def refresh_list(self):
        self.listbox.delete(0, "end")
        for game in self.games:
            row = "%s  [%s] - %s" % (game["title"], game["serial"],
                                     game["status"])
            if game["status"] == STATUS_FAILED and game["reason"]:
                row += ": " + game["reason"]
            self.listbox.insert("end", row)
        if self.selected is not None and self.selected < len(self.games):
            self.listbox.selection_set(self.selected)

    def _on_select(self, _event):
        sel = self.listbox.curselection()
        if not sel:
            return
        self.selected = sel[0]
        game = self.games[self.selected]
        slug = game["slug"]
        self.log_text.delete("1.0", "end")
        lines = list(self.log_lines.get(slug, []))
        if not lines:
            run_log = os.path.join(game_workdir(game), "run.log")
            if os.path.isfile(run_log):
                with open(run_log, encoding="utf-8",
                          errors="replace") as fh:
                    lines = fh.read().split("\n")[-400:]
        for line in lines:
            self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self._show_frames(game)
        self._show_verdict(game)

    def _show_frames(self, game):
        self.frame_images = []
        for i, lbl in enumerate(self.frame_labels):
            if i < len(game["frames"]):
                try:
                    img = tk.PhotoImage(file=game["frames"][i])
                    factor = max(1, (img.width() + 299) // 300,
                                 (img.height() + 219) // 220)
                    if factor > 1:
                        img = img.subsample(factor, factor)
                    self.frame_images.append(img)
                    lbl.configure(image=img, text="")
                except tk.TclError:
                    lbl.configure(image="", text="(frame unreadable)")
            else:
                lbl.configure(image="", text="(no frame)")

    def _show_verdict(self, game):
        verdict = game.get("verdict") or {}
        self.reach_var.set(verdict.get("reach", "gameplay"))
        self.stall_stage_var.set(verdict.get("stall_stage",
                                             STALL_STAGES[2]))
        self.stall_note_var.set(verdict.get("stall_note", ""))
        self.saving_var.set(verdict.get("saving", "untested"))
        self._update_lock(game)
        pkg = game.get("package")
        if pkg:
            self.pkg_label.configure(
                text="Packaged: %s (%d bytes)\nsha256 %s - binary shim: "
                     "%s" % (pkg["zip"], pkg["size"], pkg["sha256"],
                             pkg["shim"]))
        else:
            self.pkg_label.configure(text="")

    def _update_lock(self, game):
        # Label and button both come from the ONE gate evaluation in
        # packaging_gate() - they cannot disagree.
        enabled, message = packaging_gate(game, self.busy)
        self.lock_label.configure(text=message)
        self.package_btn.configure(
            state="normal" if enabled else "disabled")

    def _refresh_lock(self):
        """Re-evaluate the packaging gate for whatever is on screen.

        Called whenever an input to the gate changes outside the
        verdict pane itself - above all when a busy period ends,
        because the gate counts 'app busy' and the end of a run must
        free the Package button without waiting for the user to
        re-select the row or re-save the verdict (the second half of
        the field bug: verdict saved mid-run, run finished, button
        stayed greyed forever).
        """
        if self.selected is None or self.selected >= len(self.games):
            self.lock_label.configure(
                text="Packaging is locked until a verdict is "
                     "recorded.")
            self.package_btn.configure(state="disabled")
            return
        self._update_lock(self.games[self.selected])

    # -- queue operations ---------------------------------------------------
    def add_game(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Pick a PS2 disc image you own",
            filetypes=[("Disc images", "*.zip *.7z *.iso"),
                       ("All files", "*.*")])
        if not path:
            return
        self._probe_and_add(path)

    def add_game_network(self):
        """Add a game whose image lives on a network (SMB) share.

        The share is mounted first (GVFS on Linux; a plain UNC path
        on Windows), then the ordinary file picker opens at the
        mounted path and the chosen image flows into the exact same
        probe-and-add path as a local pick - nothing downstream can
        tell the difference. When the mount route fails on Linux,
        a mount-free fallback takes over instead: the share is
        browsed by its smb:// URI (gio list, smbclient as a last
        resort) and the picked image is fetched into local scratch
        first, then probed from there. Server/share/username are
        remembered for next time; the password never is."""
        if self.busy:
            return
        win = tk.Toplevel(self.root)
        win.title("Add game from a network share")
        win.transient(self.root)
        recents = recent_shares(self.settings)

        ttk.Label(win, text="Share:").grid(
            row=0, column=0, sticky="e", padx=4, pady=3)
        ref_var = tk.StringVar()
        if recents:
            ref_var.set("smb://%s/%s" % (recents[0].get("server", ""),
                                          recents[0].get("share", "")))
        elif self.settings.get("last_server"):
            ref_var.set("smb://%s/" % self.settings["last_server"])
        ttk.Entry(win, textvariable=ref_var, width=54).grid(
            row=0, column=1, padx=4, pady=3)
        ttk.Label(win, text="e.g. smb://192.168.0.102/PS2 ARCHIVE, "
                            "\\\\SERVER\\share, or server/share")\
            .grid(row=1, column=1, sticky="w", padx=4)
        ttk.Label(win, text="Username:").grid(
            row=2, column=0, sticky="e", padx=4, pady=3)
        user_var = tk.StringVar(
            value=recents[0].get("username", "") if recents else "")
        ttk.Entry(win, textvariable=user_var, width=54).grid(
            row=2, column=1, padx=4, pady=3)
        ttk.Label(win, text="Password:").grid(
            row=3, column=0, sticky="e", padx=4, pady=3)
        pw_var = tk.StringVar()
        ttk.Entry(win, textvariable=pw_var, width=54, show="*").grid(
            row=3, column=1, padx=4, pady=3)
        ttk.Label(win, text="(used for this mount only - never "
                            "saved)").grid(row=4, column=1, sticky="w",
                                        padx=4)
        ttk.Label(win, text="Workgroup:").grid(
            row=5, column=0, sticky="e", padx=4, pady=3)
        wg_var = tk.StringVar()
        ttk.Entry(win, textvariable=wg_var, width=54).grid(
            row=5, column=1, padx=4, pady=3)
        recent_box = None
        if recents:
            ttk.Label(win, text="Recent:").grid(
                row=6, column=0, sticky="ne", padx=4, pady=3)
            recent_box = tk.Listbox(win, height=min(4, len(recents)),
                                    width=52, exportselection=False)
            for entry in recents:
                recent_box.insert(
                    "end", "%s/%s" % (entry.get("server", ""),
                                      entry.get("share", "")))
            recent_box.grid(row=6, column=1, padx=4, pady=3,
                            sticky="w")

            def pick_recent(_event):
                sel = recent_box.curselection()
                if not sel:
                    return
                entry = recents[sel[0]]
                ref_var.set("smb://%s/%s" % (entry.get("server", ""),
                                             entry.get("share", "")))
                user_var.set(entry.get("username", ""))

            recent_box.bind("<<ListboxSelect>>", pick_recent)
        status = ttk.Label(win, text="")
        status.grid(row=7, column=1, sticky="w", padx=4, pady=2)
        btns = ttk.Frame(win)
        btns.grid(row=8, column=0, columnspan=2, pady=6)
        connect_btn = ttk.Button(btns, text="Connect")
        connect_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel",
                   command=win.destroy).pack(side="left", padx=4)

        def fail(err):
            if not win.winfo_exists():
                return
            status.configure(text="")
            connect_btn.configure(state="normal")
            messagebox.showerror("Network share", err, parent=win)

        def succeed(ref, start):
            remember_share(self.settings, ref["server"], ref["share"],
                           user_var.get().strip())
            win.destroy()
            path = filedialog.askopenfilename(
                title="Pick a PS2 disc image on %s/%s"
                      % (ref["server"], ref["share"]),
                initialdir=start,
                filetypes=[("Disc images", "*.zip *.7z *.iso"),
                           ("All files", "*.*")])
            if path:
                self._probe_and_add(path)

        def browse_fallback(mount_err, ref, user, password,
                            workgroup):
            """The mount-free route, reached only when
            mount_share() failed: browse the share by its smb://
            URI (gio list; smbclient as a last resort when even
            that fails) and fetch the picked image into local
            scratch. No GVFS mount directory is ever resolved."""
            if IS_WINDOWS:
                fail(mount_err)
                return
            if not win.winfo_exists():
                return  # the user cancelled while connecting
            server, share = ref["server"], ref["share"]
            netlog = lambda m: self.post(self.log, "network", m)
            netlog("network: trying direct share access "
                   "(no GVFS mount folder)")
            win.destroy()
            bwin = tk.Toplevel(self.root)
            bwin.title("Browse smb://%s/%s" % (server, share))
            bwin.transient(self.root)
            state = {"backend": "gio", "busy": False,
                     "segments": [p for p in
                                  ref["subpath"].split("/") if p],
                     "entries": []}
            where = ttk.Label(bwin, text="")
            where.grid(row=0, column=0, columnspan=3, sticky="w",
                       padx=4, pady=3)
            box = tk.Listbox(bwin, height=14, width=76,
                             exportselection=False)
            box.grid(row=1, column=0, columnspan=3, padx=4, pady=3)
            bstatus = ttk.Label(bwin, text="")
            bstatus.grid(row=2, column=0, columnspan=3, sticky="w",
                         padx=4, pady=2)
            up_btn = ttk.Button(bwin, text="Up")
            up_btn.grid(row=3, column=0, padx=4, pady=4, sticky="w")
            pick_btn = ttk.Button(bwin, text="Select")
            pick_btn.grid(row=3, column=1, padx=4, pady=4)
            ttk.Button(bwin, text="Cancel",
                       command=bwin.destroy).grid(
                           row=3, column=2, padx=4, pady=4,
                           sticky="e")

            def subpath():
                return "/".join(state["segments"])

            def render(entries):
                state["entries"] = entries
                box.delete(0, "end")
                if state["segments"]:
                    box.insert("end", "..")
                for entry in entries:
                    box.insert("end", entry["name"]
                               + ("/" if entry["is_dir"] else ""))
                where.configure(
                    text="smb://%s/%s%s" % (
                        server, share,
                        ("/" + subpath()) if subpath() else ""))

            def load():
                if state["busy"]:
                    return
                state["busy"] = True
                bstatus.configure(text="Listing ...")
                pick_btn.configure(state="disabled")
                up_btn.configure(state="disabled")

                def work_list():
                    entries, err = [], ""
                    if state["backend"] == "gio":
                        if not state.get("ensured"):
                            ok, err = _gio_ensure_uri_mounted(
                                smb_uri_for(server, share, user,
                                            ""),
                                user, password, workgroup,
                                log=netlog)
                            if ok:
                                state["ensured"] = True
                            else:
                                entries = []
                        if state.get("ensured"):
                            entries, err = _gio_list_uri(
                                smb_uri_for(server, share, user,
                                            subpath()),
                                user, password, workgroup,
                                log=netlog)
                        if err and smbclient_available():
                            netlog("network: gio direct access "
                                   "failed: %s" % err)
                            netlog("network: trying smbclient")
                            alt, alt_err = _smbclient_list(
                                server, share, subpath(), user,
                                password, workgroup, log=netlog)
                            if not alt_err:
                                state["backend"] = "smbclient"
                                entries, err = alt, ""
                            else:
                                err = "%s; smbclient: %s" % (
                                    err, alt_err)
                    else:
                        entries, err = _smbclient_list(
                            server, share, subpath(), user,
                            password, workgroup, log=netlog)

                    def done():
                        state["busy"] = False
                        if not bwin.winfo_exists():
                            return
                        if err:
                            messagebox.showerror(
                                "Network share",
                                "The mount failed, and browsing "
                                "the share directly failed too.\n\n"
                                "Mount: %s\n\nDirect: %s"
                                % (mount_err, err), parent=bwin)
                            bwin.destroy()
                            return
                        pick_btn.configure(state="normal")
                        up_btn.configure(state="normal")
                        render(entries)
                        bstatus.configure(
                            text="Direct access via %s - open a "
                                 "folder, then pick a .zip, .7z "
                                 "or .iso"
                                 % ("gio" if state["backend"]
                                    == "gio" else "smbclient"))

                    self.post(done)

                threading.Thread(target=work_list,
                                 daemon=True).start()

            def fetch(entry):
                name = entry["name"]
                if not name.lower().endswith(
                        (".zip", ".7z", ".iso")):
                    bstatus.configure(
                        text="Pick a .zip, .7z or .iso disc image")
                    return
                state["busy"] = True
                bstatus.configure(text="Fetching %s ..." % name)
                pick_btn.configure(state="disabled")
                up_btn.configure(state="disabled")
                backend = state["backend"]
                sub = subpath()

                def work_fetch():
                    stage_dir = os.path.join(
                        WORK_DIR, "_network_stage",
                        time.strftime("%Y%m%d%H%M%S"))
                    os.makedirs(stage_dir, exist_ok=True)
                    dest = os.path.join(stage_dir, name)
                    if backend == "gio":
                        uri = smb_child_uri(
                            smb_uri_for(server, share, user, sub),
                            name)
                        ok, err = _gio_copy_uri(
                            uri, dest, user, password, workgroup,
                            log=netlog)
                    else:
                        ok, err = _smbclient_get(
                            server, share, sub, name, dest, user,
                            password, workgroup, log=netlog)

                    def done():
                        state["busy"] = False
                        if not bwin.winfo_exists():
                            return
                        if not ok:
                            pick_btn.configure(state="normal")
                            up_btn.configure(state="normal")
                            bstatus.configure(text="")
                            messagebox.showerror(
                                "Network share", err, parent=bwin)
                            return
                        netlog("network: fetched %s via %s "
                               "(%d bytes)"
                               % (name,
                                  "gio copy" if backend == "gio"
                                  else "smbclient",
                                  os.path.getsize(dest)))
                        remember_share(self.settings, server,
                                       share, user)
                        bwin.destroy()
                        self._probe_and_add(dest)

                    self.post(done)

                threading.Thread(target=work_fetch,
                                 daemon=True).start()

            def activate(_event=None):
                if state["busy"]:
                    return
                sel = box.curselection()
                if not sel:
                    return
                index = sel[0]
                if state["segments"]:
                    if index == 0:
                        state["segments"].pop()
                        load()
                        return
                    index -= 1
                if not 0 <= index < len(state["entries"]):
                    return
                entry = state["entries"][index]
                if entry["is_dir"]:
                    state["segments"].append(entry["name"])
                    load()
                else:
                    fetch(entry)

            box.bind("<Double-Button-1>", activate)
            pick_btn.configure(command=activate)

            def go_up():
                if state["segments"] and not state["busy"]:
                    state["segments"].pop()
                    load()

            up_btn.configure(command=go_up)
            load()

        def connect():
            ref = parse_share_ref(ref_var.get())
            if ref is None:
                messagebox.showerror(
                    "Network share",
                    "Enter the share as server/share, "
                    "smb://server/share or \\\\server\\share.",
                    parent=win)
                return
            connect_btn.configure(state="disabled")
            status.configure(text="Connecting to %s/%s ..."
                                  % (ref["server"], ref["share"]))
            user = user_var.get().strip()
            password = pw_var.get()
            workgroup = wg_var.get().strip()

            def work():
                path, err = mount_share(
                    ref["server"], ref["share"], user, password,
                    workgroup,
                    log=lambda m: self.post(
                        self.log, "network", m))
                if err:
                    self.post(self.log, "network",
                              "connect failed: %s" % err)
                    if IS_WINDOWS:
                        self.post(fail, err)
                    else:
                        self.post(browse_fallback, err, ref, user,
                                  password, workgroup)
                    return
                start = path
                if ref["subpath"]:
                    cand = os.path.join(path, ref["subpath"])
                    if os.path.isdir(cand):
                        start = cand
                self.post(succeed, ref, start)

            threading.Thread(target=work, daemon=True).start()

        connect_btn.configure(command=connect)

    def _probe_and_add(self, path):
        self.busy = True
        self.log("app", "Reading %s ..." % path)

        def work():
            probe_dir = os.path.join(
                WORK_DIR, "_probe",
                time.strftime("%Y%m%d%H%M%S"))
            staged_iso = None
            try:
                probe = probe_image(path, probe_dir)
                disc_crc = file_crc32(probe["iso_path"])
                boot_path = os.path.join(probe_dir, "disc",
                                         probe["boot_file"])
                game_crc = elf_crc(boot_path)
                if path.lower().endswith(".7z"):
                    # port.sh cannot read .7z, so the ISO the probe
                    # extracted must outlive the probe folder: stage
                    # it in local scratch; the Add dialog moves it
                    # under work/<slug>/ on commit (or deletes it if
                    # the dialog is closed without adding).
                    stage_dir = os.path.join(
                        WORK_DIR, "_archive_stage",
                        time.strftime("%Y%m%d%H%M%S"))
                    os.makedirs(stage_dir, exist_ok=True)
                    staged_iso = os.path.join(stage_dir, "disc.iso")
                    shutil.move(probe["iso_path"], staged_iso)
                    # A .7z fetched from a share by the direct
                    # (mount-free) network route is our own local
                    # copy in _network_stage; the probe has what
                    # it needs and the queue record will name the
                    # staged ISO, so the fetched archive itself
                    # is redundant. (.iso/.zip fetches stay: the
                    # record names them as the source.) Local
                    # picks are never under _network_stage and
                    # are never touched.
                    stage_root = os.path.abspath(
                        os.path.join(WORK_DIR, "_network_stage"))
                    if os.path.abspath(path).startswith(
                            stage_root + os.sep):
                        try:
                            os.remove(path)
                            os.rmdir(os.path.dirname(
                                os.path.abspath(path)))
                        except OSError:
                            pass
            except (ProbeError, OSError) as exc:
                self.post(messagebox.showerror, "Cannot add game",
                          str(exc))
                self.post(self.log, "app", "Probe failed: %s" % exc)
                self.busy = False
                self.post(self._refresh_lock)
                return
            finally:
                shutil.rmtree(probe_dir, ignore_errors=True)
            self.post(self._add_dialog, path, probe, disc_crc, game_crc,
                      staged_iso)
            self.busy = False
            self.post(self._refresh_lock)

        threading.Thread(target=work, daemon=True).start()

    def _add_dialog(self, path, probe, disc_crc, game_crc,
                    staged_iso=None):
        win = tk.Toplevel(self.root)
        win.title("Add game")
        state = {"committed": False}

        def on_close():
            # A .7z pick staged its extracted ISO in local scratch;
            # if the game is not being added, the staged copy goes.
            if staged_iso and not state["committed"]:
                shutil.rmtree(os.path.dirname(staged_iso),
                              ignore_errors=True)
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        title_var = tk.StringVar(
            value=os.path.splitext(os.path.basename(path))[0])
        slug_var = tk.StringVar(
            value=derive_slug(path, probe["serial"]))
        rows = [("Title:", title_var), ("Slug:", slug_var)]
        for i, (label, var) in enumerate(rows):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky="e",
                                            padx=4, pady=2)
            ttk.Entry(win, textvariable=var, width=46).grid(
                row=i, column=1, padx=4, pady=2)
        info = ("Boot: %s   Serial: %s\nDisc CRC32: %s   Game CRC: %s"
                % (probe["boot_file"], probe["serial"], disc_crc,
                   game_crc or "not computed"))
        ttk.Label(win, text=info).grid(row=2, column=0, columnspan=2,
                                       padx=4, pady=4)

        def commit():
            slug = slug_var.get().strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", slug or ""):
                messagebox.showerror(
                    "Bad slug", "Use letters, digits, . _ - and start "
                    "with a letter or digit.", parent=win)
                return
            if any(g["slug"] == slug for g in self.games):
                messagebox.showerror(
                    "Duplicate", "A game with slug '%s' is already in "
                    "the queue." % slug, parent=win)
                return
            source_path = path
            if staged_iso:
                # The queue record must name a file port.sh can read:
                # the extracted ISO, kept locally under the game's
                # own work folder (it moves aside with Redo, like
                # the rest of the work tree). The .7z itself stays
                # wherever it was picked from, untouched.
                stem = os.path.splitext(os.path.basename(path))[0]
                dest_dir = os.path.join(WORK_DIR, slug)
                os.makedirs(dest_dir, exist_ok=True)
                source_path = os.path.join(dest_dir, stem + ".iso")
                shutil.move(staged_iso, source_path)
                shutil.rmtree(os.path.dirname(staged_iso),
                              ignore_errors=True)
                state["committed"] = True
            game = new_game_record(title_var.get().strip() or slug,
                                   slug, source_path, probe, disc_crc,
                                   game_crc)
            self.games.append(game)
            self.persist()
            self.refresh_list()
            win.destroy()

        ttk.Button(win, text="Add to Queue", command=commit).grid(
            row=3, column=0, columnspan=2, pady=6)

    def remove_game(self):
        if self.selected is None or self.busy:
            return
        game = self.games[self.selected]
        if not messagebox.askyesno(
                "Remove game",
                "Remove '%s' from the queue? Its work folder stays on "
                "disk under work/%s." % (game["title"], game["slug"])):
            return
        del self.games[self.selected]
        self.selected = None
        self.persist()
        self.refresh_list()
        # Nothing is selected now; the gate display must not keep
        # showing the removed game's state.
        self._refresh_lock()

    def redo_game(self):
        if self.selected is None or self.busy:
            return
        game = self.games[self.selected]
        workdir = game_workdir(game)
        if os.path.isdir(workdir):
            aside = "%s.bak-%s" % (workdir,
                                   time.strftime("%Y%m%d%H%M%S"))
            os.rename(workdir, aside)
            self.log(game["slug"],
                     "Previous run moved aside to %s" % aside)
        game.update({"status": STATUS_QUEUED, "reason": "",
                     "frames": [], "verdict": None, "package": None})
        self.persist()
        self.refresh_list()
        self._show_verdict(game)

    def open_log(self):
        if self.selected is None:
            return
        game = self.games[self.selected]
        path = os.path.join(game_workdir(game), "run.log")
        if not os.path.isfile(path):
            messagebox.showinfo("No log yet",
                                "This title has no run log yet - it is "
                                "kept at work/%s/run.log once it runs."
                                % game["slug"])
            return
        subprocess.Popen(["xdg-open", path],
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    # -- conversion ---------------------------------------------------------
    def convert_queue(self):
        if self.busy or not compiler_ok(self.comp):
            return
        try:
            frames = int(self.frames_var.get())
            if frames <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Frames", "Frames must be a positive "
                                 "number.")
            return
        try:
            timeout_seconds = int(float(self.timeout_var.get()) * 3600)
            if timeout_seconds <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Timeout", "The per-title timeout "
                                 "must be a positive number of hours.")
            return
        self.busy = True
        self.cancel_event.clear()
        self.convert_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        threading.Thread(target=self._convert_all,
                         args=(frames, timeout_seconds),
                         daemon=True).start()

    def _convert_all(self, frames, timeout_seconds):
        """Convert every Queued title, one at a time.

        Isolation rule: whatever a single title does - fail, crash the
        tool, hang past its timeout, or raise unexpectedly - it is
        marked Failed with the reason and the queue moves to the next
        title. Nothing a title does can stop the pile.
        """
        attempted = converted = failed = 0
        total = sum(1 for g in self.games
                    if g["status"] == STATUS_QUEUED)
        try:
            for game in self.games:
                if self.cancel_event.is_set():
                    break
                if game["status"] != STATUS_QUEUED:
                    continue
                attempted += 1
                game["status"] = STATUS_CONVERTING
                game["reason"] = ""
                self.post(self.persist)
                self.post(self.refresh_list)
                slug = game["slug"]
                self.post(self._progress_begin, game, attempted, total,
                          frames)
                try:
                    ok, reason = run_conversion(
                        game, self.comp,
                        log_cb=lambda line, s=slug: self.log(s, line),
                        cancel_event=self.cancel_event, frames=frames,
                        timeout_seconds=timeout_seconds)
                except Exception as exc:  # a title must never kill us
                    ok, reason = False, "unexpected error: %s" % exc
                self.post(self._progress_end)
                if ok:
                    converted += 1
                    game["status"] = STATUS_NEEDS_REVIEW
                    self.log(slug, "Conversion done - %d milestone "
                             "frames ready for review."
                             % len(game["frames"]))
                else:
                    failed += 1
                    game["status"] = STATUS_FAILED
                    game["reason"] = reason
                    self.log(slug, "FAILED: %s" % reason)
                self.post(self.persist)
                self.post(self.refresh_list)
                if self.selected is not None and \
                        self.games[self.selected] is game:
                    self.post(self._show_frames, game)
        finally:
            still_queued = sum(1 for g in self.games
                               if g["status"] == STATUS_QUEUED)
            summary = ("Run finished: %d converted (need review), "
                       "%d failed, %d still queued."
                       % (converted, failed, still_queued))
            if self.cancel_event.is_set() and attempted:
                summary += " (cancelled by user)"
            self.log("app", summary)
            self.post(self.summary_label.configure, text=summary)
            self.busy = False
            self.post(self.convert_btn.configure,
                      state="normal" if compiler_ok(self.comp)
                      else "disabled")
            self.post(self.cancel_btn.configure, state="disabled")
            # The gate counts 'busy': a verdict saved mid-run must
            # free its Package button now that the run is over.
            self.post(self._refresh_lock)

    def cancel(self):
        self.cancel_event.set()

    # -- verdict + packaging --------------------------------------------------
    def save_verdict(self):
        if self.selected is None:
            return
        game = self.games[self.selected]
        game["verdict"] = {
            "reach": self.reach_var.get(),
            "stall_stage": self.stall_stage_var.get(),
            "stall_note": self.stall_note_var.get().strip(),
            "saving": self.saving_var.get(),
        }
        if game["status"] == STATUS_PACKAGED and \
                game["verdict"]["reach"] != "gameplay":
            game["status"] = STATUS_NEEDS_REVIEW
            game["package"] = None
        self.persist()
        self.refresh_list()
        self._update_lock(game)
        self.log(game["slug"], "Verdict saved: %s"
                 % game["verdict"]["reach"])

    def package_game(self):
        if self.selected is None or self.busy:
            return
        game = self.games[self.selected]
        self.busy = True
        # Through the one gate evaluation, like everywhere else: the
        # button disables because the gate says 'busy', and the label
        # says so too. Completion posts _show_verdict, which
        # re-evaluates with busy cleared.
        self._update_lock(game)
        slug = game["slug"]

        def work():
            try:
                info = build_package(
                    game, self.comp,
                    log_cb=lambda line: self.log(slug, line))
                game["status"] = STATUS_PACKAGED
                self.log(slug, "Packaged OK: %s" % info["zip"])
            except (ValueError, OSError) as exc:
                self.log(slug, "Packaging failed: %s" % exc)
                self.post(messagebox.showerror, "Packaging failed",
                          str(exc))
            self.busy = False
            self.post(self.persist)
            self.post(self.refresh_list)
            self.post(self._show_verdict, game)

        threading.Thread(target=work, daemon=True).start()


def main():
    if tk is None:
        sys.stderr.write("Tkinter is not installed. Install it with:\n"
                         "  sudo apt install -y python3-tk\n")
        return 1
    normalize_bundled_tools_if_needed()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
