#!/usr/bin/env python3
"""Already-mounted netshare suite for PS2 Native Recomp Tools.

Adapted from the Resigner GUI's test_netshare.py (the
2026-11-24 already-mounted fix, propagated per
~/workspace/netshare-already-mounted-fix-2026-11-24.md)
to this app's inline API: mount_share() returns
(path, error) instead of raising, and the strict matcher
is find_share_mount().

Driven by a stub `gio`, like the reference test: this VM
has no GVFS/FUSE, so the harness fakes both sides -- a
stub gio on PATH whose behaviour is set by $FAKE_GIO_MODE
(already / prompt / fail) and which answers
`gio mount -l` with $FAKE_GIO_LIST_URI, plus a fake GVFS
mount root under a temp XDG_RUNTIME_DIR.

Runs against a COPY of the app in /tmp/app-test, like the
other suites:

    python3 tests/test_netshare_already_mounted.py

Covered (the same 5 cases as the reference test):

* the 2026-11-24 field bug -- the share is ALREADY
  mounted, but under a different spelling of the server
  (an alias), so find_share_mount()'s strict match misses
  it, `gio mount` answers "Location is already mounted",
  and the old code returned that as a fatal error.
  mount_share must now proceed to the existing mount
  path, resolved (a) via the `gio mount -l` listing and
  (b) via the share-only gvfs scan when the listing is
  empty.
* the credential-prompt fix stays intact: gio asks
  Domain FIRST, then Password, under a header line that
  itself contains the word "password"; WORKGROUP must
  land in the Domain prompt and the password in the
  Password prompt.
* a genuine mount failure still returns a plain error.
* the 2026-11-24 FOLLOW-UP field bug (Duane: "still same
  shit"): his existing mount was created from a
  smb://user@host/share URL, so its gvfs dir name carries
  a trailing user= part -- "smb-share:server=...,share=
  PS2 ARCHIVE,user=billtookahit420". The old mount-name
  regex grabbed share=(.+)$ and swallowed ",user=..."
  into the share name, so NO layer could ever match the
  mount by its real share name. His pasted share line
  also had no username in the URL and a subfolder after
  the share: smb://192.168.0.102/PS2 ARCHIVE/PS2 USA
  Collection/. Covered end-to-end (parse -> mount ->
  subpath) plus via the gio-list and share-scan routes.
* the 2026-11-24 REAL-GVFS repro (node 2, glib 2.88 /
  gvfs 1.60, real server): every mount path passed,
  including the real dialog driven end-to-end -- but the
  real gvfs dir name lowercases + percent-encodes the
  share ("share=ps2%20archive"), a shape the stub suites
  had only used in the raw typed case. Covered below.
  The same repro found the field failure left NO trace
  (empty log pane, no readable error), so mount_share /
  _gio_mount now emit a secret-free step trace into the
  app log; covered below.
"""
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = "/tmp/app-test"
spec = importlib.util.spec_from_file_location(
    "gui", os.path.join(APP_DIR, "ps2_native_gui.py"))
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)

FAKE_GIO = r"""#!/bin/bash
if [ "$1" = "mount" ] && [ "$2" = "-l" ]; then
  if [ -n "$FAKE_GIO_LIST_URI" ]; then
    echo "Mount(0): share on server -> $FAKE_GIO_LIST_URI"
    echo "  Type: GProxyMount (GProxyVolumeMonitor)"
  fi
  exit 0
fi
if [ "$1" = "mount" ]; then
  url="$2"
  case "$FAKE_GIO_MODE" in
    already)
      echo "gio: $url: Location is already mounted" >&2
      exit 1 ;;
    prompt)
      echo "Enter password for share “PS2 ARCHIVE” on 192.168.0.102:"
      printf "Domain: "
      read -r a1
      printf "Password: "
      read -r a2
      printf "%s\n%s\n" "$a1" "$a2" > "$FAKE_GIO_ANSWERS"
      mkdir -p "$XDG_RUNTIME_DIR/gvfs/$FAKE_GIO_MOUNTNAME"
      exit 0 ;;
    fail)
      echo "gio: $url: Connection refused" >&2
      exit 1 ;;
  esac
fi
exit 2
"""

SERVER = "192.168.0.102"
ALIAS = "fileserver"
SHARE = "PS2 ARCHIVE"
CANON_MOUNT = "smb-share:server=%s,share=ps2 archive" % SERVER
ALIAS_MOUNT = "smb-share:server=%s,share=ps2 archive" % ALIAS
# Mounts created with credentials (smb://user@host/share,
# e.g. by a file manager) carry a trailing user= part in
# the gvfs dir name.
USER_MOUNT = ("smb-share:server=%s,share=PS2 ARCHIVE,"
              "user=billtookahit420" % SERVER)
USER_ALIAS_MOUNT = ("smb-share:server=%s,share=PS2 ARCHIVE,"
                    "user=billtookahit420" % ALIAS)


class AlreadyMountedHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(
            prefix="ps2gui-alreadymounted-"))
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        gio = self.bindir / "gio"
        gio.write_text(FAKE_GIO)
        gio.chmod(gio.stat().st_mode | stat.S_IXUSR)
        self.runtime = self.tmp / "runtime"
        self.gvfs = self.runtime / "gvfs"
        self.gvfs.mkdir(parents=True)
        self.answers = self.tmp / "answers.txt"
        self._saved_env = dict(os.environ)
        os.environ["PATH"] = "%s:%s" % (
            self.bindir, self._saved_env.get("PATH", ""))
        os.environ["XDG_RUNTIME_DIR"] = str(self.runtime)
        os.environ["FAKE_GIO_ANSWERS"] = str(self.answers)
        os.environ["FAKE_GIO_MOUNTNAME"] = CANON_MOUNT

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def mode(self, mode, list_uri=""):
        os.environ["FAKE_GIO_MODE"] = mode
        os.environ["FAKE_GIO_LIST_URI"] = list_uri

    def add_mount(self, name):
        path = self.gvfs / name
        path.mkdir()
        return str(path)

    # -- the field bug ------------------------------------
    def test_already_mounted_resolved_via_gio_list(self):
        want = self.add_mount(ALIAS_MOUNT)
        self.mode("already",
                  "smb://billtookahit420@%s/PS2%%20ARCHIVE/"
                  % ALIAS)
        # strict match must miss (alias server), proving
        # the browse really goes through gio + the resolver
        self.assertIsNone(gui.find_share_mount(SERVER, SHARE))
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)
        self.assertTrue(Path(path).is_dir())

    def test_already_mounted_resolved_via_share_scan(self):
        want = self.add_mount(ALIAS_MOUNT)
        self.mode("already", "")
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)

    def test_already_mounted_canonical_still_fast_path(self):
        want = self.add_mount(CANON_MOUNT)
        self.mode("already", "")
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)

    # -- the follow-up field bug: user= in the mount name --
    def test_user_mount_parses_to_real_share(self):
        want = self.add_mount(USER_MOUNT)
        mounts = gui.list_smb_mounts()
        self.assertEqual(len(mounts), 1)
        # NOT "PS2 ARCHIVE,user=billtookahit420"
        self.assertEqual(mounts[0]["share"], SHARE)
        self.assertEqual(mounts[0]["server"], SERVER)
        self.assertEqual(mounts[0]["path"], want)
        self.assertEqual(gui.find_share_mount(SERVER, SHARE),
                         want)

    def test_user_mount_duane_exact_case(self):
        # His exact pasted line: no username in the URL, a
        # subfolder after the share; his existing mount
        # carries user=; gio lists the user-carrying URI.
        want = self.add_mount(USER_MOUNT)
        sub = Path(want) / "PS2 USA Collection"
        sub.mkdir()
        self.mode("already",
                  "smb://billtookahit420@%s/ps2%%20archive/"
                  % SERVER)
        ref = gui.parse_share_ref(
            "smb://192.168.0.102/PS2 ARCHIVE/"
            "PS2 USA Collection/")
        self.assertEqual(ref, {"server": SERVER, "share": SHARE,
                               "subpath": "PS2 USA Collection"})
        path, err = gui.mount_share(ref["server"], ref["share"],
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)
        # the dialog joins the subpath onto the mount path
        cand = os.path.join(path, ref["subpath"])
        self.assertTrue(os.path.isdir(cand))
        self.assertEqual(cand, str(sub))

    def test_user_mount_resolved_via_gio_list(self):
        want = self.add_mount(USER_ALIAS_MOUNT)
        self.mode("already",
                  "smb://billtookahit420@%s/ps2%%20archive/"
                  % ALIAS)
        self.assertIsNone(gui.find_share_mount(SERVER, SHARE))
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)

    def test_user_mount_resolved_via_share_scan(self):
        want = self.add_mount(USER_ALIAS_MOUNT)
        self.mode("already", "")
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)

    # -- credential prompts (regression) ------------------
    def test_domain_then_password_prompts(self):
        self.mode("prompt")
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "s3cret")
        self.assertEqual(err, "")
        self.assertEqual(path, str(self.gvfs / CANON_MOUNT))
        answers = self.answers.read_text().splitlines()
        self.assertEqual(answers, ["WORKGROUP", "s3cret"])

    # -- genuine failure still fails ----------------------
    def test_real_failure_returns_error(self):
        self.mode("fail")
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(path, "")
        self.assertIn("could not mount", err)
        self.assertIn("Connection refused", err)

    # -- the connect trace (2026-11-24 real-GVFS follow-up) --
    def test_mount_trace_logged_without_secrets(self):
        # The node-2 real-GVFS repro (glib 2.88 / gvfs 1.60)
        # passed every mount path, yet Duane's rig still
        # failed with NO readable error and an empty log.
        # mount_share/_gio_mount now trace the attempt into
        # the app log: prompts by KIND, outcomes, resolved
        # paths - never the PASSWORD value (the username
        # rides in the mount URL, as it always has in the
        # app's own error text).
        self.mode("prompt")
        lines = []
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "s3cret",
                                    log=lines.append)
        self.assertEqual(err, "")
        self.assertTrue(path)
        joined = "\n".join(lines)
        self.assertIn("gio mount", joined)
        self.assertIn("asked for workgroup", joined)
        self.assertIn("asked for password", joined)
        self.assertIn("mounted at", joined)
        self.assertNotIn("s3cret", joined)

    def test_failure_trace_logged_without_secrets(self):
        self.mode("fail")
        lines = []
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "s3cret",
                                    log=lines.append)
        self.assertEqual(path, "")
        self.assertIn("could not mount", err)
        joined = "\n".join(lines)
        self.assertIn("gio mount", joined)
        self.assertIn("exited", joined)
        self.assertNotIn("s3cret", joined)
        self.assertNotIn("s3cret", err)

    def test_real_mount_dir_shape_lowercase_encoded(self):
        # The REAL gvfs dir name observed against the real
        # server on node 2 (glib 2.88 / gvfs 1.60): the
        # share is LOWERCASED and percent-encoded in the
        # spec - "ps2 archive" arrives as ps2%20archive.
        # The parser must unquote it back to the typed name.
        want = self.add_mount(
            "smb-share:server=%s,share=ps2%%20archive,"
            "user=billtookahit420" % SERVER)
        mounts = gui.list_smb_mounts()
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["share"], "ps2 archive")
        self.assertEqual(gui.find_share_mount(SERVER, SHARE),
                         want)
        self.mode("already", "")
        path, err = gui.mount_share(SERVER, SHARE,
                                    "billtookahit420", "pw")
        self.assertEqual(err, "")
        self.assertEqual(path, want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
