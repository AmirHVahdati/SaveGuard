# SaveGuard 🛡️

**A launcher-free game save backup utility.** Point it at a game's save folder
and it keeps a restorable, timestamped copy of it, then tells you when the game
has changed since you last backed up. No Steam Cloud, no launcher, no account.

Built for the games that have neither — or for players who simply don't trust a
background sync to be the only copy of their saves.

| | |
|---|---|
| **Version** | 1.0.0 |
| **License** | [MIT](LICENSE) |
| **Platforms** | Windows, Linux (including SteamOS and CachyOS) |
| **Runtime dependencies** | [PySide6](https://doc.qt.io/qtforpython/) only |

---

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Day-to-day use](#day-to-day-use)
- [How change detection works](#how-change-detection-works)
- [Running the tests](#running-the-tests)
- [Building a Windows installer](#building-a-windows-installer)
- [Notes](#notes)
- [License](#license)

---

## Features

### Track any game

Add a game by pointing at the folder it writes saves to and the folder you want
backups kept in. Nothing about SaveGuard is tied to a store or launcher — if it
saves to a folder, SaveGuard can back it up.

### Automatic game discovery

SaveGuard scans the places games keep their saves and offers the ones you aren't
already tracking. This runs once automatically the first time you open the app,
and any time you click **Scan Games**. It never adds anything on its own: you
review the results and pick what to keep.

Scanning always happens on a background thread, so the window never freezes.

| Platform | Locations scanned |
|---|---|
| **Windows** | `Documents\My Games`, `Saved Games`, `Documents`, `AppData\Local` |
| **Linux** | `~/.local/share`, `~/.config` |
| **Steam** (any OS) | Local `userdata` cloud-save folders, across native, Flatpak and Snap installs |
| **Proton** (any OS) | Windows game prefixes — Proton mirrors a real Windows user folder internally, so a Proton game is detected as accurately as it would be on native Windows |

Steam and Proton results are only shown when the real game name can be resolved
from a local Steam file, never as a generic `Steam App <id>`, and Steam's own
internal components are filtered out of the results.

### One-click, restorable backups

Every backup is a separate timestamped `.zip` (`2026-09-11_20-30-00.zip`) written
into the destination folder you chose. You can use any zip tool to open an old
one, and nothing you do in SaveGuard overwrites a backup you already have.

### Change detection

Each time the app opens, it re-reads every tracked save folder and compares it
against the last backup. Anything new, removed or modified flags the game with a
**Changes detected** badge and raises a banner, so you know a backup is worth
taking before you close the game.

### Settings

Reachable from the **Settings** button in the header:

- **Compression level** — smaller archives take longer. The top *Maximum* setting
  uses LZMA instead of DEFLATE; see [Notes](#notes) before using it.
- **Backups to keep per game** — unlimited by default, or a number. Once a game
  exceeds it, the oldest archives are pruned on the backup thread, immediately
  after a new backup is verified.
- **Default location for scanned games**, plus an **About** tab with version and
  config path information.

### Modern dark UI

Card-based layout, status badges, and a colour tag per game so a large library
stays readable.

### Everything local

No cloud, no accounts, no telemetry. All of SaveGuard's data lives in
`~/.saveguard/` and is written atomically, so an interrupted write can't leave
it half-written. The exact path is shown in **Settings → About**.

---

## Quick start

Requires Python 3.9 or newer (3.11+ is what the Windows build is tested against)
and PySide6, which is the only runtime dependency.

```bash
git clone <repository-url>
cd saveguard

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python main.py
```

On Windows the activation line is `.venv\Scripts\activate`; on Linux and macOS
it is `source .venv/bin/activate`.

---

## Day-to-day use

1. Click **Add Game**, name it, and select:
   - the **save folder** the game writes to — e.g.
     `%USERPROFILE%\Saved Games\CD Projekt Red\Cyberpunk 2077`
   - a **backup destination** — e.g. a folder on an external drive
2. Click **Backup Now** on that game's card to compress the current save state
   into a new timestamped archive in the destination.
3. Next time you launch SaveGuard after a play session, it re-checks every
   tracked folder. If anything changed, the card shows **Changes detected** and a
   banner offers to back up.
4. From the card menu, **Open Backups** jumps straight to the backup folder.
   **Rescan** re-checks for changes on demand, and **Backup All** (header or
   banner) backs up every game with pending changes in one click.

Backups run on a background thread, one game at a time, so a large library never
blocks the window.

---

## How change detection works

Each save folder is fingerprinted into a snapshot of every file's size and
modification time. A game is flagged when the current snapshot differs from the
one recorded by the last backup.

This is deliberately not a content hash. Hashing was measured at roughly 44× the
cost of a stat-only walk — 319 ms versus 7 ms on a 52 MB, 603-file save folder —
and the walk runs once per game on the UI thread at startup. Size plus full
modification-time precision detects every normal save write while keeping a large
library's startup under a second.

Consequences worth knowing:

- Restoring an old backup changes the files' timestamps, which correctly reads as
  a change and prompts a fresh backup.
- A file whose contents change without its size *and* timestamp changing is not
  detected. No normal game save does this.

---

## Running the tests

```bash
pip install -r requirements.txt
python -m pytest
```

851 tests, roughly 9 seconds, no arguments needed. The suite runs headless and is
safe to run on a real machine: it opens no windows, never touches your real
`~/.saveguard/` folder, and never makes a network request.

See [BUILDING.md](BUILDING.md#running-the-tests) for what the suite covers, the
fixtures that guarantee that isolation, and how to write new UI tests against it.

---

## Building a Windows installer

PyInstaller does not cross-compile, so a Windows `.exe` has to be built on a
Windows machine. Full instructions are in [BUILDING.md](BUILDING.md).

---

## Notes

**Compression levels.** Levels 0–8 use DEFLATE, which every zip tool
understands. Level 9 (*Maximum*) uses LZMA, which produces meaningfully smaller
archives but is not recognised by minimal zip utilities — 7-Zip and most modern
tools handle it fine. If a *Maximum* backup will not open with your usual tool,
that is why.

**Removing a game** stops tracking it and deletes nothing. Archives already
written stay exactly where they are.

**Updates.** The app checks GitHub Releases once a day at most, in the background,
and only asks when a newer version exists. No update is ever downloaded or
installed without you saying yes.

---

## License

MIT — see [LICENSE](LICENSE).