"""Discovers likely game save folders across Windows AND Linux (including
SteamOS, ChimeraOS, Bazzite, CachyOS and other Arch/Fedora-based gaming
distros).

Windows locations:
  - Documents\\My Games\\<Game>              (high confidence)
  - Saved Games\\<Game>                       (high confidence)
  - Documents\\<Game>                         (low confidence — Documents
                                                also holds lots of non-game
                                                folders, so we exclude a
                                                blocklist of common ones)
  - AppData\\Local\\<Game>\\...\\Saves\\...    (low confidence — only
                                                surfaced if a nested folder
                                                name looks save-related,
                                                otherwise AppData is far too
                                                noisy to scan blindly)

Linux locations:
  - ~/.local/share/<Game>  (XDG_DATA_HOME)    (low confidence, blocklist-filtered
                                                — this is where native Linux
                                                games conventionally put saves,
                                                directly in the game's own folder
                                                rather than a nested "Saves" dir)
  - ~/.config/<Game>       (XDG_CONFIG_HOME)  (low confidence, blocklist-filtered)

Steam (both OSes — a Steam install is looked for in the OS-appropriate
places, native Linux install, Flatpak, and Snap included):
  - .../userdata/<SteamID>/<AppID>/remote/    (native cloud-synced saves —
                                                high confidence if the app's
                                                real name resolves from a
                                                local appmanifest, otherwise
                                                skipped entirely — see below)
  - .../steamapps/compatdata/<AppID>/pfx/drive_c/users/steamuser/...
    Proton runs every Windows game inside a private Wine prefix that mirrors
    a real Windows user folder — so a Windows game played through Proton on
    Linux gets the exact same Documents\\My Games / Saved Games / AppData
    heuristics as native Windows, just rooted inside that prefix. Only
    surfaced when the AppID resolves to a real name (same policy as native
    Steam entries below), labeled "Proton" so the source is clear.

On Steam AppID naming: userdata contains numeric folders for things besides
games too (Steam's own internal client components, e.g. AppID 5 and 7 are
not games at all). Rather than maintain an exhaustive blocklist of every
such ID, candidates are only surfaced when the AppID resolves to a real name
via a local appmanifest_<id>.acf file — Steam doesn't generate one of those
for its own internal IDs, so requiring a resolved name is simultaneously how
we identify a genuine game *and* how unrelated folders get filtered out,
rather than showing confusing, unlabeled "Steam App <id>" entries.

This module never adds anything itself — it only returns candidates. The UI
is responsible for showing them to the user for review before tracking any
of them, since heuristic folder-guessing will occasionally be wrong.

All the actual scanning functions take an explicit root path, so they're
testable on any OS without needing real Windows/Linux environment variables
or an actual Steam install.
"""

import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Set

from PySide6.QtCore import QThread, Signal


@dataclass
class ScanCandidate:
    name: str
    save_path: str
    source: str        # human-readable origin, e.g. "My Games", "Steam", "Proton"
    confidence: str     # "high" or "low" — used to pre-check/uncheck in the review dialog


# Common non-game folders that show up directly under Documents — scanning
# Documents for game folders means we have to actively exclude these or
# they'd show up as false "games" on almost every scan. Also doubles as the
# exclude list inside a Proton prefix's emulated Documents folder.
_DOCUMENTS_EXCLUDE = {
    "my games", "my pictures", "my music", "my videos", "outlook files",
    "custom office templates", "onedrive", "sharepoint", "github",
    "visual studio 2019", "visual studio 2022", "power bi desktop",
    "wechat files", "bluej", "arduino", "zoom", "fiddler2",
    "sql server management studio", "adobe", "corel", "autodesk",
    "obsidian", "notion", "minecraft", "steam", "epic games", "ubisoft",
    "ea app", "xbox games", "windows", "desktop", "downloads", "links",
    "iisexpress", "megascans library", "megascans", "my websites", "my web sites",
    "kilo", "clash verge", "clash.verge", "node_modules", "wordpress",
    "laravel", "django", "react", "angular", "vue", "next.js", "nuxt",
    "python projects", "source", "src", "projects", "workspace",
    "godot projects", "unity projects", "unreal projects", "unreal",
    "blender", "dropbox", "googledrive", "steam", "skidrow",
    "rainmeter", "sound recordings", "zarf", "saved games",
    "sports interactive", "paradox interactive", "cd projekt", "cd projekt red",
}

# Game publishers that store saves in Documents\<Publisher>\<Game>\ format.
# Each subfolder of these folders is treated as a game candidate.
# (Unlike generic Documents folders which are low-confidence, these are
# publisher-specific paths we can be confident about.)
_PUBLISHER_SAVE_FOLDERS = {
    "capcom",        # Documents\CAPCOM\OnimushaWotS\backup
    "koei tecmo",    # Documents\KoeiTecmo\<game>\
    "koeitecmo",
    "koei",
    "tecmo",
    "konami",        # Documents\Konami\<game>\
    "square enix",   # Documents\SquareEnix\<game>\
    "squareenix",
    "square enix holdings",
    "bandai namco",  # Documents\Bandai Namco\<game>\
    "bandainamco",
    "bandai",
    "namco",
    "bethesda",      # Documents\Bethesda\<game>\
    "arkane studios",  # Saved Games\Arkane Studios\<game>\
    "id software",     # Saved Games\id Software\<game>\
    "gamefreak",      # Saved Games\Gamefreak\<game>\
    "sega",
    "capcom entertainment",
    "cd projekt",    # Documents\CD Projekt\<game>\
    "cd projekt red",
    "paradox interactive",  # Documents\Paradox Interactive\<game>\
    "paradox",
    "sports interactive",  # Documents\Sports Interactive\<game>\
    "warner bros. interactive entertainment",
    "warner bros",
    "warner bros.",
    "electronic arts",
    "ea",
    "take 2 interactive",
    "take 2",
    "activision blizzard",
    "activision",
    "blizzard",
    "thq nordic",
    "thq",
    "deep silver",
    "gearbox entertainment",
    "gearbox",
    "2k games",
    "2k",
    "miHoYo",        # Genshin, Honkai
    "HoYoverse",
    "Honkai",
    "genshin impact",
}

# Common non-game folders directly under AppData\Local — mostly OS and
# browser/utility internals that would otherwise flood the results. Also
# doubles as the exclude list inside a Proton prefix's emulated AppData.
# Includes common non-game publishers too (creative, productivity, and
# communication software) since those frequently use a "Profile" or
# similar folder that would otherwise pass the save-folder keyword check.
_APPDATA_EXCLUDE = {
    "microsoft", "google", "mozilla", "temp", "packages", "programs",
    "microsoftedge", "connecteddevicesplatform", "d3dscache",
    "crashdumps", "comms", "elevateddiagnostics", "nvidia", "nvidia corporation",
    "amd", "intel", "diagnostics", "downloaded installations", "energyui",
    "adobe", "autodesk", "jetbrains", "docker", "docker desktop", "slack",
    "spotify", "zoom", "1password", "postman", "notion", "obs-studio",
    "obs studio", "virtualbox", "vmware", "dropbox", "box", "evernote",
    "skype", "microsoft teams", "webex", "citrix", "teamviewer", "anydesk",
    "malwarebytes", "ccleaner", "winrar", "7-zip", "vlc", "corel",
    "roblox", "epic games", "steam", "ubisoft", "ea app", "xbox",
}

# Common non-game folders directly under ~/.local/share on a typical Linux
# desktop (GNOME, KDE Plasma — including SteamOS's and CachyOS's default
# desktops) — filtered out for the same reason as the AppData blocklist.
# Steam itself and Lutris are excluded here because they're handled by their
# own dedicated, deeper scanning logic instead of a flat folder guess.
_XDG_DATA_EXCLUDE = {
    "trash", "flatpak", "systemd", "dconf", "pulse", "gvfs-metadata",
    "keyrings", "webkitgtk", "baloo", "akonadi", "nvidia", "vulkan",
    "mesa_shader_cache", "fontconfig", "icons", "themes", "sounds",
    "applications", "mime", "dbus-1", "gnome-shell", "gnome-boxes",
    "tracker3", "zeitgeist", "evolution", "gvfs", "recently-used.xbel",
    "steam", "lutris", "wineprefixes", "man", "backgrounds",
    "spotify", "slack", "zoom", "dropbox", "obs-studio", "vlc",
    "teamviewer", "anydesk", "1password", "kilo", "clash-verge", "clash_verge",
}

# Common non-game folders directly under ~/.config on a typical Linux
# desktop, for the same reason as the XDG data blocklist above.
_XDG_CONFIG_EXCLUDE = {
    "autostart", "gtk-3.0", "gtk-4.0", "dconf", "pulse", "systemd",
    "kdedefaults", "plasma-workspace", "menus", "fontconfig",
    "environment.d", "gnome-session", "goa-1.0", "ibus", "procps",
    "nvidia", "vulkan", "pip", "git", "gh", "htop", "neofetch",
    "steam", "lutris", "user-dirs.dirs", "user-dirs.locale",
    "kwinrc", "kglobalshortcutsrc", "khotkeysrc", "baloofilerc",
    "mimeapps.list", "spotify", "slack", "zoom", "dropbox", "obs-studio",
    "vlc", "teamviewer", "anydesk", "1password", "kilo", "clash-verge",
    "cagent", "cagent-updater",
}

# Nested folder names (anywhere within a couple levels) that suggest "this
# is where the save data actually lives" inside a noisy per-game data folder
# (AppData\Local on Windows, or the same idea inside a Proton prefix).
#
# Deliberately does NOT include bare "profile"/"profiles" — that was tried
# and caused real false positives (e.g. Adobe apps use a "Profile" folder
# for their own settings, not game saves; so do several browsers and chat
# clients). Every keyword here is specifically save-related instead.
_SAVE_FOLDER_KEYWORDS = {
    "save", "saves", "savegame", "savegames", "saved games", "savedgames",
    "savedata", "save_data", "save data", "gamesave", "gamesaves",
    "player", "players",
}


def _list_subdirs(path: str) -> List[str]:
    try:
        return [
            entry.path for entry in os.scandir(path)
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")
        ]
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Piracy/emulator save scanners
#
# Many games (both cracked and legitimately played through emulators like
# Goldberg/LibreVR) store saves in non-standard locations. These scanners
# look for those specific folders so the user doesn't have to add them
# manually. Each entry maps a group name to its known save directories.
# ---------------------------------------------------------------------------

def _list_subdirs_hidden(path: str) -> List[str]:
    """Like _list_subdirs but includes hidden directories (names starting with .).
    Needed for folders like %APPDATA%/.1911 (Razor1911 saves)."""
    try:
        return [
            entry.path for entry in os.scandir(path)
            if entry.is_dir(follow_symlinks=False)
        ]
    except OSError:
        return []


# Known piracy/emulator save locations that follow a simple structure:
# the base folder contains per-game subfolders.
# Format: (env_var_name, relative_path, group_label, include_hidden_dirs)
_PIRACY_SAVE_LOCATIONS = [
    ("APPDATA", "GSE Saves", "Goldberg Fork", False),
    ("APPDATA", "Goldberg SteamEmu Saves", "Goldberg", False),
    ("PUBLIC", os.path.join("Documents", "OnlineFix"), "OnlineFix", False),
    ("APPDATA", "EMPRESS", "EMPRESS", False),
    ("APPDATA", os.path.join("Steam", "CODEX"), "CODEX", False),
    ("LOCALAPPDATA", "skidrow", "SkidRow", False),
    ("APPDATA", "SmartSteamEmu", "SmartSteamEmu", False),
    ("APPDATA", ".1911", "Razor1911", True),  # hidden dot-folder
    ("APPDATA", "FLT", "FLT", False),
]


def _resolve_env_path(env_var: str, relative_path: str = "") -> Optional[str]:
    """Resolve a path using an environment variable (APPDATA, LOCALAPPDATA, etc.).
    Returns None on non-Windows platforms or if the env var is unset."""
    if sys.platform.startswith("win"):
        base = os.environ.get(env_var)
        if base:
            return os.path.join(base, relative_path) if relative_path else base
    return None


# Folder names that appear inside game save directories but definitely aren't
# save folders (used in piracy/emulator scanners to filter out config-only folders)
_NON_GAME_SUBFOLDERS = {
    "settings", "config", "configs", "logs", "log", "readme", "readmes",
    "screenshots", "shader", "shaders", "texturecache", "cache",
    "crashdumps", "crash dumps", "backup", "backups",
    "metadata", "temp", "tmp", "benchmarkresults", "editordata",
    "exportedteams", "languages", "skins", "tactics",
    "profiles", "profile",
}

# Known non-game folders that appear under My Games / Saved Games
# and should be skipped by scan_flat_style (they're engine/system folders,
# not per-game saves).
_NON_GAME_FLAT_FOLDERS = {
    "unrealengine", "unrealengine4", "unrealengine5", "ue4", "ue5",
    "unity", "unitycache", "unity hub",
    "modorganizer", "mod manager", "modmanager",
    "launcher", "launchers", "bethesdaprebypass",
    "common", "shaders", "shadercache",
}

# Known non-game AppIDs that sometimes appear in Steam/Goldberg user data
_NON_GAME_APPIDS = {
    "0", "4", "5", "7", "8", "1007", "228980",  # Steam internal components
    "settings",  # Goldberg config folder
}


def _find_steam_root() -> Optional[str]:
    """Look for a Steam installation, accounting for common install locations
    on both Windows and Linux (including Flatpak/Snap paths)."""
    if sys.platform.startswith("win"):
        candidates = [
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
            os.environ.get("PROGRAMDATA", ""),
        ]
        for base in candidates:
            if not base:
                continue
            steam = os.path.join(base, "Steam")
            if os.path.isdir(os.path.join(steam, "steamapps")):
                return steam
    else:
        candidates = [
            os.path.join(os.path.expanduser("~"), ".local", "share", "Steam"),
            os.path.join(os.path.expanduser("~"), ".steam", "steam"),
            "/var/lib/flatpak/app/com.valvesoftware.Steam/x86_64/active",
        ]
        for steam in candidates:
            if os.path.isdir(os.path.join(steam, "steamapps")):
                return steam
    return None


def _resolve_appid_name(folder_name: str, app_names_cache: Optional[dict] = None) -> Optional[str]:
    """If folder_name is a numeric Steam AppID, try to resolve it to the real
    game name using local Steam appmanifest files. Returns None if the folder
    name isn't an AppID or no name could be resolved."""
    if not folder_name.isdigit():
        return None
    if folder_name in _NON_GAME_APPIDS:
        return None
    if app_names_cache is None:
        steam_root = _find_steam_root()
        if not steam_root:
            return None
        app_names_cache = _read_steam_app_names(steam_root)
    return app_names_cache.get(folder_name)


def scan_piracy_saves() -> List[ScanCandidate]:
    """Scan known piracy/emulator save directories for game folders.

    Each supported group (Goldberg, CODEX, SkidRow, etc.) stores saves in a
    well-known directory — we look there, treat each subfolder as a game, and
    resolve its actual save path via _resolve_game_folders (same nesting
    logic used for 'My Games' and publisher folders).

    Numeric folder names (common in Goldberg/CODEX/FLT) are treated as Steam
    AppIDs and resolved to real game names where possible via local Steam
    appmanifest files.

    Uses environment variables internally (APPDATA, LOCALAPPDATA, PUBLIC) —
    returns an empty list on non-Windows platforms.
    """
    results = []

    steam_root = _find_steam_root()
    app_names_cache = None
    if steam_root:
        app_names_cache = _read_steam_app_names(steam_root)

    for env_var, rel_path, group_label, include_hidden in _PIRACY_SAVE_LOCATIONS:
        base = _resolve_env_path(env_var, rel_path)
        if not base or not os.path.isdir(base):
            continue

        subdirs_fn = _list_subdirs_hidden if include_hidden else _list_subdirs
        game_subs = subdirs_fn(base)

        for game_folder in game_subs:
            folder_name = os.path.basename(game_folder)

            # Skip non-game folders (settings, config, etc.)
            if folder_name.lower() in _NON_GAME_SUBFOLDERS:
                continue

            # Skip known non-game numeric AppIDs (Steam internal components)
            if folder_name.isdigit() and folder_name in _NON_GAME_APPIDS:
                continue

            resolved = _resolve_game_folders(game_folder, max_depth=3)
            for save_path in resolved:
                if _has_any_files(save_path) and not _is_empty_playtime_only(
                    save_path
                ):
                    # If folder is a numeric AppID, try to resolve the real
                    # game name from local Steam appmanifests.
                    game_name = _resolve_appid_name(
                        folder_name, app_names_cache
                    ) or folder_name
                    results.append(ScanCandidate(
                        name=game_name,
                        save_path=save_path,
                        source=f"{group_label} (piracy)",
                        confidence="high",
                    ))
    return results


def scan_tenoke_and_universe(
    search_roots: Optional[List[str]] = None,
) -> List[ScanCandidate]:
    """TENOKE and UniverseLAN store save folders with a 'TENOKE' or
    'UniverseLANData' marker directory that can appear under any game
    installation directory. We search common game library roots for these
    markers and extract the parent game folder.
    """
    results = []
    if not sys.platform.startswith("win"):
        return results

    if search_roots is None:
        # Common game installation locations on Windows
        search_roots = []
        for letter in "DEFGHI":
            search_roots.append(f"{letter}:\\Games")
            search_roots.append(f"{letter}:\\GOG Games")
        search_roots.append("C:\\Games")

    marker_dirs = {"TENOKE", "UniverseLANData", "UniverseLAN"}
    seen_paths = set()

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root):
            # Only check 2 levels deep to keep this fast
            depth = dirpath[len(root):].count(os.sep)
            if depth > 2:
                dirnames[:] = []
                continue

            for d in dirnames:
                if d in marker_dirs:
                    game_path = os.path.join(dirpath, d)
                    if game_path in seen_paths:
                        continue
                    seen_paths.add(game_path)
                    if _has_any_files(game_path):
                        game_name = os.path.basename(os.path.dirname(game_path)) or os.path.basename(dirpath)
                        group = "TENOKE" if "TENOKE" in d else "UniverseLAN"
                        results.append(ScanCandidate(
                            name=game_name,
                            save_path=game_path,
                            source=f"{group} (piracy)",
                            confidence="high",
                        ))

    return results


def _is_empty_playtime_only(path: str) -> bool:
    """True if `path` contains only `playtime.txt` files (Goldberg
    Steam Emulator tracking files). These are tiny (usually 4 bytes)
    and not actual save data — the real saves are stored elsewhere.
    If this is the only content in a folder, skip it as a candidate."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False):
                    if entry.name.lower() == "playtime.txt":
                        continue
                    return False  # found a non-playtime file
                elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                    # Recurse into subfolders — if any non-playtime file exists
                    # anywhere, this isn't playtime-only.
                    if not _is_empty_playtime_only(entry.path):
                        return False
            return True  # only playtime.txt files found (or empty)
    except OSError:
        return False


def _has_any_files(path: str, max_check: int = 1) -> bool:
    """Cheap existence check — stops at the first file found instead of
    walking the whole tree, since we only need a yes/no answer."""
    stack = [path]
    found = 0
    checked = 0
    while stack:
        current = stack.pop()
        checked += 1
        if checked > 500:
            return True  # safety: if we've checked many dirs without finding a
                         # file, the folder is likely non-game; bail out early
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                        found += 1
                        if found >= max_check:
                            return True
                    elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        stack.append(entry.path)
        except OSError:
            continue
    return False


def _has_direct_files(path: str) -> bool:
    """True if `path` contains at least one regular file directly inside it
    — NOT counting files in subfolders. This is the signal used to tell an
    actual save folder apart from a folder that's just grouping other
    folders (most commonly a publisher/studio name used as an extra level,
    e.g. My Games\\CD Projekt Red\\Cyberpunk 2077\\ instead of just
    My Games\\Cyberpunk 2077\\)."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                    return True
    except OSError:
        pass
    return False


def _looks_like_save_folder(name: str) -> bool:
    """Heuristic: returns True if a folder name suggests it holds save data."""
    lower = name.lower()
    save_keywords = {"save", "saves", "savegame", "savegames", "saved games",
                     "savedata", "save_data", "save data", "gamesave", "gamesaves",
                     "player", "players", "userdata", "user-data",
                     "profil", "savefiles", "save game", "gamedata",
                     "saved", "autosave", "autosaves", "quick saves"}
    non_save_keywords = {"config", "configs", "logs", "log", "screenshots",
                         "shader", "shaders", "texturecache", "cache", "metadata",
                         "temp", "tmp", "backup", "backups", "benchmark",
                         "benchmarkresults", "editordata", "exportedteams",
                         "languages", "skins", "tactics", "profiles",
                         "crashdumps", "crashdump", "settings"}
    if lower in non_save_keywords or any(nk in lower for nk in non_save_keywords):
        return False
    return any(kw in lower for kw in save_keywords)


def _resolve_game_folders(path: str, max_depth: int = 2, _depth: int = 1) -> List[str]:
    """Given one direct child of a 'My Games'/'Saved Games'-style root,
    returns the path(s) that actually represent a game's save folder.

    Usually that's just `path` itself. But some publishers nest an extra
    level (My Games\\<Publisher>\\<Game>\\) — detected here by the folder
    being a known publisher name, which means treating its OWN name as "the
    game" would show the publisher's name instead of the actual game (this
    was a real, reported bug: games were showing up labeled by their studio).
    When that's detected, each inner folder is resolved instead — correctly
    recovering the real per-game names, and handling a publisher folder that
    groups more than one of their games the same way (each becomes its own
    candidate).

    When descending into subfolders, prefers save-related folders (e.g.
    'SaveGames') over generic ones (e.g. 'Config', 'Logs') to avoid
    duplicate candidates for the same game."""
    folder_name = os.path.basename(path).lower()
    if folder_name in _PUBLISHER_SAVE_FOLDERS:
        # Publisher/studio folder — always descend, never return the
        # publisher folder itself as a candidate.
        subs = _list_subdirs(path)
        resolved = []
        for sub in subs:
            if os.path.basename(sub).lower() in _NON_GAME_SUBFOLDERS:
                continue
            resolved.extend(_resolve_game_folders(sub, max_depth, _depth + 1))
        return resolved
    if _has_direct_files(path) or _depth > max_depth:
        return [path]
    subs = _list_subdirs(path)
    if not subs:
        return []  # genuinely empty — nothing to report

    # If any subfolder looks like a save folder, prefer those over others
    save_subs = [s for s in subs if _looks_like_save_folder(os.path.basename(s))]
    if save_subs:
        # Recurse into save-related subfolders only
        resolved = []
        for sub in save_subs:
            resolved.extend(_resolve_game_folders(sub, max_depth, _depth + 1))
        return resolved
    else:
        # No subfolder looks like a save folder — check if the path itself
        # has files (it might be a game with saves directly) or has subdirs
        # that contain saves deeper. If the path has any files anywhere,
        # return it as the candidate.
        if _has_any_files(path):
            return [path]
        # Fallback: recurse into all subdirs but only return paths that have files
        resolved = []
        for sub in subs:
            resolved.extend(_resolve_game_folders(sub, max_depth, _depth + 1))
        return resolved


def scan_flat_style(root: str, source_label: str) -> List[ScanCandidate]:
    """For 'My Games' / 'Saved Games' style roots: every direct subfolder is
    normally one game, at high confidence — these locations are, by Windows
    convention, only ever used for per-game save folders. Handles the
    publisher-name nesting some games use via _resolve_game_folders, so the
    candidate is always named after the actual game, not its publisher."""
    results = []
    for sub in _list_subdirs(root):
        if os.path.basename(sub).lower() in _NON_GAME_FLAT_FOLDERS:
            continue
        sub_name = os.path.basename(sub)
        is_publisher = sub_name.lower() in _PUBLISHER_SAVE_FOLDERS
        for resolved_path in _resolve_game_folders(sub):
            # For publisher folders, the resolved path goes deeper into a
            # game subfolder — use that folder's name, not the publisher's.
            name = os.path.basename(resolved_path) if is_publisher else sub_name
            results.append(ScanCandidate(
                name=name, save_path=resolved_path,
                source=source_label, confidence="high",
            ))
    return results


def _get_documents_paths(home: str) -> List[str]:
    """Returns all possible Documents paths — standard plus OneDrive redirect.
    On Windows, the Documents folder is frequently redirected to OneDrive,
    so we check both locations."""
    paths = [os.path.join(home, "Documents")]
    onedrive_docs = os.path.join(home, "OneDrive", "Documents")
    if onedrive_docs not in paths:
        paths.append(onedrive_docs)
    return paths


def scan_publisher_style(documents_root: str) -> List[ScanCandidate]:
    """Some publishers store saves in Documents\\<Publisher>\\<Game>\\ rather
    than the 'My Games' convention. This scanner finds those known publisher
    folders and resolves the actual per-game save paths using
    _resolve_game_folders (which handles the extra nesting layer some games
    add, e.g. Documents\\CAPCOM\\OnimushaWotS\\backup\\)."""
    results = []
    for sub in _list_subdirs(documents_root):
        name = os.path.basename(sub).lower()
        if name not in _PUBLISHER_SAVE_FOLDERS:
            continue
        # For each known publisher folder, resolve its sub-folders as games.
        # Use max_depth=3 to handle deeper nesting like:
        # Publisher -> Game -> Saves -> *.sav
        game_subs = _list_subdirs(sub)
        for game_folder in game_subs:
            # Use the game folder name (e.g. "OnimushaWotS") as the candidate
            # name, not the resolved save path which might be "backup" or
            # "SaveGames" etc.
            game_name = os.path.basename(game_folder)
            resolved = _resolve_game_folders(game_folder, max_depth=3)
            for save_path in resolved:
                if _has_any_files(save_path):
                    results.append(ScanCandidate(
                        name=game_name,
                        save_path=save_path,
                        source=os.path.basename(sub).title(),
                        confidence="high",
                    ))
    return results


def scan_documents_style(documents_root: str) -> List[ScanCandidate]:
    """Documents itself holds all sorts of non-game folders, so results here
    are always low-confidence and meant to be reviewed before adding."""
    results = []
    for sub in _list_subdirs(documents_root):
        name = os.path.basename(sub)
        if name.lower() in _DOCUMENTS_EXCLUDE:
            continue
        # Skip publisher folders — handled by scan_publisher_style which does
        # deeper resolution into the actual per-game save paths.
        if name.lower() in _PUBLISHER_SAVE_FOLDERS:
            continue
        if not _has_any_files(sub):
            continue
        results.append(ScanCandidate(
            name=name, save_path=sub, source="Documents", confidence="low",
        ))
    return results


def scan_xdg_style(root: str, exclude: Set[str], source_label: str) -> List[ScanCandidate]:
    """For ~/.local/share and ~/.config: unlike Windows' AppData convention,
    native Linux games typically put saves directly in their own top-level
    folder here rather than nested inside an extra "Saves" subfolder — so,
    after blocklist filtering, each remaining non-empty folder is itself
    the candidate (similar to the Documents scan: broader than 'My Games',
    so always low confidence and meant to be reviewed)."""
    results = []
    for sub in _list_subdirs(root):
        name = os.path.basename(sub)
        if name.lower() in exclude:
            continue
        if not _has_any_files(sub):
            continue
        results.append(ScanCandidate(
            name=name, save_path=sub, source=source_label, confidence="low",
        ))
    return results


def _find_save_subfolder(root: str, max_depth: int = 2) -> Optional[str]:
    """Look a few levels deep for a folder whose name suggests it holds
    saves (e.g. AppData\\Local\\SomeGame\\Saved\\SaveGames\\)."""
    stack = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        for sub in _list_subdirs(current):
            if os.path.basename(sub).lower() in _SAVE_FOLDER_KEYWORDS:
                return sub
            stack.append((sub, depth + 1))
    return None


def scan_appdata_style(appdata_local_root: str) -> List[ScanCandidate]:
    """AppData\\Local is too noisy to scan blindly (browsers, OS components,
    unrelated utilities all live here) — so a folder only becomes a
    candidate if it contains a nested folder that looks save-related."""
    results = []
    for sub in _list_subdirs(appdata_local_root):
        name = os.path.basename(sub)
        if name.lower() in _APPDATA_EXCLUDE:
            continue
        save_dir = _find_save_subfolder(sub)
        if save_dir and _has_any_files(save_dir):
            results.append(ScanCandidate(
                name=name, save_path=save_dir, source="AppData", confidence="low",
            ))
    return results


def _read_steam_app_names(steam_root: str) -> dict:
    """Reads local appmanifest_<id>.acf files (plain text, no network
    access) to resolve a Steam AppID to its real game name where possible."""
    names = {}
    steamapps = os.path.join(steam_root, "steamapps")
    try:
        entries = os.scandir(steamapps)
    except OSError:
        return names
    with entries:
        for entry in entries:
            if not (entry.is_file() and entry.name.startswith("appmanifest_") and entry.name.endswith(".acf")):
                continue
            appid = entry.name[len("appmanifest_"):-len(".acf")]
            try:
                with open(entry.path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                match = re.search(r'"name"\s*"([^"]+)"', content)
                if match:
                    names[appid] = match.group(1)
            except OSError:
                continue
    return names


# Low AppIDs that belong to Steam's own internal components rather than any
# actual game — Steam creates userdata cloud-sync folders for these too
# (e.g. "5" = Steam Client Service, "7" = the Steam client itself), which is
# why they can show up during a scan even though they're not games. Kept as
# a defensive second layer on top of requiring a resolved name below, in
# case one of these ever ends up with a matching local appmanifest.
_STEAM_NON_GAME_APPIDS = {"1", "4", "5", "7", "8", "1007", "228980"}


def scan_steam_userdata(steam_root: str) -> List[ScanCandidate]:
    results = []
    userdata = os.path.join(steam_root, "userdata")
    app_names = _read_steam_app_names(steam_root)
    for steamid_dir in _list_subdirs(userdata):
        for appid_dir in _list_subdirs(steamid_dir):
            appid = os.path.basename(appid_dir)
            if not appid.isdigit() or appid in _STEAM_NON_GAME_APPIDS:
                continue
            # Only surface a candidate when we can actually resolve its real
            # name from a local appmanifest file (see the module docstring
            # for why this doubles as the non-game filter).
            if appid not in app_names:
                continue
            remote_dir = os.path.join(appid_dir, "remote")
            if os.path.isdir(remote_dir) and _has_any_files(remote_dir):
                save_path = remote_dir
            elif _has_any_files(appid_dir):
                save_path = appid_dir
            else:
                continue
            results.append(ScanCandidate(
                name=app_names[appid], save_path=save_path,
                source="Steam", confidence="high",
            ))
    return results


def scan_proton_prefixes(steam_root: str) -> List[ScanCandidate]:
    """Proton (Steam Play) runs each Windows game in its own Wine prefix at
    steamapps/compatdata/<AppID>/pfx/, which contains a full emulated
    Windows user folder — C:\\users\\steamuser\\Documents, \\AppData, etc.
    Since that's structurally identical to a real Windows install, the same
    My Games / Saved Games / Documents / AppData heuristics apply, just
    rooted inside the prefix. Every prefix belongs to exactly one AppID, so
    (like native Steam userdata) results are only surfaced when the AppID
    resolves to a real name, and labeled with that name rather than a raw
    folder name for accuracy."""
    results = []
    compatdata = os.path.join(steam_root, "steamapps", "compatdata")
    app_names = _read_steam_app_names(steam_root)

    for appid_dir in _list_subdirs(compatdata):
        appid = os.path.basename(appid_dir)
        if not appid.isdigit() or appid in _STEAM_NON_GAME_APPIDS:
            continue
        if appid not in app_names:
            continue

        win_user_root = os.path.join(appid_dir, "pfx", "drive_c", "users", "steamuser")
        if not os.path.isdir(win_user_root):
            continue

        found_path = None

        # Same publisher-nesting handling as scan_flat_style — a Proton
        # prefix is per-game already, so its My Games/Saved Games folder
        # normally has exactly one real leaf, but resolving through any
        # publisher-name layer still matters for pointing save_path at the
        # actual save folder rather than one level too shallow.
        my_games_subs = _list_subdirs(os.path.join(win_user_root, "Documents", "My Games"))
        if my_games_subs:
            resolved = _resolve_game_folders(my_games_subs[0])
            if resolved:
                found_path = resolved[0]

        if not found_path:
            saved_games_subs = _list_subdirs(os.path.join(win_user_root, "Saved Games"))
            if saved_games_subs:
                resolved = _resolve_game_folders(saved_games_subs[0])
                if resolved:
                    found_path = resolved[0]

        if not found_path:
            for sub in _list_subdirs(os.path.join(win_user_root, "Documents")):
                if os.path.basename(sub).lower() not in _DOCUMENTS_EXCLUDE:
                    found_path = sub
                    break

        if not found_path:
            for sub in _list_subdirs(os.path.join(win_user_root, "AppData", "Local")):
                if os.path.basename(sub).lower() in _APPDATA_EXCLUDE:
                    continue
                save_dir = _find_save_subfolder(sub)
                if save_dir and _has_any_files(save_dir):
                    found_path = save_dir
                    break

        if found_path and _has_any_files(found_path):
            results.append(ScanCandidate(
                name=app_names[appid], save_path=found_path,
                source="Proton", confidence="high",
            ))

    return results


def _default_steam_roots(home: str, platform: Optional[str] = None) -> List[str]:
    """Where to look for a Steam install, by platform. A person could in
    principle have more than one of these (e.g. a native install alongside
    a Flatpak) so discover_candidates scans every one that actually exists
    rather than stopping at the first match."""
    platform = platform if platform is not None else sys.platform
    if platform.startswith("win"):
        return [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
    if platform == "darwin":
        return [os.path.join(home, "Library", "Application Support", "Steam")]
    # Linux, including SteamOS, ChimeraOS, Bazzite, CachyOS, and other
    # Arch/Fedora-based gaming distros — Steam can end up in any of these
    # depending on install method.
    return [
        os.path.join(home, ".steam", "steam"),                     # classic native install
        os.path.join(home, ".local", "share", "Steam"),             # newer native install layout
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),  # Flatpak
        os.path.join(home, "snap", "steam", "common", ".local", "share", "Steam"),  # Snap
    ]


def discover_candidates(
    existing_paths: Set[str],
    home: Optional[str] = None,
    appdata_local: Optional[str] = None,
    steam_roots: Optional[List[str]] = None,
    xdg_data_home: Optional[str] = None,
    xdg_config_home: Optional[str] = None,
) -> List[ScanCandidate]:
    """Scan the common Windows and Linux save-game locations and return
    candidates not already tracked. Any location that doesn't exist (not on
    that OS, Steam not installed, no Proton games, etc.) is silently
    skipped rather than erroring."""
    home = home or os.path.expanduser("~")
    appdata_local = appdata_local or os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    if steam_roots is None:
        steam_roots = _default_steam_roots(home)
    if xdg_data_home is None:
        xdg_data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    if xdg_config_home is None:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")

    candidates: List[ScanCandidate] = []

    my_games = os.path.join(home, "Documents", "My Games")
    if os.path.isdir(my_games):
        candidates += scan_flat_style(my_games, "My Games")

    saved_games = os.path.join(home, "Saved Games")
    if os.path.isdir(saved_games):
        candidates += scan_flat_style(saved_games, "Saved Games")

    # Check both standard Documents and OneDrive Documents paths
    for documents in _get_documents_paths(home):
        if os.path.isdir(documents):
            candidates += scan_publisher_style(documents)
            candidates += scan_documents_style(documents)
            # Also check My Games and Saved Games under OneDrive
            onedrive_my_games = os.path.join(documents, "My Games")
            if os.path.isdir(onedrive_my_games) and onedrive_my_games != my_games:
                candidates += scan_flat_style(onedrive_my_games, "My Games")
            onedrive_saved_games = os.path.join(documents, "Saved Games")
            if os.path.isdir(onedrive_saved_games) and onedrive_saved_games != saved_games:
                candidates += scan_flat_style(onedrive_saved_games, "Saved Games")

    if os.path.isdir(appdata_local):
        candidates += scan_appdata_style(appdata_local)

    # Scan piracy/emulator save directories (Goldberg, CODEX, SkidRow, etc.)
    # Only on Windows — these paths use Windows environment variables.
    if sys.platform.startswith("win"):
        candidates += scan_piracy_saves()
        candidates += scan_tenoke_and_universe()

    if os.path.isdir(xdg_data_home):
        candidates += scan_xdg_style(xdg_data_home, _XDG_DATA_EXCLUDE, "Local Data")

    if os.path.isdir(xdg_config_home):
        candidates += scan_xdg_style(xdg_config_home, _XDG_CONFIG_EXCLUDE, "Config")

    for steam_root in steam_roots:
        if os.path.isdir(steam_root):
            candidates += scan_steam_userdata(steam_root)
            candidates += scan_proton_prefixes(steam_root)

    normalized_existing = {os.path.normcase(os.path.normpath(p)) for p in existing_paths}
    seen = set()
    unique: List[ScanCandidate] = []
    for c in candidates:
        key = os.path.normcase(os.path.normpath(c.save_path))
        if key in normalized_existing or key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


class GameScanWorker(QThread):
    """Runs discover_candidates() on a background thread so scanning
    Documents/AppData/Steam folders never freezes the UI.

    Named `scan_finished` rather than `finished` for the same reason
    BackupWorker uses `backup_finished` — QThread's own built-in `finished`
    signal (fired only once the OS thread has truly terminated) must stay
    available for safe cleanup; shadowing it risks destroying the thread
    object while it's still technically running."""

    scan_finished = Signal(list)   # List[ScanCandidate]
    scan_error = Signal(str)

    def __init__(self, existing_paths: Set[str]):
        super().__init__()
        self.existing_paths = existing_paths

    def run(self):
        try:
            results = discover_candidates(self.existing_paths)
            self.scan_finished.emit(results)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
            self.scan_error.emit(str(exc))
