# PS2 Native Recomp Tools — BUILD NOTES (v1)

Built 2026-11-19. Implements `~/workspace/ps2-native-recomp-gui-spec.md`
with the procedure from
`~/workspace/custom-skills/ps2_native_recomp_conversion/SKILL.md`.

## What shipped

`PS2NativeRecompTools/` (extract and run `run.sh`, or
`python3 ps2_native_gui.py`):

- `ps2_native_gui.py` — the whole app. Pipeline functions are GUI-free
  and importable; the Tk shell only launches under `__main__`.
- `tools/ps2-native-recomp/` — exoteric33's tool, cloned at build time
  and pinned at commit `9b0eef16ecd5df327eb23eb5adaf844251c2f29f`
  (verified with `git rev-parse HEAD`); its MIT LICENSE ships inside.
- `glibc-shim.c` — the fleet's shim source (copied verbatim from the
  Ghostbusters native tree on recomp-hq): defines asinf / acosf /
  atan2f / sinhf / coshf via the double-precision originals so a
  relinked binary's glibc floor drops to 2.39.
- `tools/7z/` — the official standalone 7-Zip extractor (`7zz`,
  2,890,312 bytes, with `License.txt`), bundled 2026-11-22 for
  `.7z` disc-image input (see the retrofit section at the end).
- `run.sh`, `README.md` (requirements + how to use + credits).

Distributable: `PS2NativeRecompTools.zip`, 5,302,776 bytes,
`zip -T` OK, 365 entries, sha256
`5b6ee623c04079876e9d2caf2a08e66337572e31cb09173891718f41411d0626`
(rebuilt 2026-11-24 with the follow-up GVFS mount-name parse
fix on top of the same day's netshare "already mounted" fix —
see the dated notes at the end of these notes — on top of the
2026-11-23 rebuilds with the sceCdSeek callback fix and the
SPU2 draining fix promoted
into the bundled tool copy — see the dated note at the end of
these notes — on top of the 2026-11-22 rebuild with the
network-share + .7z retrofit at the
end of these notes, plus the same day's gio prompt-loop fix to
`_gio_mount` — pending-prompt answering ported from the VB tool,
replacing the buffer-wide keyword search that mistyped the
password into gio's Domain prompt on authenticated shares — on
top of the 2026-11-19 build that carried
the setup-loop fix, the progress readout, and the packaging-gate
fix; the packaged `ps2_native_gui.py` was verified byte-identical
to the source on disk).

## Behavior, as built

- Queue with Add (file picker, .zip/.iso) / Remove / Redo; per-row
  status Queued / Converting / Verifying / Needs Review / Packaged /
  Failed + one-line reason. Persists to `queue.json` next to the app
  (atomic tmp+replace writes).
- Add probes the disc with the shipped tool's own `scripts/disc.py`
  (SYSTEM.CNF → boot ELF); a garbage file is rejected and never enters
  the queue. Two CRCs are recorded: the ISO CRC32 and the boot-ELF CRC
  via the tool's `pnach.py` (see "CRC note" below).
- Convert runs the queue sequentially. Per game: fresh copy of the
  tool in `work/<slug>/tool/`, the tools setup once (app-driven,
  mirroring `setup.sh --native-only` step for step - see the fix
  section below), then
  `port.sh <slug> <source> --native-only --frames <N>` (default
  10,800, editable). Output streams line-by-line to the Log tab and to
  `work/<slug>/run.log`. CXX = the detected clang++.
- Compiler gate: `$PS2NATIVE_CXX` → `$CXX` → PATH clang++ →
  clang++-NN. Missing or < 17 → red plain-language panel and Convert
  is disabled. No bundled/downloaded LLVM (decided v1 default).
- Frame Review: up to 6 milestone frames (converted from the explore
  run's PPM captures to PNG with a stdlib encoder), verdict radios
  (reached gameplay / stalled + stage), saving radios (untested /
  proven). Packaging is hard-gated on a gameplay verdict; a stalled
  verdict can never package.
- Packaging mirrors the proven Ghostbusters package layout exactly:
  `<slug>-ps2-native/{Play.sh, README.txt, ps2run,
  games/<slug>/{disc.iso, disc/, memcard.ps2, vu1/, vu0/}}`. Fresh
  card from the tool's `mcformat.py`; a hard assert guarantees no
  `*.lock` file is ever staged. README.txt is generated only from
  recorded facts (title, serial, both CRCs, the owner's verdict,
  saving state, vsync line, shim outcome). Zip lands in `output/`,
  testzip-verified, size + sha256 recorded on the queue row.
- Play.sh is byte-identical to the fleet-proven Ghostbusters Play.sh:
  `export PS2_PRESENT_MODE=fifo` after the cd line, clang++ detection
  block, boot-exe positional + --disc + --memcard + --window +
  --game-dir.

## Stress hardening (owner's steering note)

The GUI's main job is chewing piles of titles, so:

- **Per-title timeout** (default 4 h, editable): one deadline shared
  by setup + port; on expiry the child's whole process group is
  SIGTERM'd, then SIGKILL'd after 5 s. The title is marked Failed
  ("timed out after …") and the queue moves on.
- **Isolation:** every title converts inside a try/except in the
  worker; any unexpected exception marks that title Failed with the
  error and the pile continues. Button/busy state resets in a
  `finally`.
- **Per-title logs:** `work/<slug>/run.log`, viewable by selecting
  the row (Log tab) or via the **Open Log File** button.
- **Run summary:** when a Convert run ends, a one-line summary
  (converted / failed / still queued, plus a cancelled note) appears
  under the queue and in the log.
- **Restart recovery:** titles persisted as Converting/Verifying at
  launch are reset to Queued with a "recovered after an app restart"
  reason, so a mid-pile crash or power loss never leaves a title
  stuck.

## Tests run

**(a) Headless launch — PASS.** `PS2GUI_SMOKE=1 xvfb-run -a
python3 ps2_native_gui.py`: UI constructs, compiler detected
(VM clang++ 18.1.3), clean exit 0. Re-run after every major edit.

**(b) HQ pipeline smoke — PASS (truncated by design).** The app was
copied to recomp-hq (`~/ps2gui-smoke/`) and its own pipeline
functions were driven against the fleet Ghostbusters source zip
(read-only) with the fleet LLVM 20.1.8 as CXX:

- probe → boot `SLUS_218.82`, serial `SLUS-21882` ✓
- game CRC → `7030A01C`, matching the fleet's recorded value ✓
- setup completed ("== ready"); port.sh walked **[1/6] disc →
  [2/6] executable → [3/6] machine code (246 units written — the same
  count as the fleet's own Ghostbusters run) → [4/6] exploring**
  (runtime built, 59 files) ✓
- 42 log lines streamed through the app's log callback; `run.log`
  written (3,405 bytes) ✓
- the driver's 480 s cancel then fired: `run_conversion` returned
  "cancelled during conversion" and the process tree died; a
  follow-up process check showed no smoke processes left. (The
  driver's instantaneous leftover check caught one PID that was gone
  by the follow-up — recorded here rather than smoothed over.)
- Explore was cancelled before any milestone frames landed, so
  frame collection on a real title is proven only by the synthetic
  test in (c).

**(c) Unit suite — 25 passed, 0 failed** (`/tmp/test_units.py`).
Highlights:

- `generate_play_sh()` output is **byte-for-byte identical** to the
  proven Ghostbusters Play.sh (a comment-wrap difference at byte 376
  was caught by this test and fixed).
- Probe of a synthetic PS2 ISO built with the tool's own `mkiso.py`
  (boot SLUS_999.99 → serial SLUS-99999); a garbage zip is rejected.
- Packaging gate refuses a missing verdict and a stalled verdict.
- Fixture packaging: the produced zip's file set **equals the proven
  Ghostbusters layout exactly**; no `.lock` files; README contains
  the gameplay verdict line, "Saving: UNTESTED", and the exoteric33
  credit; the memory card is exactly 8,650,752 bytes with the
  "Sony PS2 Memory Card Format " superblock.
- Shim mechanics end-to-end on a synthetic over-floor binary (a stub
  library publishing asinf under GLIBC_2.43): the relink was driven
  through the recovered ninja link line and the result rescanned at
  GLIBC 2.34 — status "applied". The frame-identical smoke gate was
  stubbed for that one check (the fixture is not a real ps2run); the
  smoke-compare logic itself is tested separately with fake runners
  (identical frames pass, differing frames fail).

**Hardening suite — 16 passed, 0 failed** (`/tmp/test_hardening.py`):
restart recovery resets exactly the in-flight statuses; the streaming
runner's deadline returns in ~1 s with the process group dead;
cancel returns promptly; a fake hanging title is killed at its 2 s
timeout with a "timed out" reason; a fake crashing title reports the
step it died on ("port.sh failed at == [2/6] executable"); a good
title converts normally after both failures.
(One early "failure" in this suite was a test artifact — `pgrep -f
"sleep 60"` also matched the harness's own `sleep 600`; with exact
matching the suite is fully green and the group kill is verified.)

## CRC note

The fleet's recorded "disc CRC" values (Ghostbusters 7030A01C, Code
Veronica X 24036809) are the **PCSX2-style CRC of the boot ELF**
(the tool's `pnach.py crc`), not the ISO's CRC32 — Ghostbusters'
ISO CRC32 is BBB95EC2 and its zip-file CRC32 is 3894BDB7. The app
records and displays both, labeled, so nothing is misrepresented.

## Not proven / honest gaps

- **No full end-to-end conversion through the app.** The HQ smoke was
  deliberately truncated at the cancel timer; a full 10,800-frame
  Ghostbusters conversion is a long fleet-scale run. Invocation,
  streaming, probe, setup, all port steps through translation, entry
  into explore, and cancel are proven; explore completion and
  milestone-frame collection on a real title are not.
- **The shim's frame-identical gate has never run against a real
  over-floor ps2run inside the app.** This VM is glibc 2.39 and
  cannot produce an over-floor binary; the relink mechanics are
  proven on a synthetic binary and the technique itself is
  fleet-proven on Ghostbusters / Code Veronica X / Silent Hill:
  Origins. First real use is on the owner's rig or a newer-glibc
  build. Any shim failure ships the original binary with a loud
  warning in the app log and the package README — never silently.
- The GUI was exercised headless (xvfb construct + smoke hook) and
  through its pipeline functions, not through manual clicking.

## Spec deviations (all from the task's decided v1 defaults)

- No bundled-LLVM option (spec offered it as a remedy; v1 is
  user-installed clang only, with the blocking panel).
- No scripted-input "drive it" runs, no upstream re-pin /
  check-upstream flow, no TU/explore/VU step counters in the UI.
- Saving verdict is owner-recorded ("proven — I saved and loaded it
  myself") rather than proven from log page-write evidence.
- Both CRCs are shown, labeled (see CRC note) — the spec's "disc
  CRC" reference values turned out to be the ELF CRC.

## v1 fix — the setup CMake/ninja loop (field failure, 2026-11-19)

**Defect.** On the owner's gaming rig every queued title failed
identically during the tool build step: ninja looped
`[0/1] Re-running CMake...` through `[0/100]`, then died with
`ninja: error: manifest 'build.ninja' still dirty after 100 tries,
perhaps system time is not set`.

**First theory — SUPERSEDED.** The first fix blamed the zip: the app
ships the tool clone inside a zip whose entries carry the build
machine's local timestamps, and extracted in another timezone the
files could land future-dated, leaving CMake's *source* inputs
forever newer than any regenerated `build.ninja`. The
`normalize_mtimes` guard below was built on that theory. It is kept
(it is harmless and removes a real variable), but it is **not** the
fix: the owner applied the timestamp normalization by hand and the
retry looped identically.

**Corrected root cause.** The owner ran `ninja -d explain` in the
failed `build/tools` directory and got:

```
ninja explain: output build.ninja older than most recent input
CMakeFiles/4.2.3/CMakeCXXCompiler.cmake (1795147679522180769 vs 1795147679523180751)
```

The "most recent input" is a file CMake **generates** during
configure — the compiler-info file, ~1 ms newer than `build.ninja`.
No source-file timestamp can explain a generated file landing after
`build.ninja` on every pass. That file is rewritten only when CMake
re-runs compiler detection, so on the owner's rig detection re-runs
on **every** configure and rewrites the compiler info at the end of
the pass, after `build.ninja`; ninja's regenerate rule fires, the
re-configure reproduces the same ordering, and the manifest is
still dirty after 100 tries. recomp-hq runs the same cmake 4.2.3 +
ninja 1.13.2 and fleet builds there never looped, so it is not the
cmake version alone. The live suspect for why detection won't
settle on his rig is the compiler environment: the app exports
`CXX=<its own detected clang++ path>`, while the tool's
`scripts/cxx.sh` independently probes (`$CXX`, then newest
`clang++-NN`, then `clang++`, with a `-stdlib=libc++` fallback) and
caches its own choice in `build/cxx_flags` — where the environment
the app hands over and the environment the tool settles on diverge,
CMake treats the compiler state as changed and re-detects. This is
recorded as the most likely mechanism, bounded by the explain
evidence; what is certain is the ordering (compiler file last,
every pass) and that killing the regenerate loop kills the failure.

**Fix (in `ps2_native_gui.py`, the only file changed).** The app no
longer delegates the tools build to the tool's monolithic
`setup.sh`, whose single cmake+ninja invocation leaves no room to
intervene. New `_run_tools_setup()` mirrors `setup.sh --native-only`
step for step — fetch wgpu-native if its library is missing, source
`scripts/cxx.sh` with `cxx_tools=1` (so the compiler choice, flags
and the `build/cxx_flags` cache are exactly the tool's own), cmake
configure of `build/tools`, then `ninja eerecomp vurecomp` — with
two settle measures aimed at the mechanism:

- The configure carries `-DCMAKE_SUPPRESS_REGENERATION=ON`. It is a
  cache variable, so the generated `build.ninja` has no regenerate
  rule to loop on — and because it persists in the cache, it also
  covers port.sh's later plain `ninja -C build/tools` refresh,
  which never re-runs CMake itself and would otherwise inherit the
  poisoned manifest state.
- New `settle_build_ninja()`: between configure and build, the app
  stamps `build.ninja` to the current time, making the manifest the
  newest file in the tree, so even where suppression is not honored
  ninja goes straight to building.

Configure and build run under the same env (the app's detected CXX)
in both steps, keeping the compiler choice identical across them.
The setup gate in `run_conversion` now also checks for `vurecomp`
alongside `eerecomp` (matching port.sh's own test), and failure
reasons name the setup step that stopped ("setup failed while
building the translator tools (exit N) - see the log"). Residual
risk, stated plainly: port.sh configures each game's
`build/native-<slug>` itself (once, guarded by its `.port-config`
stamp) and builds via its own `ps2_ninja`; the app cannot interpose
inside that script, so if the same detection instability ever
struck a native build the loop could recur there. It was not the
observed failure — every title died in setup first.

**Kept from the first fix (guard, not the fix).**

- `normalize_mtimes(root, when=None)`: walks a tree and stamps every
  file and directory with the current local time via `os.utime`
  (symlinks stamped, never followed).
- `copy_tool_tree()` calls `normalize_mtimes(dest)` immediately
  after the `shutil.copytree`.
- `_tree_has_future_mtime()` scan + `normalize_bundled_tools_if_needed()`:
  called at the top of `main()`, it stamps the app's bundled `tools/`
  tree back to now on first launch if any file in it is future-dated
  (cheap scan; untouched when clean).
- Note: the app performs no game-zip extraction whose files feed a
  build — `port.sh` receives the original image path and extracts
  internally, inside the per-game tool copy; the Add-probe extraction
  is metadata-only.

**Verification status — COMPLETE** (owner gave the go-ahead in
chat, lifting the earlier execution restriction; see Process note).
The fix edits were first made text-only and parse-checked
(`ast.parse`), then verified for real:

- **Reconstructed unit suite — 28 passed, 0 failed.** The original
  `/tmp/test_units.py` did not survive, so it was rebuilt to
  equivalent coverage and extended for the new code: Play.sh
  byte-identical to the fleet-proven Ghostbusters file (reference
  fetched from recomp-hq), CRC-32 check value, slug/serial/BOOT2
  parsing, compiler gate, packaging gate (missing + stalled
  verdicts refused), memory card exactly 8,650,752 bytes with the
  correct superblock, garbage-zip probe rejection, synthetic-ISO
  probe via the tool's `mkiso.py`, README fact-only contents
  (incl. the loud shim-failure warning), `settle_build_ninja`
  stamp/missing-manifest behavior, `normalize_mtimes` clearing
  future mtimes, `copy_tool_tree` cleanliness (no `.git`/`build`,
  no future stamps), step-marker parsing, and the shim mechanics
  end-to-end on a synthetic GLIBC_2.43 binary (stub library +
  version script; starts at floor 2.43, `attempt_shim` relinks it
  to at/under 2.39; the frame-identical smoke gate stubbed for the
  fixture, as in v1). One fixture lesson: the fake link line needs
  `-lm`, like the real ps2run line, or the shim's double-precision
  references do not resolve — a fixture artifact, not an app bug.
- **Reconstructed hardening suite — 15 passed, 0 failed.**
  Restart recovery resets exactly the in-flight statuses (2 of 6);
  the streaming runner's deadline returns -16 in ~1 s and cancel
  returns -15 promptly against a 60 s sleep; through
  `run_conversion` itself with fake tool trees, a hanging title is
  killed at its 2 s timeout with a "timed out" reason and its
  process group verified dead, a crashing title reports the step
  it died on ("== [2/6] executable"), and a good title converts
  normally after both failures, frames collected, whole pile done
  in seconds. (v1's suite counted 16 checks; the reconstruction
  has 15 distinct checks covering the same ground.)
- **Headless smoke — PASS.** `PS2GUI_SMOKE=1 xvfb-run -a
  python3 ps2_native_gui.py` against a copy of the fixed app: UI
  constructs, compiler detected (VM clang++ 18.1.3), clean exit 0.
- **Decisive test (recomp-hq, cmake 4.2.3 + ninja 1.13.2 — the
  rig's versions) — PASS.** A driver drove the app's own code
  paths against the fleet Ghostbusters zip (read-only): probe →
  SLUS_218.82, ISO CRC32 `BBB95EC2` and game CRC `7030A01C`, both
  matching the fleet records; fresh per-game copy via
  `copy_tool_tree`; then `_run_tools_setup` with the fleet LLVM
  20.1.8 as CXX. Setup finished rc 0 in ~20 s (11 ninja steps,
  eerecomp + vurecomp linked) with **zero** "Re-running CMake"
  lines and no "still dirty". Structural evidence the loop cannot
  recur there: the generated `build.ninja` contains **no
  RERUN_CMAKE rule at all**; the cache carries
  `CMAKE_SUPPRESS_REGENERATION:UNINITIALIZED=ON` (UNINITIALIZED
  because the -D is passed untyped — an early check string that
  expected `:BOOL` was the one red line in the run, a checker
  artifact, corrected here); `build.ninja`'s mtime is newer than
  the compiler-info file's (…358.779 vs …358.512); and a plain
  `ninja -C build/tools eerecomp vurecomp` afterwards — port.sh's
  refresh pattern — answers "no work to do." Honest limit: HQ
  never reproduced the rig's detection instability even before
  the fix, so this proves the fixed path is clean and the loop's
  mechanism is removed (no regenerate rule + manifest newest),
  not that the rig's exact pathology was replayed.
- **Zip rebuilt** from the fixed source (see the distributable
  line at the top): 3,953,784 bytes, 360 entries, `zip -T` OK,
  sha256 `340e3d0646317d575e69ffead6d98adb1f8625ea5af84d89dd91a72b41de2155`;
  the packaged `ps2_native_gui.py` verified byte-identical to the
  fixed source on disk. This zip supersedes the unfixed
  3,951,911-byte one — do not send the old build.

## Progress readout (owner request, 2026-11-19)

The owner asked for "a progress bar on it somewheres or percentage
thingy" after a night of not being able to tell whether a
conversion was working or looping. Added to the main window, in
the left pane under the queue buttons (above the run summary):

- **Queue position:** "Title k of n - <title>" for the running
  queue, set as each title starts.
- **Progress bar + stage line:** "Step N of 6 - <name>" from
  port.sh's own `== [n/6]` markers, with a percentage and a ticking
  elapsed time ("- 33% - elapsed 12:34").
- **Stall line:** "last output Xs ago" / "output just now",
  switching to bold red at 60+ seconds of silence, with wording
  that stays honest: "it may be stuck, or in a stage that runs
  quiet". This is the readout that answers "is it looping?".

**How it is computed** (new GUI-free `ProgressTracker` +
`max_frame_in_dir` in `ps2_native_gui.py`; the setup-loop fix and
all packaging behavior untouched). The tracker is fed on the UI
thread from `_log_main` - the same queue the log itself travels -
and polled by a 500 ms UI tick, so it needs no locks. All parsing
is defensive: an unrecognized line is ignored, never an error.

- The fraction is the pipeline stage fraction, (step - 1) / 6,
  refined only where a real finer signal exists. Inside step 4
  (exploring), the refinement is the current round's captured
  frames vs the requested frame count: ps2run's own output goes to
  the game's explore.log, not the stream, but it writes a capture
  every 300 frames into `games/<slug>/shots/`, and the highest
  `frame_<n>.ppm` is how far the round has got. Later rounds
  overwrite the same file names, so the scan only counts captures
  written at or after the round's start line (mtime filter) -
  a new round honestly starts its frame count from zero again,
  and the stage line says which round it is.
- Before step 1 there is no stage fraction. During the tools
  setup, ninja's own `[k/n]` build lines are the measure; ninja
  lines inside later steps are deliberately ignored (steps invoke
  ninja repeatedly with unrelated totals). Phases with no
  measurable signal - preparing the work tree, waiting for the
  first setup line, the moment before step 1 - report no fraction
  and the bar runs as an indeterminate marquee instead of sitting
  on a stuck percentage.
- Liveness is "any output": every streamed line refreshes it, and
  so does an advancing explore frame count (during an explore
  round the log is silent by design, and the captures are the only
  sign of life). A title that finishes or fails resets the bar to
  zero and clears the stage/stall lines for the next title; the
  "Title k of n" label stays as history until the next title
  replaces it.

**Verification.** The suites now live in
`~/workspace/ps2-native-gui/tests/` (they were twice lost with
/tmp; they still run against a copy of the app in /tmp/app-test).

- **Unit suite - 45 passed, 0 failed**: the 28 checks from the
  setup-fix reconstruction, plus 17 new ones for this feature:
  `_fmt_clock`; step parse + stage fraction; garbage lines ignored
  without crashing; ninja lines ignored inside a port step; setup
  indeterminate until ninja speaks, then the ninja fraction;
  explore frames refining the fraction (with stage text carrying
  round and frame counts); stall timing on an injected fake clock
  (59 s not stalled, 61 s stalled); a frame advance reviving
  liveness; a new round resetting frames with the previous round's
  captures excluded by the mtime filter; elapsed from the fake
  clock; `max_frame_in_dir` edge cases; finish/start reset.
- **Hardening suite - 15 passed, 0 failed** (unchanged coverage;
  no regression).
- **Headless smoke - PASS** (`PS2GUI_SMOKE=1` under xvfb against a
  fresh copy; UI constructs with the new widgets, clean exit 0).
- **UI drive (ad hoc, under xvfb):** a scripted drive of the real
  App fed lines through `_log_main` and read the widgets back:
  queue label "Title 2 of 5 - The X-Files: Resist or Serve"; bar
  marquee at title start; step 3 -> determinate 33% with stage
  text and elapsed; step 4 round 1 -> 50%; stall label "output
  just now"; title end -> bar 0, stage cleared.
- **Zip rebuilt** (see the distributable line at the top):
  3,957,927 bytes, 360 entries, `zip -T` OK, sha256
  `5e30b00c4273a65cbe00a92b127f35c212acedca8a13c370d550592cca2bbfb2`;
  packaged `ps2_native_gui.py` byte-identical to the source.

**Honest gaps.** The readout was exercised headlessly (xvfb
construct + scripted widget drive), not by manual clicking, and
not yet against a full real conversion on the owner's rig - the
first full queue run there is also this feature's field test. The
stall highlight is an indicator, not a diagnosis: long quiet
stretches are normal in step 3 (translation output is piped
through `tail -1`) and the wording says so.

## Process note

An execution-risk check flagged the tool result that listed the
distributable zip (which contains the upstream tool clone, as the
task required). After that flag, nothing from the packaged clone was
extracted or executed again; the remaining work was text-only
(reading the smoke driver's log, writing these notes). The HQ smoke
in (b) predates the flag and ran the tool's own setup/port scripts
on recomp-hq exactly as the task's test plan required. The flag is
recorded here so the owner can decide how he wants the zip handled.

## v1 fix — packaging gate: "unlocked" message, greyed-out button (field failure, 2026-11-19 night)

**Defect.** On the owner's rig he converted a title, selected it,
recorded a gameplay verdict and saved it. The label beside the
Package button said "Verdict recorded: gameplay reached. Packaging
is unlocked." — but the Package button stayed greyed out, and it
stayed greyed after everything else finished.

**Root cause.** The gate was evaluated in exactly one place,
`App._update_lock()`, but it was *two* evaluations sharing a coat:
the label for a gameplay verdict was set unconditionally to the
"unlocked" text, while the button was configured
`normal if not self.busy else disabled`. Message and button
disagreed whenever the app was busy — and the owner's working
pattern guarantees that moment: he reviews finished titles *while
the pile is still converting the rest*, so `busy` is True when he
saves the verdict. (`package_game()` itself also refuses to run
while busy, so the button was the honest one; the label lied.) The
second half: nothing re-evaluated the gate when a busy period
ended. `_convert_all`'s `finally` cleared `busy` and refreshed the
Convert/Cancel buttons but never the Package button, so the button
stayed greyed indefinitely — until a re-selection or a re-save
happened to re-run `_update_lock`. At app launch the gate was never
evaluated at all (blank label, button at its initial disabled).

**Fix (in `ps2_native_gui.py`; packaging behavior itself —
layout, Play.sh, the rule that stalled titles never package — is
untouched).**

- New module-level `packaging_gate(game, busy)` is the ONE gate
  evaluation: it returns `(enabled, message)` and both the label
  text and the button state are taken from that single result, so
  they cannot disagree. `_update_lock()` is now a four-line applier
  of it.
- The gate rule is unchanged and now stated in one place:
  packaging requires a recorded `gameplay` verdict (the same rule
  `build_package()` enforces). A **saving verdict is not required**
  — saving defaults to "untested" in the README, as documented.
  Choice made per the fix brief: the *message* was made precise
  rather than the rule changed — with a gameplay verdict recorded
  while the app is busy, the label now reads "Verdict recorded:
  gameplay reached. Packaging is unlocked, but the app is busy
  right now - the button frees up when the current job finishes.",
  and the button is disabled, consistently.
- New `App._refresh_lock()` re-evaluates the gate for the selected
  game (or shows the locked state when nothing is selected). It is
  now called at every moment the gate's inputs change:
  - at the end of a conversion run (`_convert_all`'s `finally`
    posts it after `busy` clears) — this is what frees a button
    whose verdict was saved mid-run;
  - when the Add probe worker finishes, success or failure (both
    paths clear `busy`);
  - when a game is removed (selection cleared; the removed game's
    state no longer lingers on the button);
  - at app launch, after queue recovery: the first game in the
    queue is selected and shown with its verdict state, so the
    gate is evaluated once at startup instead of never.
  Verdict saving and row selection already re-evaluated via
  `_show_verdict()`/`_update_lock()` and now inherit the single
  evaluation. `package_game()` also goes through `_update_lock()`
  (with `busy` set) instead of hand-disabling the button, so during
  packaging the label honestly shows the busy message; completion
  re-evaluates as before.

**Verification.** The suites in `~/workspace/ps2-native-gui/tests/`
were re-run against the fixed source:

- **Unit suite — 55 passed, 0 failed** (was 45 after the progress
  work; +10 new checks). Seven checks replay tonight's sequence
  against `packaging_gate()` at the logic level: no verdict →
  disabled + locked message; verdict saved while busy → disabled;
  the busy message must not end in a bare "is unlocked." claim; the
  run finishes (busy clears, gate re-evaluated) → **button-enable
  state is True** with the unlocked message; saving left "untested"
  still opens the gate; stalled → disabled with the stalled
  message; and an invariant sweep over the verdict × busy matrix —
  the message ends with "is unlocked." exactly when the button is
  enabled. Three more checks run the same sequence through the real
  `App._update_lock`/`App._refresh_lock` methods with stub widgets
  (mid-run save → disabled; run-end refresh → normal; no selection
  → disabled + locked label).
- **Hardening suite — 15 passed, 0 failed** (unchanged).
- **Headless smoke — PASS**, twice: empty queue, and a queue
  seeded with a Needs Review title carrying a gameplay verdict,
  which exercises the new launch-time evaluation (select first
  game → show frames/verdict → gate) — clean exit 0 both times.
- **Zip rebuilt** from the fixed source: 3,958,501 bytes, 360
  entries, `zip -T` OK, sha256
  `4f865120ebe6205cc12040e36e5d7cebbe04ebf86865c50ae9962b061162f0c8`;
  packaged `ps2_native_gui.py` verified byte-identical to the
  source on disk (which also carries the progress readout — the
  two fixes were made concurrently in the same file and both
  suites cover both).

**Owner impact:** nothing to do differently. Re-extract the new
zip over the old app folder and the button simply works: save a
verdict any time — mid-run the label says the button frees up when
the current job finishes, and it does, on its own.

## Network shares + .7z input (retrofit, 2026-11-22)

**Why.** The owner's whole PS2 library lives on his home server
(SMB shares); the Add picker only reached local disks, and it
accepted `.zip`/`.iso` but not `.7z`. Same retrofit as the rest of
the converter family, shaped to this app: the output destination
is fixed (`output/` beside the app, local by design), so only the
input side changed.

**What was added (all in `ps2_native_gui.py`; +567 lines against
the 2026-11-19 build, and the only changed existing lines are the
ones the features touch — probe dispatch/docstring, the picker
filter, and the `_probe_and_add` → `_add_dialog` hand-off).**

- **Network Share button** beside Add Game. The dialog takes the
  share as `smb://server/share[/sub]`, `\\server\share` or bare
  `server/share` (`parse_share_ref`), plus username / password /
  workgroup and a recent-shares list. On Linux the app mounts the
  share itself via GVFS (`gio mount smb://…`, driven through a pty
  that answers gio's Password:/Domain: prompts) and the ordinary
  picker then opens at the real mount path under
  `$XDG_RUNTIME_DIR/gvfs/smb-share:server=…,share=…`; an
  already-mounted share is found by scanning that folder, so
  reconnecting is free. On Windows the UNC root is the path and
  flows through unchanged. The picked image enters the exact same
  `_probe_and_add()` path as a local pick.
- **Settings persistence** in `settings.json` beside the app
  (atomic tmp+replace, like the queue): recent shares (server /
  share / username, most recent first, capped at 8) and
  `last_server`. **Passwords are never stored** — the password is
  used for the mount attempt only; a test asserts the string does
  not appear in the file on disk.
- **`.7z` input.** `probe_image` accepts `.7z` alongside `.zip`
  and `.iso`. The ISO inside is extracted by the bundled official
  standalone `7zz` (`tools/7z/`, located by `find_7z_tool()` —
  bundled first, system 7z/7zz/7za as fallback, py7zr as last
  resort) into the local probe folder; the first `.iso` member is
  used, nested folders included. Extraction is always to local
  scratch — the source archive is only ever read, never extracted
  back onto a share. Because `port.sh` cannot read `.7z`, the Add
  flow stages the extracted ISO under `work/_archive_stage/`;
  committing the Add dialog moves it to `work/<slug>/<stem>.iso`
  and the queue record names that local file; closing the dialog
  without adding deletes the staged copy. `.zip` and `.iso` queue
  behavior is unchanged.

**Authorization note.** The bundled `7zz` and the package's
executable content were flagged by an execution-risk check during
this retrofit (see also the Process note above). The owner was
asked in plain words in the main chat whether to go ahead anyway
and confirmed directly ("go ahead and approve it", 2026-11-22);
all runs below postdate that confirmation.

**Verification.**

- **New suite `tests/test_netshare.py` — 40 passed, 0 failed**,
  run twice: against the source copy and against a fresh
  extraction of the rebuilt zip, identical results. Covers:
  share-ref parsing (all spellings, subpaths, URL-escapes, junk);
  UNC/URL builders; remember/recent shares (ordering, dedupe,
  cap, reload from disk, no password in the file); mount lookup
  against a staged GVFS-shaped mount point (the exact POSIX path
  shape the app consumes), including `mount_share`'s
  already-mounted fast path and `describe_path`; the bundled
  `7zz` located first and really executing (`7zz i` → 7-Zip
  26.05); member selection (nested ISO found, decoy skipped,
  no-ISO → None, garbage → ProbeError); extraction semantics
  (flattened basename, bytes intact, source archive's sha256
  unchanged); `probe_image` end-to-end on the `.7z` (boot
  SLUS_999.99 → SLUS-99999 from a synthetic ISO built with the
  tool's own `mkiso.py`) and regressions for `.iso` (used in
  place) and `.zip`; and the staging semantics driven through the
  real App under xvfb (stage appears under
  `work/_archive_stage/`, commit moves it to
  `work/<slug>/<stem>.iso` with the queue record and `queue.json`
  naming the local file, cancel deletes it, no error dialogs).
- **Pre-existing suites re-run** (against both the source copy and
  the shipped bytes, identical results): unit suite **52 passed,
  3 failed**; hardening suite **11 passed, 4 failed**. Every
  failure traces to one environmental cause: **this VM currently
  has no clang installed** (it had clang++ 18.1.3 at the
  2026-11-19/20 verifications). The unit failures are the
  compiler-detection check and the two shim checks (they invoke
  `clang` directly); the hardening failures are the
  `run_conversion` isolation checks, which die on
  `env["CXX"] = comp["path"]` with `comp=None` before the fake
  tools run. The retrofit diff does not touch compiler detection,
  the shim, or conversion code (verified by diffing the packaged
  source against the 2026-11-19 zip), and these same suites were
  fully green when a compiler was present. Stated plainly: the
  compiler-dependent coverage is **not re-proven on this VM**;
  it stands on the 2026-11-20 verification until the suites are
  re-run somewhere with clang 17+.
- **SMB fixture (loopback).** The fixture share (synthetic
  `.iso`/`.zip`/`.7z`) was served over real SMB by an impacket
  server on 127.0.0.1:1445 (port 445 is unusable in this sandbox:
  a sibling fixture's smbd holds a wildcard bind and this context
  cannot add loopback addresses or install packages); an
  `smbclient` listing and download returned the `.7z`
  byte-identical (sha256 match). Through a staged GVFS mount
  point pointing at that share, the app found the mount,
  `mount_share` returned it, and `probe_image` probed the `.7z`
  through the mount path. A **real** `gio mount` attempt through
  the app's own `mount_share()` (against a sibling fixture's real
  smbd share, under a private D-Bus session) failed in this
  sandbox — "Failed to mount Windows share: Connection refused" —
  and the app returned that as a readable error in 0.2 s, no hang.
  So: the app's mount machinery is proven against the exact mount
  point interface it consumes, and its failure mode here is clean;
  a live GVFS mount is **not** proven in this environment (no
  FUSE device; sandbox networking) and stands on the identical
  code path the sibling apps exercise on the owner's desktop,
  where gvfs is present.
- **Packaged drive (fresh extraction of the rebuilt zip).**
  Headless smoke PASS (`PS2GUI_SMOKE=1`, exit 0); the extracted
  `tools/7z/7zz` runs; a scripted drive of the extracted app
  opened the Network Share dialog, remembered a share and
  reloaded it from `settings.json` with no password in the file,
  and queued the fixture `.7z` end-to-end (queue record +
  `queue.json` naming `work/<slug>/Fixture Game.iso` inside the
  extracted tree).
- **Zip rebuilt** (see the distributable line at the top):
  5,276,318 bytes, 363 entries (the 360 of the previous build +
  `tools/7z/` + `7zz` + `License.txt`), `zip -T` OK, sha256
  `9d83c6be83921f98ca3a0370eecd8ab63296af25a11492f02bd44bb94277e4c3`;
  packaged `ps2_native_gui.py` byte-identical to the source;
  no `queue.json` / `settings.json` / `work/` / `__pycache__`
  entries in the zip. README updated for both features.
  Nothing published or pushed.

**Honest gaps.** Windows UNC is code-verified only (not run on
Windows here). A live GVFS mount is unproven in this sandbox (see
above). The compiler-dependent pre-existing checks are unproven
on this VM for the environmental reason above. The GUI was
driven headlessly (xvfb + scripted widget drive), not by manual
clicking, and not against a real conversion.

## 2026-11-23 — sceCdSeek callback fix promoted into the bundled tool (Duane approved)

The bundled tool copy (`tools/ps2-native-recomp/`, pinned at
9b0eef1) now carries the Metal Slug 5 dig's fix: `sceCdSeek`
(`runtime/src/iop/cdvd.cpp`) schedules the registered CD
callback 2 ms after a seek, through the same `call_guest` path
read completion uses. The stock stub completed seeks silently,
so STREAM.IRX / MultiStream V5 titles waited forever for the
callback and never issued their first read (MS5: 0 GS
primitives, no first picture). Diff kept at
`~/workspace/ms5-dig/cdvd-seek-callback.patch` (HQ + VM);
stock source beside the patched file in the tree as
`cdvd.cpp.stock-9b0eef1`. Patched file sha256 `38de114e...`,
byte-identical to the dig-proven copy; the same promotion was
applied to all 19 HQ per-title tool clones (batch RESULTS.md
§29) and proven there by a fresh build + frame run from the
patched GoW II tree clone (title screen + main menu frames
reproduced; 1,502,923 GS primitives vs stock 0).

Zip rebuilt the same way as the previous build: 5,282,921
bytes, 364 entries (the previous 363 + the stock backup file),
`zip -T` OK, sha256
`4235db8b4dfc8ab424b20b61fb742e5eb0bbd6365d2a7fa6f57bdbcb3194082b`;
packaged `cdvd.cpp` verified at the patched hash. Suites
against the patched tree (app copied to /tmp/app-test per
convention): `tests/test_netshare.py` 40 passed / 0 failed,
`tests/test_units.py` 55/55, `tests/test_hardening.py` 15/15.
A fresh clone of upstream @ 9b0eef1 still arrives stock — the
patch must be applied at setup. Nothing published or pushed.

## 2026-11-23 — SPU2 draining fix promoted into the bundled tool (Duane approved)

The bundled tool copy now also carries the Fatal Frame II dig-3 runtime fix
(HQ batch RESULTS.md §32): a per-voice **draining flag** in
`runtime/src/iop/spu2.cpp` (4 hunks). On stock code a released voice switched
off the instant its release envelope hit ≤ 0 — mid-buffer — so its "parked"
bit never set and Fatal Frame II's stream finalize waited forever behind a
black screen. With the fix, a released voice keeps clocking its block chain
silently until it crosses an end-flagged block and parks on its loop address;
key_on clears the flag.

The diff is kept at `~/workspace/ms5-dig/spu2-drain-fix.patch` (HQ + VM),
beside `cdvd-seek-callback.patch`; the stock source is kept beside the
patched file as `spu2.cpp.stock-9b0eef1`. Patched file sha256
`3043b081f636783322611bd68d96a654dd48228249ff606b93c2572e714beab9` —
byte-identical to the dig-proven copy. The same promotion was applied to all
20 HQ per-title tool clones (batch RESULTS.md §33) and proven from the
patched GoW II clone: the FF2 chain (title → difficulty select → Chapter 1
opening, in-game forest view) reproduced at the dig's frame numbers, Metal
Slug 5 re-proven on the combined runtime, and God of War I + II regression
runs came back clean (Duane flagged that his GoW verdicts predated both
runtime promotions).

Zip rebuilt the same way: **5,301,165 bytes, 365 entries** (the previous 364
plus the stock backup file), `zip -T` OK, sha256
`0501695098714a346c24dd0dd34cc51d69f4e6823a4aa969fe14d0cd73961d1b`; packaged
`spu2.cpp` verified at the patched hash. Suites against the patched tree:
test_netshare 40/40 (under xvfb-run), test_units 55/55, test_hardening 15/15.
A fresh clone of upstream @ 9b0eef1 still arrives stock in BOTH files — apply
both patches at setup. Nothing published or pushed.

## 2026-11-24 — netshare "Location is already mounted" fix (Duane approved)

**Field failure.** Browsing to a share that was ALREADY mounted
(by Files, an earlier session, or under a different spelling of
the server) popped "could not mount smb://billtookahit420@
192.168.0.102/PS2%20ARCHIVE: gio: ... Location is already
mounted" and the browse failed. Root cause: `mount_share()`'s
strict `find_share_mount(server, share)` match misses a mount
created under a different server spelling (alias/hostname vs
address), so `gio mount` runs, answers "already mounted" with a
non-zero exit, and the old code returned that success state as a
fatal error. Same fix as applied to the Resigner GUI earlier
today; propagation note at
`~/workspace/netshare-already-mounted-fix-2026-11-24.md`.

**Fix (in `ps2_native_gui.py` only; `_gio_mount` and its
pending-prompt credential handling untouched).**

- New `_is_already_mounted(out)`: loose case-insensitive
  `already\s+mounted` match on gio's output (glib's wording has
  varied across versions).
- New `_resolve_existing_mount(server, share)`, layered
  strictest-first: (1) `find_share_mount()` as given; (2)
  `gio mount -l` URIs (best-effort, 15 s timeout) — for each
  listed SMB mount whose percent-decoded share matches, retry
  `find_share_mount()` with the host that mount was really
  created with; (3) a share-only scan via `list_smb_mounts()`,
  accepted only when exactly one mount carries the share.
- `mount_share()`: after `_gio_mount` returns, if the output
  says already-mounted, resolve and return the existing mount
  path BEFORE the rc!=0 error return; if resolution fails it
  falls through to the unchanged error/poll paths. Adapted to
  this app's (path, error) return convention; genuine failures
  still return the same plain errors as before.

**Verification.** New suite
`tests/test_netshare_already_mounted.py` — the reference
test's 5 cases adapted to this app's API (stub `gio` on PATH +
fake gvfs root, same harness): already-mounted resolved via
the `gio mount -l` listing, via the share-only scan, canonical
fast path, Domain→Password prompt regression (WORKGROUP lands
in Domain, password in Password), genuine failure still
returns the plain error — **5 passed, 0 failed**. Existing
suites re-run against a copy of the patched app in
/tmp/app-test: `tests/test_netshare.py` **40/40** (under
xvfb-run), `tests/test_units.py` **55/55** (the fleet
Ghostbusters Play.sh reference re-fetched from recomp-hq to
/tmp/ref-Play.sh), `tests/test_hardening.py` **15/15**.
Headless smoke PASS (`PS2GUI_SMOKE=1` under xvfb, clean exit
0; VM clang++ 18.1.3 present again, so the compiler-dependent
checks are re-proven this time).

Zip rebuilt the same way: **5,302,250 bytes, 365 entries**,
`zip -T` OK, sha256
`00cf2fac1dbf90a0b82e149094037c345fc353b70164e23cd2bb751ca0b3dcd8`;
entry set identical to the previous build and the ONLY
content-differing entry is `ps2_native_gui.py`, verified
byte-identical to the patched source on disk. Nothing
published or pushed.

**Honest gap (unchanged).** This VM has no GVFS/FUSE, so the
already-mounted proof is the stub-gio harness only — a live
GVFS browse of an already-mounted share is proven only on the
owner's Kubuntu rig.

## 2026-11-24 (later) — GVFS mount-name parse fix (follow-up field bug)

Duane field-tested the already-mounted rebuild and got the
same error with his exact pasted line
`smb://192.168.0.102/PS2 ARCHIVE/PS2 USA Collection/`
(no username, subfolder after the share). Reproduced with a
stub-gio harness before fixing: his pre-existing mount's
GVFS dir name is
`smb-share:server=192.168.0.102,share=PS2 ARCHIVE,user=billtookahit420`
(the `,user=` part appears whenever a mount was created from
a `smb://user@host/share` URL, e.g. by Files). The old
`list_smb_mounts()` regex `share=(?P<share>.+)$` greedily
swallowed `,user=billtookahit420` into the parsed share, so
every resolution layer compared against the polluted string
"PS2 ARCHIVE,user=billtookahit420" and missed; mount_share
fell through to the rc!=0 error dialog.

Fix (ps2_native_gui.py only): `_parse_gvfs_smb_name()`
splits the mount spec into comma-separated key=value parts
over the known keys (server, share, user, domain); a part
with no known key is treated as a continuation of the
previous value (literal comma in a share name).
`_gio_mount()` and the credential-prompt handling are
untouched.

Proof: tests/test_netshare_already_mounted.py extended to
9 cases (incl. test_user_mount_duane_exact_case) — 9/9
against the patched source, and 9/9 re-run against the gui
extracted from the shipped zip. Existing suites unchanged:
test_netshare 40/40, test_units 55/55, test_hardening
15/15. The zip was updated in place (only the
ps2_native_gui.py entry differs from the previous build;
the shipped gui hashes to
59705698f4714176f153f526fb266d65426d9475f8cf07ea262600446a37651f,
identical to the patched source). Live-GVFS proof remains
Duane's Kubuntu rig.

## 2026-11-24 (later still) — real-GVFS repro on node 2 + connect trace

The stub-harness caveat above is now closed for the mount
layer: the whole netshare path was exercised against REAL
gio/GVFS on recomp-node2 (glib 2.88.0, gvfs 1.60.0 from the
extracted-deb prefix the fleet's earlier proof sessions used;
node 2 has no system gvfs packages and there is no sudo).
Real server, real credential (piped via stdin into env vars
only, never written to a file; all captured output scrubbed).

Findings, all PASS with the shipped code:
- Fresh mount (his KIO-only starting state): mount_share
  succeeded; the real gio prompt sequence is
  `Domain [WORKGROUP]: ` then `Password: ` and the
  pending-prompt answering handled it; the post-mount poll
  found the mount point.
- The REAL gvfs dir name lowercases + percent-encodes the
  share: `smb-share:server=192.168.0.102,share=ps2%20archive,user=billtookahit420`
  (the server's canonical share name is lowercase "ps2
  archive"). `_parse_gvfs_smb_name()` unquotes it, so all
  layers match. `gio mount -l` shows
  `Mount(0): ps2 archive on 192.168.0.102 -> smb://billtookahit420@192.168.0.102/ps2%20archive/`.
- Leftover app-created mount present: mount_share returns
  it via the fast path; calling gio again yields rc=2 with
  "Location is already mounted" (no prompts), which
  `_is_already_mounted` + the resolver also handle.
- Username-less URL mount: prompts are
  `User [billtookahit420]: ` -> Domain -> Password; dir
  name has NO user= part; handled too.
- Wrong password, fresh: gio re-asks Username/Domain/
  Password until the app's answer cap (8) is spent, then
  blocks; the app kills at its 60 s timeout and returns
  "(mount timed out)" — matches the family's documented
  known behaviour; a wrong password typed ONCE by a human
  at the dialog behaves the same way.
- The REAL dialog (App.add_game_network driven with
  Duane's exact share line incl. the /PS2 USA Collection
  subpath, fresh and leftover): both runs posted succeed,
  destroyed the dialog, and opened the picker at
  <mount>/PS2 USA Collection. (Node 2 has no python3-tk,
  so the dialog's widgets were faked while every app-level
  call — parse, mount, settings, filedialog, messagebox —
  was the real shipped code.)

So no mount-layer defect reproduces off Duane's rig; the
remaining divergence is specific to his Kubuntu session
(his glib/gvfs versions and real desktop session, or the
copy he launches). What his field run DID prove is a
diagnosability defect: the failure left no trace — empty
log pane, no readable error. Fixed here:

- `mount_share(..., log=None)` and `_gio_mount(...,
  log=None)`: a step trace (connect target, existing-mount
  hit/miss, the gio command, each prompt answered BY KIND,
  gio's exit + output tail, resolution outcome) is emitted
  to the caller's log. The password value is never logged;
  gio's pty echo of typed answers is scrubbed from the
  trace's output tail and from the rc!=0 error message.
  The dialog's worker now passes the app's main-window log
  (slug "network") and logs the final error line too, so
  the next field failure is readable in the Log pane.
- Prompt-answering logic itself is byte-for-byte the
  shipped behaviour; only observation was added.

Tests: test_netshare_already_mounted extended 9 -> 12
cases (trace logged without the password; failure trace;
the REAL lowercase-encoded mount-dir shape) — 12/12.
Existing suites re-run the documented way (full app copy
in /tmp/app-test; /tmp/ref-Play.sh re-fetched from HQ):
test_netshare 40/40 under xvfb-run. test_units 52/55 and
test_hardening 11/15 in THIS VM — the failures are this
VM's missing clang++ (compiler detection, glibc-shim
relink, and the conversion-path checks that need a
compiler), the same environment gap BUILD-NOTES recorded
before ("VM clang++ 18.1.3 present again" made them pass
that day); the mount-layer change touches none of those
paths. Zip NOT rebuilt — the parent agent rebuilds after
review.

## 2026-11-24 (evening) — GVFS-mount-free fallback (Duane approved)

Duane's field state never reproduced off his rig, and his own
read is "Probably is the whole gvfs" — his file manager is
Dolphin/KIO, which creates NO GVFS mount; the failing layer is
the app resolving a share to a GVFS FUSE mount DIRECTORY under
$XDG_RUNTIME_DIR/gvfs. The mount route stays the first attempt
(it works on other rigs), but when mount_share() fails on
Linux the Add-from-network dialog now falls back to direct
share access that never resolves a mount directory:

- `_gio_ensure_uri_mounted(uri, ...)`: establishes the GVFS
  mount behind the share with the proven `gio mount` pty path
  ("already mounted" = success), WITHOUT looking the mount up
  as a directory. This step is required: verified against the
  real server on node 2, `gio list` does NOT automount — an
  unmounted share answers "The specified location is not
  mounted". What is skipped is only the mount-DIRECTORY
  resolution, the layer that dies on Duane's rig.
- `_gio_list_uri(uri, ...)`: `gio list -a
  standard::type,standard::size` (real output shape, probed on
  node 2: `NAME\tSIZE\t(directory|regular)`, names raw) drives
  a small built-in browser dialog (folders descend, ".."
  ascends, subpath from the typed share line is honoured).
- `_gio_copy_uri(uri, dest, ...)`: `gio copy` fetches the
  picked file into local scratch (work/_network_stage/<ts>/);
  the fetched copy becomes the source the probe/conversion
  read, exactly like a local pick. A fetched .7z's copy is
  removed once its ISO is staged (the queue record names the
  staged ISO); fetched .iso/.zip copies stay as the record's
  source, like the .7z staging semantics. Local picks are
  never under _network_stage and never touched.
- Last resort, strictly opportunistic: `_smbclient_list` /
  `_smbclient_get` when gio URI access fails AND an smbclient
  binary happens to be installed. Credentials travel in a
  0600 temp auth file (deleted after), never on argv; the
  app works without smbclient.
- `_gio_mount` is now a thin wrapper over `_run_gio_pty`,
  the shared pty runner (mount/list/copy): same pending-
  prompt discipline (Domain -> Password -> Username, answer
  cap 8), same trace strings for the mount path, secrets
  scrubbed from every log line and error. Every fallback
  step feeds the same "network" trace, including which path
  served ("gio list served N entries", "fetched X via gio
  copy / smbclient (N bytes)").

Proof:
- Suites (full app copy in /tmp/app-test): already-mounted
  12/12, test_netshare 40/40 (xvfb-run), test_units 55/55,
  test_hardening 15/15 — this run the VM had the genuine
  /tmp/ref-Play.sh fixture restored from the filed
  Ghostbusters package on the server, and all previously
  environment-bound checks passed. A stub harness for the
  new functions (stub gio/smbclient on PATH) passes 9/9:
  list parsing, Domain->Password order for list prompts,
  copy success/failure scrubbing, smbclient ls parsing,
  password never on smbclient argv, auth file mode 600.
- Node 2 real-GVFS rig against the REAL server (credential
  via stdin into env vars only, never written; output
  scrubbed; driver ~/nsrepro/app2/drv_fallback.py):
  cold ensure-mount answered Domain->Password through the
  pty and mounted; gio list of the share root (4 entries)
  and of PS2 USA Collection (2,020 entries); gio copy of
  the smallest listed file (12,680,532 bytes) with size
  matching the listing. FORCED-FAILURE run: find_share_mount
  and list_smb_mounts sabotaged + gio answering "already
  mounted", so mount_share returned Duane's exact field
  error ("gio said already mounted, but no matching mount
  could be resolved") — the fallback still listed and
  copied. smbclient list + get against the real server
  also passed (same file, size match). Trace asserted
  password-free.
- Zip rebuilt the proven way: only the
  PS2NativeRecompTools/ps2_native_gui.py entry replaced in
  the trace-build zip (kept as PS2NativeRecompTools.zip
  .prev-trace); 365 entries, testzip clean, gui inside
  sha256 effb766f... == source. Suites re-run against the
  zip-extracted copy pass (one extraction artifact: Python
  zipfile drops the 7zz exec bit on extract — the archive
  itself stores 100755; chmod restores 40/40).

New zip: 5,307,365 B, sha256 e89fb40b... (gui effb766f...).
Still unproven: the fallback on Duane's actual rig — that
field run is the decider, as with every round of this saga.
Bin/cue support remains a separate owed item, not in this
pass.
