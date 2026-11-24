#!/usr/bin/env python3
"""Reconstructed unit suite for PS2 Native Recomp Tools (v1 + setup fix).

The original /tmp/test_units.py (25 checks) did not survive; this is an
equivalent rebuild per BUILD-NOTES.md, extended with checks for the new
_run_tools_setup / settle_build_ninja code. Runs against a COPY of the
app in /tmp/app-test so the shipped folder stays pristine.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
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


TMP = tempfile.mkdtemp(prefix="ps2gui-units-")

# 1. Play.sh byte-equality against the fleet-proven Ghostbusters file.
gb = {"title": "Ghostbusters: The Video Game", "serial": "SLUS-21882",
      "slug": "ghostbusters", "boot_file": "SLUS_218.82"}
with open("/tmp/ref-Play.sh", "rb") as fh:
    ref_play = fh.read()
mine = gui.generate_play_sh(gb).encode("utf-8")
check("play.sh byte-identical to proven Ghostbusters Play.sh",
      mine == ref_play,
      "first diff at %s" % next(
          (i for i, (a, b) in enumerate(zip(mine, ref_play)) if a != b),
          "len %d vs %d" % (len(mine), len(ref_play))))

# 2. CRC-32 standard check value.
p = os.path.join(TMP, "crc.bin")
with open(p, "wb") as fh:
    fh.write(b"123456789")
check("file_crc32('123456789') == CBF43926",
      gui.file_crc32(p) == "CBF43926", gui.file_crc32(p))

# 3-6. Slug / serial / BOOT2 parsing.
check("derive_slug Tekken 4",
      gui.derive_slug("Tekken 4 (USA).zip") == "tekken4")
check("derive_slug Ghostbusters",
      gui.derive_slug("Ghostbusters - The Video Game (USA).zip")
      == "ghostbusters")
check("serial_display SLUS_218.82",
      gui.serial_display("SLUS_218.82") == "SLUS-21882")
check("parse_boot2",
      gui.parse_boot2("BOOT2 = cdrom0:\\SLUS_218.82;1\r\nVER = 1.00\r\n")
      == "SLUS_218.82")

# 7-8. Compiler gate logic.
comp = gui.detect_compiler()
check("detect_compiler finds clang >= 17 on this machine",
      gui.compiler_ok(comp), repr(comp))
check("compiler_ok rejects None / clang 16",
      not gui.compiler_ok(None)
      and not gui.compiler_ok({"path": "x", "version": "16.0.0",
                               "major": 16})
      and gui.compiler_ok({"path": "x", "version": "18.0.0",
                           "major": 18}))

# 9-10. Packaging gate refuses missing and stalled verdicts.
base = gui.new_game_record("T", "gate", "/nonexistent.iso",
                           {"boot_file": "SLUS_999.99",
                            "serial": "SLUS-99999"}, "", "")
try:
    gui.build_package(dict(base), comp, lambda line: None)
    check("packaging gate refuses missing verdict", False, "no raise")
except ValueError:
    check("packaging gate refuses missing verdict", True)
stalled = dict(base)
stalled["verdict"] = {"reach": "stalled", "stall_stage": "Title screen",
                      "stall_note": "", "saving": "untested"}
try:
    gui.build_package(stalled, comp, lambda line: None)
    check("packaging gate refuses stalled verdict", False, "no raise")
except ValueError:
    check("packaging gate refuses stalled verdict", True)

# 11-12. Memory card: exact size + superblock.
card = os.path.join(TMP, "card.ps2")
gui.make_memcard(card)
with open(card, "rb") as fh:
    head = fh.read(28)
check("memcard is exactly 8,650,752 bytes",
      os.path.getsize(card) == 8650752, str(os.path.getsize(card)))
check("memcard superblock",
      head == b"Sony PS2 Memory Card Format ", repr(head))

# 13. Garbage zip rejected by the probe.
junk = os.path.join(TMP, "junk.zip")
with open(junk, "wb") as fh:
    fh.write(b"this is not a zip at all" * 64)
try:
    gui.probe_image(junk, os.path.join(TMP, "probe-junk"))
    check("probe rejects garbage zip", False, "no raise")
except gui.ProbeError:
    check("probe rejects garbage zip", True)

# 14. Synthetic ISO probe via the tool's own mkiso.py.
boot = os.path.join(TMP, "SLUS_999.99")
with open(boot, "wb") as fh:
    fh.write(b"\x7fELF" + bytes(4096))
iso = os.path.join(TMP, "synth.iso")
mkiso = os.path.join(gui.TOOLS_SRC, "scripts", "mkiso.py")
rc = subprocess.run([sys.executable, "-I", mkiso, iso, boot]).returncode
probe = gui.probe_image(iso, os.path.join(TMP, "probe-synth")) \
    if rc == 0 else None
check("probe of synthetic ISO (boot SLUS_999.99 -> SLUS-99999)",
      probe is not None and probe["boot_file"] == "SLUS_999.99"
      and probe["serial"] == "SLUS-99999", repr(probe))

# 15. README generated only from recorded facts.
game = gui.new_game_record("Ghostbusters: The Video Game",
                           "ghostbusters", "/x/GB.zip",
                           {"boot_file": "SLUS_218.82",
                            "serial": "SLUS-21882"}, "3894BDB7",
                           "7030A01C")
game["verdict"] = {"reach": "gameplay", "stall_stage": "",
                   "stall_note": "", "saving": "untested"}
readme = gui.generate_readme(game, "not-needed", "")
check("README carries verdict, UNTESTED saving, exoteric33 credit",
      "GAMEPLAY reached" in readme and "Saving: UNTESTED" in readme
      and "exoteric33" in readme)
readme_bad = gui.generate_readme(game, "failed", "needs GLIBC_2.43")
check("README warns loudly when shim failed",
      "WARNING" in readme_bad and "NOT" in readme_bad)

# 16. settle_build_ninja stamps an existing manifest, ignores missing.
bd = os.path.join(TMP, "build")
os.makedirs(bd)
manifest = os.path.join(bd, "build.ninja")
with open(manifest, "w") as fh:
    fh.write("# fake\n")
old = time.time() - 5000
os.utime(manifest, (old, old))
ok_stamp = gui.settle_build_ninja(bd)
check("settle_build_ninja stamps manifest to ~now",
      ok_stamp and abs(os.path.getmtime(manifest) - time.time()) < 5)
check("settle_build_ninja returns False when no manifest",
      gui.settle_build_ninja(os.path.join(TMP, "nope")) is False)

# 17. normalize_mtimes clears future-dated files.
tree = os.path.join(TMP, "future")
os.makedirs(tree)
victim = os.path.join(tree, "f.txt")
with open(victim, "w") as fh:
    fh.write("x")
future = time.time() + 3600
os.utime(victim, (future, future))
before = gui._tree_has_future_mtime(tree)
gui.normalize_mtimes(tree)
check("normalize_mtimes clears future mtimes",
      before and not gui._tree_has_future_mtime(tree)
      and os.path.getmtime(victim) <= time.time() + 2)

# 18. copy_tool_tree: clean copy (no .git/build), normalized stamps.
dest = os.path.join(TMP, "toolcopy")
gui.copy_tool_tree(dest)
check("copy_tool_tree copies scripts, leaves .git/build behind",
      os.path.isfile(os.path.join(dest, "scripts", "setup.sh"))
      and not os.path.exists(os.path.join(dest, ".git"))
      and not os.path.exists(os.path.join(dest, "build")))
check("copy_tool_tree copy has no future mtimes",
      not gui._tree_has_future_mtime(dest))

# 19. _last_step_marker reads the last port step from a log.
logp = os.path.join(TMP, "run.log")
with open(logp, "w") as fh:
    fh.write("== [1/6] disc\n== [2/6] executable\ndied\n")
check("_last_step_marker",
      gui._last_step_marker(logp) == "== [2/6] executable",
      gui._last_step_marker(logp))

# 20. new_game_record defaults.
rec = gui.new_game_record("T", "slug", "/x.iso",
                          {"boot_file": "SLUS_999.99",
                           "serial": "SLUS-99999"}, "AAAAAAAA",
                          "BBBBBBBB")
check("new_game_record defaults",
      rec["status"] == gui.STATUS_QUEUED
      and rec["upstream"] == gui.UPSTREAM_PIN
      and rec["verdict"] is None and rec["package"] is None)

# 21. Duration formatting.
check("_fmt_duration",
      gui._fmt_duration(3600) == "1 h"
      and gui._fmt_duration(120) == "2 min"
      and gui._fmt_duration(90) == "90 s"
      and gui._fmt_duration(0) == "no time limit")

# 22. scan_binary_versions runs on a real ELF.
mg, mgx, syms = gui.scan_binary_versions("/usr/bin/python3.12")
check("scan_binary_versions returns sane floors",
      mg >= (2,) and isinstance(syms, dict), repr((mg, mgx)))

# 23. run_streaming happy path streams lines and returns 0.
lines = []
rc = gui.run_streaming(["echo", "hello-stream"], cwd=TMP, env=None
                       if False else dict(os.environ), log_cb=lines.append)
check("run_streaming echo",
      rc == 0 and "hello-stream" in lines, repr((rc, lines)))

# 24-25. Shim mechanics end-to-end on a synthetic GLIBC_2.43 binary.
shim_ok = False
shim_detail = ""
try:
    sdir = os.path.join(TMP, "shim")
    os.makedirs(sdir)
    with open(os.path.join(sdir, "stub.c"), "w") as fh:
        fh.write("float __asinf_new(float x) { return x; }\n"
                 "__asm__(\".symver __asinf_new,asinf@@GLIBC_2.43\");\n")
    with open(os.path.join(sdir, "stub.ver"), "w") as fh:
        fh.write("GLIBC_2.43 { global: asinf; };\n")
    with open(os.path.join(sdir, "main.c"), "w") as fh:
        fh.write("extern float asinf(float);\n"
                 "int main(void) { return (int)asinf(1.0f); }\n")
    subprocess.run(["clang", "-shared", "-o",
                    os.path.join(sdir, "libstub.so"),
                    os.path.join(sdir, "stub.c"),
                    "-Wl,--version-script="
                    + os.path.join(sdir, "stub.ver")], check=True)
    subprocess.run(["clang", "-c", "-o", os.path.join(sdir, "main.o"),
                    os.path.join(sdir, "main.c")], check=True)
    # Fake per-game tool tree: build/native-<slug> with ps2run + build.ninja
    gui.WORK_DIR = os.path.join(TMP, "work")
    sgame = gui.new_game_record("Shim", "shimtest", "/x.iso",
                                {"boot_file": "SLUS_999.99",
                                 "serial": "SLUS-99999"}, "", "")
    build = os.path.join(gui.game_tool_dir(sgame), "build",
                         "native-shimtest")
    os.makedirs(build)
    # -lm mirrors the real ps2run link line: the shim's float wrappers
    # call the double-precision originals from libm.
    link = "clang %s -L%s -lstub -Wl,-rpath,%s -lm -o ps2run" % (
        os.path.join(sdir, "main.o"), sdir, sdir)
    with open(os.path.join(build, "build.ninja"), "w") as fh:
        fh.write("rule link\n  command = %s\n"
                 "build ps2run: link\n" % link)
    subprocess.run(["ninja", "-C", build, "ps2run"], check=True,
                   capture_output=True)
    binary = os.path.join(build, "ps2run")
    mg0, _, _ = gui.scan_binary_versions(binary)
    gui._smoke_compare = lambda g, a, b, cb: (True, "identical")
    status, detail, out_binary = gui.attempt_shim(
        sgame, comp, lambda line: None)
    mg1 = (0,)
    if os.path.isfile(out_binary):
        mg1, _, _ = gui.scan_binary_versions(out_binary)
    shim_ok = (mg0 == (2, 43) and status == "applied"
               and mg1 <= gui.GLIBC_FLOOR
               and out_binary.endswith("ps2run-shimmed"))
    shim_detail = "before=%s status=%s after=%s detail=%s" % (
        mg0, status, mg1, detail)
except Exception as exc:  # noqa: BLE001 - report, don't hide
    shim_detail = "exception: %r" % exc
check("shim: synthetic binary starts at GLIBC 2.43, relink applies",
      shim_ok, shim_detail)
check("shim: shimmed binary rescans at/under the 2.39 floor",
      shim_ok and mg1 <= gui.GLIBC_FLOOR, shim_detail)

# 26. _fmt_clock elapsed formatting.
check("_fmt_clock formats",
      gui._fmt_clock(7) == "0:07" and gui._fmt_clock(754) == "12:34"
      and gui._fmt_clock(3723) == "1:02:03" and gui._fmt_clock(0) == "0:00")

# 27-36. ProgressTracker: stage/frame parsing, stall timing, liveness.
class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


fc = FakeClock()
trk = gui.ProgressTracker(frames_total=10800, clock=fc,
                          wall_clock=time.time)
trk.start("xfiles", "The X-Files")
snap = trk.snapshot()
check("progress: prep phase has no fraction (marquee)",
      snap["phase"] == "prep" and snap["fraction"] is None)
trk.feed_line("== [3/6] machine code to C++")
snap = trk.snapshot()
check("progress: step parse + stage fraction",
      snap["stage_text"] == "Step 3 of 6 - machine code to C++"
      and abs(snap["fraction"] - 2 / 6.0) < 1e-9, repr(snap))
trk.feed_line("random noise [9/9] nonsense")
trk.feed_line("== [x/y] broken marker")
trk.feed_line("")
snap = trk.snapshot()
check("progress: garbage lines ignored, never crash",
      snap["step"] == 3 and abs(snap["fraction"] - 2 / 6.0) < 1e-9)
trk.feed_line("[1/2] Building a unit (ninja inside a step)")
snap = trk.snapshot()
check("progress: ninja lines inside a port step are ignored",
      abs(snap["fraction"] - 2 / 6.0) < 1e-9)

fc2 = FakeClock()
trk2 = gui.ProgressTracker(clock=fc2, wall_clock=time.time)
trk2.start("g", "G")
trk2.feed_line("== setup (first game in this work tree)")
snap = trk2.snapshot()
check("progress: setup is indeterminate until ninja speaks",
      snap["phase"] == "setup" and snap["fraction"] is None)
trk2.feed_line("[42/93] Building CXX object foo.o")
snap = trk2.snapshot()
check("progress: setup fraction from ninja [k/n]",
      abs(snap["fraction"] - 42 / 93.0) < 1e-9
      and "42/93" in snap["stage_text"], repr(snap))

# Explore: captures written after the round starts are the measure.
shots = os.path.join(TMP, "shots")
os.makedirs(shots)
fc3 = FakeClock()
trk3 = gui.ProgressTracker(frames_total=10800, clock=fc3,
                           wall_clock=time.time)
trk3.frame_probe = lambda since: gui.max_frame_in_dir(shots, since)
trk3.start("g", "G")
trk3.feed_line("== [4/6] exploring")
trk3.feed_line("[port] testing round 1 of at most 8")
for n in (300, 600):
    cap = os.path.join(shots, "frame_%05d.ppm" % n)
    with open(cap, "wb") as fh:
        fh.write(b"P6\n2 2\n255\n" + bytes(12))
    os.utime(cap, None)  # written now, i.e. after the round started
trk3.poll_frames()
snap = trk3.snapshot()
check("progress: explore frames refine the stage fraction",
      snap["frames_done"] == 600
      and abs(snap["fraction"] - (3 + 600 / 10800.0) / 6.0) < 1e-9
      and "frame 600 of 10,800" in snap["stage_text"]
      and "round 1" in snap["stage_text"], repr(snap))
fc3.advance(59)
snap = trk3.snapshot()
check("progress: 59 s of silence is not yet a stall",
      snap["idle"] == 59 and snap["stalled"] is False)
fc3.advance(2)
snap = trk3.snapshot()
check("progress: 61 s of silence is a stall",
      snap["idle"] == 61 and snap["stalled"] is True)
cap = os.path.join(shots, "frame_00900.ppm")
with open(cap, "wb") as fh:
    fh.write(b"P6\n2 2\n255\n" + bytes(12))
trk3.poll_frames()
snap = trk3.snapshot()
check("progress: an advancing frame count revives liveness",
      snap["frames_done"] == 900 and snap["idle"] == 0
      and snap["stalled"] is False, repr(snap))
trk3.feed_line("[port] testing round 2 of at most 8")
trk3.poll_frames()
snap = trk3.snapshot()
check("progress: a new round resets frames; old captures excluded",
      snap["round"] == 2 and snap["frames_done"] == 0, repr(snap))
check("progress: elapsed comes from the injected clock",
      abs(trk3.snapshot()["elapsed"] - 61) < 1e-9,
      repr(trk3.snapshot()["elapsed"]))

os.makedirs(os.path.join(TMP, "empty-shots"))
check("max_frame_in_dir: empty and missing dirs give None",
      gui.max_frame_in_dir(os.path.join(TMP, "empty-shots")) is None
      and gui.max_frame_in_dir(os.path.join(TMP, "no-such-dir")) is None)
odd = os.path.join(TMP, "odd-shots")
os.makedirs(odd)
for name in ("frame_abc.ppm", "frame_00100.png", "other.txt"):
    with open(os.path.join(odd, name), "wb") as fh:
        fh.write(b"x")
check("max_frame_in_dir: non-capture names ignored",
      gui.max_frame_in_dir(odd) is None)

trk3.finish()
snap = trk3.snapshot()
check("progress: finish reports done at 100%",
      snap["phase"] == "done" and snap["fraction"] == 1.0
      and snap["active"] is False)
trk3.feed_line("== [1/6] disc")  # inactive: must be a no-op
trk3.start("g2", "G2")
snap = trk3.snapshot()
check("progress: start resets cleanly for the next title",
      snap["phase"] == "prep" and snap["fraction"] is None
      and snap["frames_done"] == 0 and snap["step"] == 0)

# Packaging-gate UI regression (field bug, 2026-11-19 night): the
# owner saved a gameplay verdict while the app was busy; the label
# said "Packaging is unlocked" but the Package button stayed greyed,
# and it stayed greyed after the run ended. packaging_gate() is now
# the ONE evaluation behind both the label and the button. Replay
# tonight's exact sequence at the logic level:
gate_game = gui.new_game_record("T", "gateui", "/nonexistent.iso",
                                {"boot_file": "SLUS_999.99",
                                 "serial": "SLUS-99999"}, "", "")
enabled, message = gui.packaging_gate(gate_game, busy=False)
check("gate: no verdict -> disabled, locked message",
      enabled is False and "locked until a verdict" in message,
      repr((enabled, message)))
# ... he records a gameplay verdict (saving left untested) mid-run:
gate_game["verdict"] = {"reach": "gameplay", "stall_stage": "",
                        "stall_note": "", "saving": "untested"}
enabled, message = gui.packaging_gate(gate_game, busy=True)
check("gate: verdict saved while busy -> still disabled",
      enabled is False, repr((enabled, message)))
check("gate: busy message is honest (no bare 'unlocked' claim)",
      "busy" in message and not message.endswith("is unlocked."),
      message)
# ... the run finishes; busy clears; the gate is re-evaluated:
enabled, message = gui.packaging_gate(gate_game, busy=False)
check("gate: run finished -> button-enable state is True",
      enabled is True and message.endswith("is unlocked."),
      repr((enabled, message)))
check("gate: saving verdict is NOT required (untested still opens)",
      enabled is True and gate_game["verdict"]["saving"] == "untested")
# Stalled never opens, busy or not:
gate_game["verdict"] = {"reach": "stalled",
                        "stall_stage": "Title screen",
                        "stall_note": "", "saving": "untested"}
en_st, msg_st = gui.packaging_gate(gate_game, busy=False)
check("gate: stalled verdict -> disabled with stalled message",
      en_st is False and "Stalled games are not packaged" in msg_st,
      repr((en_st, msg_st)))
# Invariant over the whole matrix: the message claims 'unlocked'
# exactly when the button is enabled - they can never disagree.
matrix_ok = True
for v in (None,
          {"reach": "gameplay", "stall_stage": "", "stall_note": "",
           "saving": "untested"},
          {"reach": "gameplay", "stall_stage": "", "stall_note": "",
           "saving": "proven"},
          {"reach": "stalled", "stall_stage": "Boot", "stall_note": "",
           "saving": "untested"}):
    gate_game["verdict"] = v
    for b in (False, True):
        en, msg = gui.packaging_gate(gate_game, busy=b)
        if en != msg.endswith("is unlocked."):
            matrix_ok = False
check("gate: label and button agree across the verdict x busy matrix",
      matrix_ok)

# Same sequence through the real App methods (stub widgets, no Tk):
# _update_lock on the verdict save, then _refresh_lock when the run
# ends - the wiring, not just the pure function.
class _FakeWidget:
    def __init__(self):
        self.state = None
        self.text = None

    def configure(self, **kw):
        if "state" in kw:
            self.state = kw["state"]
        if "text" in kw:
            self.text = kw["text"]


fake = object.__new__(gui.App)
fake.busy = True
fake.games = [gate_game]
fake.selected = 0
fake.package_btn = _FakeWidget()
fake.lock_label = _FakeWidget()
gate_game["verdict"] = {"reach": "gameplay", "stall_stage": "",
                        "stall_note": "", "saving": "untested"}
fake._update_lock(gate_game)  # the Save Verdict click, mid-run
check("app wiring: mid-run save -> button disabled, gate message",
      fake.package_btn.state == "disabled"
      and fake.lock_label.text
      == gui.packaging_gate(gate_game, True)[1],
      repr((fake.package_btn.state, fake.lock_label.text)))
fake.busy = False
fake._refresh_lock()  # the run finishes; nobody re-clicks anything
check("app wiring: run end refresh -> button normal, unlocked label",
      fake.package_btn.state == "normal"
      and fake.lock_label.text.endswith("is unlocked."),
      repr((fake.package_btn.state, fake.lock_label.text)))
fake.selected = None
fake._refresh_lock()
check("app wiring: no selection -> button disabled, locked label",
      fake.package_btn.state == "disabled"
      and "locked until a verdict" in fake.lock_label.text,
      repr((fake.package_btn.state, fake.lock_label.text)))

print("\n%d passed, %d failed" % (passed, failed))
if failures:
    print("failed: %s" % ", ".join(failures))
sys.exit(1 if failed else 0)
