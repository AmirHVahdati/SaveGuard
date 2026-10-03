# SaveGuard 🛡️

A lightweight desktop utility for PC gamers who don't use Steam Cloud (or any
launcher) — track any game's save folder and back it up manually, with
automatic change detection. Works on Windows and Linux (including SteamOS
and CachyOS).

**Version 1.0** · MIT licensed — see [LICENSE](LICENSE).

## Features

- **Add any game** by pointing to its save folder and a backup destination — works with any game, any launcher, or no launcher at all.
- **Automatic game discovery** — SaveGuard scans common save locations and suggests games it finds that aren't tracked yet. This runs once automatically the first time you open the app, and any time you click **"Scan Games"**. Scanning always happens on a background thread, so the app never freezes — and nothing is added automatically: you review and pick which results to track from a checklist first. Locations scanned:
  - **Windows**: `Documents\My Games`, `Saved Games`, `Documents`, `AppData\Local`
  - **Linux** (including SteamOS, CachyOS, and other Arch/Fedora-based gaming distros): `~/.local/share`, `~/.config`
  - **Steam**, on any OS: its local `userdata` cloud-save folders, checking native, Flatpak, and Snap install locations, plus **Proton game prefixes** — a Windows game played through Proton gets the same accurate detection as it would on native Windows, since Proton mirrors a real Windows user folder internally
  - Every Steam/Proton result is only shown when its real name can be resolved from a local Steam file — never as a generic "Steam App `<id>`", and Steam's own internal components (which aren't games) are filtered out
- **One-click backup** — compresses the save folder into a timestamped `.zip` archive.
- **Settings** — opens from the "Settings" button in the header:
  - **Compression level** for future backups (fastest/largest to smallest/slowest). The top "Maximum" setting uses a stronger algorithm (LZMA) than the rest of the scale for noticeably smaller files — see the note below.
  - **Backups to keep per game** — set to **Unlimited** (the default) to never delete old backups, or to a specific number to automatically keep only that many most recent backups. Once a game has more than that, the oldest ones are deleted right after each new backup finishes, on the same background thread — so this never blocks the UI, even with hundreds of existing backups.
  - **Default location for scanned games**, and an **About** tab with version info.
- **Automatic change detection** — every time you open SaveGuard, it rescans each save folder and compares it against the last backup. If anything changed (new save, modified file, etc.), the game is flagged with a **"Changes detected"** badge and an alert banner.
- **Backup history** — every backup is its own timestamped `.zip` archive, so you never overwrite an old save and can always go back to a previous one.
- **Modern dark UI** — card-based layout, status badges, a custom color-tag picker to tell games apart at a glance, smooth accents throughout.
- **No cloud, no accounts** — everything is local. Config lives at `~/.saveguard/` (see **Settings → About** in the app for the exact path).
- **Support button** — a "♥ Support" link in the header for anyone who wants to support development. *(Note for whoever's maintaining this: the URL is a placeholder in `app/ui/main_window.py` — search for `SUPPORT_URL` and swap in the real donation link once it exists.)*

## Setup

Requires Python 3.9+ and PySide6. Works on Windows and Linux (including
SteamOS, CachyOS, and other gaming-focused distros); macOS is untested but
should work for manual game tracking (automatic Steam/Proton discovery is
Windows/Linux-focused).

```bash
pip install -r requirements.txt
python main.py
```

Want a standalone Windows `.exe` and installer instead of running from source? See [BUILDING.md](BUILDING.md).

## Running the tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The suite runs headless and is safe to run on your real machine: it never opens
a window, never touches your real `~/.saveguard/` folder, and never makes a
network request. See [BUILDING.md](BUILDING.md#running-the-tests) for details.

## How it works

1. Click **"Add Game"**, give it a name, and select:
   - The **save folder** the game writes to (e.g. `%USERPROFILE%\Saved Games\CD Projekt Red\Cyberpunk 2077`)
   - A **backup destination** folder (e.g. a folder on an external drive or cloud-synced folder)
2. Click **"Backup Now"** on a game card to compress the current save state into a new timestamped `.zip` archive inside your chosen destination (e.g. `2026-09-11_20-30-00.zip`). Compression keeps backups small without needing any extra tools — just double-click the archive in your file explorer to open or extract it.
3. Next time you launch SaveGuard (e.g. after a play session), it automatically rescans every tracked save folder. If the save data changed since the last backup, the card shows a **"Changes detected"** badge and a banner appears prompting you to back up again.
4. Use **"Open Backups"** to jump straight to the backup folder in your file explorer, or **"Rescan"** anytime to manually re-check for changes. Use **"Backup All"** (in the header or the alert banner) to back up every game with pending changes in one click.

## Notes

- Change detection compares file size + modification time for every file in the save folder — fast and reliable without needing to hash large save files, and it never has to open the backup archive to work: it always compares against the live save folder.
- Backups are stored as compressed `.zip` archives (one per backup) rather than plain folder copies, to keep disk usage down. Compression levels 0-8 use the standard DEFLATE algorithm (opens with anything — Explorer, Finder, any zip tool). Level 9 ("Maximum") switches to LZMA for meaningfully smaller files, which is a standard part of the zip format but isn't recognized by every minimal zip tool — 7-Zip and most modern archive tools handle it without issue, but if you ever need to open a "Maximum"-level backup manually and your usual tool can't read it, try 7-Zip.
- Removing a game from SaveGuard only stops tracking it; it never deletes any backup files already created.
- All of SaveGuard's own data (tracked games, settings, and a small crash log if anything ever goes wrong) lives in `~/.saveguard/` and is written atomically — an interrupted write (crash, power loss) can't leave that data corrupted. Find the exact path anytime from **Settings → About**.
