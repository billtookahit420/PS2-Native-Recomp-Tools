# PS2 Native Recomp Tools

A queue GUI for [ps2-native-recomp](https://github.com/exoteric33/ps2-native-recomp)
by exoteric33 (MIT), built on ps2-web-recomp. It wraps the workflow the
fleet proved by hand: queue a PS2 disc image you own, convert it, review
the captured frames and record an honest verdict, then package only what
the verdict proves.

The upstream tool is **pinned** at commit
`9b0eef16ecd5df327eb23eb5adaf844251c2f29f` (the commit Ghostbusters and
Code Veronica X were proven on) and ships in `tools/` next to the app.
Nothing is downloaded at conversion time except what the tool's own
setup script fetches (the wgpu-native library), exactly as upstream
does it.

## Requirements

On the machine that runs this tool:

```
sudo apt install -y python3-tk clang cmake ninja-build
```

- **clang++ 17 or newer** — hard requirement. The app detects it at
  startup and disables Convert with a plain explanation if it is
  missing or too old. There is no bundled compiler.
- cmake + ninja (the tool's setup builds its translators with them).
- A few GB of free space per game (a package runs roughly the size of
  the disc image plus build output).
- bash 4+, Python 3.
- Optional, for the Network Share button on Linux: the `gio` tool
  with the GVFS SMB backend (`gvfs`, `gvfs-backends` packages —
  present on most desktop installs). Without it, local picks are
  unaffected and the button explains what is missing.

On the machine that **plays** a converted game:

- Linux x86-64 with an AVX2 CPU, an SDL2 runtime, and a Vulkan driver.
- **clang++ 17 or newer as well**: code the conversion's explore run
  never reached is translated on the fly the first time you reach it.
  Without the compiler the game can stop at the first unreached area.
  Translation is cached, so each area only pays once.

## How to use it

1. `./run.sh` (or `python3 ps2_native_gui.py`).
2. **Add Game...** and pick a disc image you own (`.zip` or `.7z`
   containing an ISO, or a bare `.iso`). The app reads the boot program
   and serial from the disc's SYSTEM.CNF and shows the disc CRC32 and
   the game CRC before you commit. Confirm or edit the title and slug.
   A `.7z` pick is unpacked to local scratch first (the official 7-Zip
   standalone, bundled in `tools/7z/`, does the unpacking; the archive
   itself is never modified), and the queue ends up naming the
   extracted ISO under the game's own `work/<slug>/` folder — closing
   the Add dialog without adding deletes the staged copy.
   **Network Share...** is the same Add flow for images that live on
   an SMB share (a home server, a NAS): enter the share as
   `server/share`, `smb://server/share` or `\\server\share`, connect,
   and pick the image there. On Linux the app mounts the share itself
   with GVFS (`gio`), so the picker opens on a real folder; on Windows
   the UNC path is used directly. Server, share and username are
   remembered for next time — the password never is; it is used for
   that one mount only.
3. **Convert Queue** — conversions run one game at a time, built for
   piles: each game gets its own work tree under `work/<slug>/`, its
   full output streams into the Log tab and is kept in
   `work/<slug>/run.log` (select the row to read it, or **Open Log
   File**). A title that fails, crashes the tool, or hangs past the
   **per-title timeout** (default 4 hours, shown at the top) is killed,
   marked Failed with the reason, and the queue moves on to the next
   title — one bad title can never stall the pile. When the run ends, a
   one-line summary (converted / failed / still queued) appears under
   the queue. If the app is closed mid-pile, titles that were in flight
   come back as Queued on the next launch, never stuck.
4. **Frame Review** — when a conversion finishes, up to six milestone
   frames from the run are shown. Look at them and record the verdict:
   *Gameplay reached*, or *Stalled* (and where). Separately record
   whether saving is untested or proven by you. **Packaging stays
   locked until a verdict exists, and a stalled verdict never
   packages.**
5. **Package** — assembles `<slug>-ps2-native/` with the proven layout:
   the runner (relinked to a glibc 2.39 / GLIBCXX 3.4.31 floor with a
   small shim when needed, and only shipped after a frame-identical
   smoke test — if the shim cannot be applied, the package ships the
   original binary with a loud warning in its README instead of hiding
   it), `Play.sh`, a fresh pre-formatted memory card made with the
   tool's own `mcformat.py`, your disc image, and a `README.txt`
   generated only from the recorded facts and your verdict. The zip
   lands in `output/` and is verified (`zip -T` equivalent) with its
   size and sha256 recorded.

The queue persists in `queue.json` next to the app, so it survives a
restart. **Redo** moves a game's previous work folder aside to
`work/<slug>.bak-<timestamp>` — never deletes it — and re-queues the
game. **Remove** drops the queue entry; the work folder stays on disk.

The tool never uploads anything and never fetches game data. Filing a
finished zip anywhere is a separate, manual step.

## Layout of this folder

- `ps2_native_gui.py` — the app. Its pipeline functions are GUI-free
  and can be driven headlessly by importing the module.
- `tools/ps2-native-recomp/` — the pinned upstream tool (MIT; see its
  LICENSE).
- `tools/7z/` — the official standalone 7-Zip command-line extractor
  (`7zz`, with its license), used only to unpack `.7z` disc images
  the user picks. On Windows no binary is bundled; a system 7-Zip
  install is used instead.
- `glibc-shim.c` — the fleet-proven symbol shim used by the packager.
- `work/`, `output/`, `queue.json` — created at runtime.

## Credits

ps2-native-recomp by exoteric33 (MIT), built on ps2-web-recomp.
This GUI is part of Duane's recomp tools family.
