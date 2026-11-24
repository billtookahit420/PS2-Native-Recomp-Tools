#!/usr/bin/env python3
"""Reconstructed hardening suite for PS2 Native Recomp Tools.

The original /tmp/test_hardening.py (16 checks) did not survive; this is
an equivalent rebuild per BUILD-NOTES.md: restart recovery, the
streaming runner's deadline/cancel group kills, and per-title
isolation (hang / crash / good) through run_conversion itself, using
fake tool trees whose port.sh hangs, crashes, or succeeds.
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time

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


TMP = tempfile.mkdtemp(prefix="ps2gui-hard-")
gui.WORK_DIR = os.path.join(TMP, "work")
os.makedirs(gui.WORK_DIR)
COMP = gui.detect_compiler()
ENV = dict(os.environ)

# 1-5. Restart recovery resets exactly the in-flight statuses.
games = []
for i, st in enumerate([gui.STATUS_CONVERTING, gui.STATUS_VERIFYING,
                        gui.STATUS_QUEUED, gui.STATUS_PACKAGED,
                        gui.STATUS_FAILED, gui.STATUS_NEEDS_REVIEW]):
    g = gui.new_game_record("G%d" % i, "g%d" % i, "/x.iso",
                            {"boot_file": "SLUS_999.99",
                             "serial": "SLUS-99999"}, "", "")
    g["status"] = st
    games.append(g)
n = gui.recover_queue(games)
check("recovery count is exactly the 2 in-flight titles", n == 2, str(n))
check("Converting reset to Queued with reason",
      games[0]["status"] == gui.STATUS_QUEUED
      and "recovered" in games[0]["reason"])
check("Verifying reset to Queued with reason",
      games[1]["status"] == gui.STATUS_QUEUED
      and "recovered" in games[1]["reason"])
check("Queued and Packaged untouched",
      games[2]["status"] == gui.STATUS_QUEUED
      and games[2]["reason"] == ""
      and games[3]["status"] == gui.STATUS_PACKAGED)
check("Failed and Needs Review untouched",
      games[4]["status"] == gui.STATUS_FAILED
      and games[5]["status"] == gui.STATUS_NEEDS_REVIEW)

# 6-7. Deadline: run_streaming returns -16 promptly.
t0 = time.monotonic()
rc = gui.run_streaming(["sleep", "60"], cwd=TMP, env=ENV,
                       log_cb=lambda line: None,
                       deadline=t0 + 1)
elapsed = time.monotonic() - t0
check("deadline returns -16", rc == -16, str(rc))
check("deadline kill is prompt (<10 s for a 60 s sleep)",
      elapsed < 10, "%.1f s" % elapsed)

# 8-9. Cancel: run_streaming returns -15 promptly.
cancel = threading.Event()
timer = threading.Timer(0.3, cancel.set)
t0 = time.monotonic()
timer.start()
rc = gui.run_streaming(["sleep", "60"], cwd=TMP, env=ENV,
                       log_cb=lambda line: None, cancel_event=cancel)
elapsed = time.monotonic() - t0
timer.cancel()
check("cancel returns -15", rc == -15, str(rc))
check("cancel kill is prompt (<10 s)", elapsed < 10, "%.1f s" % elapsed)


# 10-16. Per-title isolation through run_conversion with fake tools.
def seed_tool(slug, port_body):
    tool = os.path.join(gui.WORK_DIR, slug, "tool")
    os.makedirs(os.path.join(tool, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(tool, "build", "tools"), exist_ok=True)
    for name in ("eerecomp", "vurecomp"):
        stub = os.path.join(tool, "build", "tools", name)
        with open(stub, "w") as fh:
            fh.write("")
        os.chmod(stub, 0o755)
    port = os.path.join(tool, "scripts", "port.sh")
    with open(port, "w") as fh:
        fh.write(port_body)
    os.chmod(port, 0o755)


def fake_game(slug):
    return gui.new_game_record(slug, slug, "/x.iso",
                               {"boot_file": "SLUS_999.99",
                                "serial": "SLUS-99999"}, "", "")


pidfile = os.path.join(TMP, "hang.pid")
os.environ["FAKE_PIDFILE"] = pidfile
seed_tool("hangtitle",
          "#!/bin/bash\necho $$ > \"$FAKE_PIDFILE\"\nexec sleep 600\n")
seed_tool("crashtitle",
          "#!/bin/bash\necho '== [1/6] disc'\n"
          "echo '== [2/6] executable'\nexit 3\n")
seed_tool("goodtitle",
          "#!/bin/bash\nslug=\"$1\"\n"
          "mkdir -p \"games/$slug/shots\"\n"
          "python3 -c \"import sys; sys.stdout.buffer.write("
          "b'P6\\n2 2\\n255\\n' + bytes(12))\" "
          "> \"games/$slug/shots/frame_00000.ppm\"\n"
          "cp \"games/$slug/shots/frame_00000.ppm\" "
          "\"games/$slug/shots/frame_00300.ppm\"\n"
          "echo '== [6/6] builds'\nexit 0\n")

results = {}
t0 = time.monotonic()
for slug, timeout in (("hangtitle", 2), ("crashtitle", 60),
                      ("goodtitle", 60)):
    game = fake_game(slug)
    try:
        results[slug] = gui.run_conversion(
            game, COMP, log_cb=lambda line: None,
            timeout_seconds=timeout)
    except Exception as exc:  # the pile must never see this
        results[slug] = (False, "unexpected error: %r" % exc)
    results[slug + "_frames"] = len(game["frames"])
total = time.monotonic() - t0

ok, reason = results["hangtitle"]
check("hanging title fails with a timed-out reason",
      not ok and "timed out" in reason, reason)
pid_dead = False
try:
    with open(pidfile) as fh:
        pid = int(fh.read().strip())
    os.kill(pid, 0)
except (OSError, ValueError):
    pid_dead = True
check("hanging title's process group is dead", pid_dead)
ok, reason = results["crashtitle"]
check("crashing title reports the step it died on",
      not ok and "== [2/6] executable" in reason, reason)
ok, _reason = results["goodtitle"]
check("good title converts after both failures", ok is True,
      repr(results["goodtitle"]))
check("good title collected milestone frames",
      results["goodtitle_frames"] >= 1,
      str(results["goodtitle_frames"]))
check("whole pile finished promptly (<60 s)", total < 60,
      "%.1f s" % total)

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed: %s" % ", ".join(failures))
sys.exit(1 if failed else 0)
