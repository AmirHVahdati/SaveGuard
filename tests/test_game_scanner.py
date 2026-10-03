"""app/game_scanner.py — guessing where games keep their saves.

The scanner is all heuristics, so the tests build small directory trees that
look like the real thing (a My Games folder, a Steam install with
appmanifest files, a Proton prefix) and assert what does and doesn't come back
as a candidate. Every scan_* function takes an explicit root, so nothing here
depends on the machine's real layout.
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import game_scanner
from app.game_scanner import (
    ScanCandidate, _default_steam_roots, _find_save_subfolder, _find_steam_root,
    _has_any_files, _has_direct_files, _is_empty_playtime_only,
    _list_subdirs, _list_subdirs_hidden, _looks_like_save_folder,
    _NON_GAME_APPIDS, _read_steam_app_names, _resolve_appid_name,
    _resolve_env_path, _resolve_game_folders, discover_candidates,
    scan_appdata_style, scan_documents_style, scan_flat_style,
    scan_piracy_saves, scan_proton_prefixes, scan_publisher_style,
    scan_steam_userdata, scan_tenoke_and_universe, scan_xdg_style,
)
from helpers import write_files


@pytest.fixture(autouse=True)
def discovery_does_not_walk_real_system_folders(monkeypatch):
    """Keep discover_candidates off the machine's real save folders.

    Most of its roots are injectable, but the piracy/emulator branch is reached
    through Windows environment variables with no way to redirect it, so the
    integration tests below run as a non-Windows platform where that branch
    doesn't exist. The scanners themselves are tested directly.
    """
    monkeypatch.setattr(game_scanner, "sys", SimpleNamespace(platform="linux"))

ACF_TEMPLATE = '"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n}}\n'


def names_of(candidates):
    return sorted(c.name for c in candidates)


def paths_of(candidates):
    return sorted(c.save_path for c in candidates)


def only(candidates):
    assert len(candidates) == 1, f"expected exactly one candidate, got {candidates}"
    return candidates[0]


# ---------------------------------------------------------------------------
# directory listing helpers
# ---------------------------------------------------------------------------

class TestListingHelpers:
    def test_lists_subdirectories_only(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {"game/keep.sav": "a", "notes.txt": "b"})
        assert _list_subdirs(str(tmp_path)) == [str(tmp_path / "game")]

    def test_skips_hidden_directories(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {"game/keep.sav": "a", ".config/prefs.ini": "b"})
        assert [Path(p).name for p in _list_subdirs(str(tmp_path))] == ["game"]

    def test_hidden_variant_includes_hidden_directories(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {".1911/game.sav": "a"})
        assert [Path(p).name for p in _list_subdirs_hidden(str(tmp_path))] == [".1911"]

    @pytest.mark.parametrize("missing", ["", "/definitely/not/here"])
    def test_missing_directory_yields_nothing(self, missing):
        assert _list_subdirs(missing) == []
        assert _list_subdirs_hidden(missing) == []


class TestHasAnyFiles:
    def test_file_directly_inside(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {"slot.sav": "a"})
        assert _has_any_files(str(tmp_path)) is True

    def test_file_only_in_a_subfolder(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {"deep/deeper/slot.sav": "a"})
        assert _has_any_files(str(tmp_path)) is True

    def test_empty_folder(self, tmp_path):
        (tmp_path / "empty").mkdir()
        assert _has_any_files(str(tmp_path / "empty")) is False

    def test_folder_with_only_empty_subfolders(self, tmp_path):
        (tmp_path / "a" / "b").mkdir(parents=True)
        assert _has_any_files(str(tmp_path)) is False

    def test_hidden_files_do_not_count(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {".hidden.sav": "a"})
        assert _has_any_files(str(tmp_path)) is False

    def test_missing_folder(self, tmp_path):
        assert _has_any_files(str(tmp_path / "nope")) is False

    def test_bails_out_early_on_a_huge_empty_tree(self, tmp_path):
        # Cheap existence check: a folder with hundreds of empty subfolders is
        # far more likely to be a library than a save folder, so the walk gives
        # up and reports "has files" rather than crawling all of it.
        for index in range(600):
            (tmp_path / f"dir{index}").mkdir()
        assert _has_any_files(str(tmp_path)) is True

    def test_a_modest_empty_tree_is_still_reported_as_empty(self, tmp_path):
        for index in range(50):
            (tmp_path / f"dir{index}").mkdir()
        assert _has_any_files(str(tmp_path)) is False


class TestHasDirectFiles:
    def test_file_at_the_top_level(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {"slot.sav": "a", "sub/other.sav": "b"})
        assert _has_direct_files(str(tmp_path)) is True

    def test_files_only_in_subfolders(self, tmp_path, write_save_tree):
        # The signal that separates a real save folder from a grouping folder
        # like Documents\\CD Projekt Red\\.
        write_save_tree(tmp_path, {"Cyberpunk 2077/slot.sav": "a"})
        assert _has_direct_files(str(tmp_path)) is False

    def test_missing_folder(self, tmp_path):
        assert _has_direct_files(str(tmp_path / "nope")) is False


class TestLooksLikeSaveFolder:
    @pytest.mark.parametrize("name", [
        "Save", "Saves", "savegames", "SaveGames", "SaveData", "save_data",
        "Saved Games", "profile", "UserData", "Player", "Players",
        "autosaves", "quick saves", "gamedata",
    ])
    def test_save_related_names(self, name):
        assert _looks_like_save_folder(name) is True

    @pytest.mark.parametrize("name", [
        "Config", "Configs", "Logs", "Screenshots", "Shaders", "Cache",
        "Temp", "Backup", "Backups", "Benchmark", "Languages", "Skins",
        "Profiles", "CrashDumps", "Settings", "Metadata",
    ])
    def test_non_save_names(self, name):
        assert _looks_like_save_folder(name) is False

    @pytest.mark.parametrize("name", ["Unrelated", "Program Files", "12345"])
    def test_unrelated_names(self, name):
        assert _looks_like_save_folder(name) is False

    def test_is_case_insensitive(self):
        assert _looks_like_save_folder("sAvEgAmEs") is True


# ---------------------------------------------------------------------------
# resolving a folder to the actual save location
# ---------------------------------------------------------------------------

class TestResolveGameFolders:
    def test_game_with_files_directly_inside_is_its_own_save_folder(
            self, tmp_path, write_save_tree):
        game = write_save_tree(tmp_path / "Hades", {"slot.sav": "a"})
        assert _resolve_game_folders(str(game)) == [str(game)]

    def test_empty_folder_resolves_to_nothing(self, tmp_path):
        empty = tmp_path / "Empty"
        empty.mkdir()
        assert _resolve_game_folders(str(empty)) == []

    def test_prefers_a_save_subfolder_over_generic_ones(self, tmp_path, write_save_tree):
        game = write_save_tree(tmp_path / "SomeGame", {
            "Config/settings.ini": "a",
            "Logs/output.log": "b",
            "SaveGames/slot.sav": "c",
        })
        assert _resolve_game_folders(str(game)) == [str(game / "SaveGames")]

    def test_falls_back_to_the_folder_itself_when_no_subfolder_looks_save_related(
            self, tmp_path, write_save_tree):
        game = write_save_tree(tmp_path / "SomeGame", {"Data/slot.sav": "a"})
        assert _resolve_game_folders(str(game)) == [str(game)]

    def test_descends_when_nothing_at_the_top_level_has_files(self, tmp_path, write_save_tree):
        game = write_save_tree(tmp_path / "SomeGame", {"a/b/c/slot.sav": "x"})
        # The game folder itself holds save data further down, so it's the
        # candidate rather than any intermediate folder.
        assert _resolve_game_folders(str(game), max_depth=2) == [str(game)]

    def test_honours_the_depth_limit(self, tmp_path, write_save_tree):
        game = write_save_tree(tmp_path / "SomeGame", {"a/b/c/slot.sav": "x"})
        # Already past the limit, so the folder itself is accepted as-is.
        assert _resolve_game_folders(str(game), max_depth=1, _depth=2) == [str(game)]

    @pytest.mark.parametrize("publisher", ["CAPCOM", "Bethesda", "Square Enix", "Konami"])
    def test_publisher_folder_is_never_returned_as_a_game(
            self, tmp_path, write_save_tree, publisher):
        # A publisher folder would otherwise be offered as a game called
        # "Bethesda" instead of the game inside it.
        root = write_save_tree(tmp_path, {f"{publisher}/Skyrim/slot.sav": "a"})
        resolved = _resolve_game_folders(str(root / publisher))
        assert resolved == [str(root / publisher / "Skyrim")]

    def test_publisher_folder_can_group_several_games(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {
            "2K Games/Witcher 3/slot.sav": "a",
            "2K Games/Borderlands/slot.sav": "b",
        })
        resolved = _resolve_game_folders(str(root / "2K Games"))
        assert resolved == [
            str(root / "2K Games" / "Borderlands"),
            str(root / "2K Games" / "Witcher 3"),
        ]

    def test_publisher_folder_skips_config_only_subfolders(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {
            "CAPCOM/OnimushaWotS/backup/slot.dat": "a",
            "CAPCOM/Settings/prefs.ini": "b",
        })
        resolved = _resolve_game_folders(str(root / "CAPCOM"))
        assert all("Settings" not in path for path in resolved)


# ---------------------------------------------------------------------------
# My Games / Saved Games
# ---------------------------------------------------------------------------

class TestScanFlatStyle:
    def test_each_subfolder_is_a_high_confidence_game(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {
            "Hades/slot.sav": "a",
            "Hollow Knight/save.dat": "b",
        })
        results = scan_flat_style(str(root), "My Games")
        assert names_of(results) == ["Hades", "Hollow Knight"]
        assert {c.confidence for c in results} == {"high"}
        assert {c.source for c in results} == {"My Games"}
        assert paths_of(results) == [
            str(root / "Hades"), str(root / "Hollow Knight"),
        ]

    def test_empty_subfolders_are_omitted(self, tmp_path):
        (tmp_path / "Empty Game").mkdir()
        assert scan_flat_style(str(tmp_path), "Saved Games") == []

    @pytest.mark.parametrize("folder", [
        "UnrealEngine", "UE5", "Unity", "UnityCache", "Launcher", "Common", "Shaders",
    ])
    def test_engine_and_launcher_folders_are_skipped(self, tmp_path, write_save_tree, folder):
        write_save_tree(tmp_path, {f"{folder}/stuff.sav": "a", "Real Game/slot.sav": "b"})
        assert names_of(scan_flat_style(str(tmp_path), "My Games")) == ["Real Game"]

    def test_publisher_subfolder_is_labelled_by_the_game_inside(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"CD Projekt Red/Cyberpunk 2077/slot.sav": "a"})
        candidate = only(scan_flat_style(str(root), "My Games"))
        assert candidate.name == "Cyberpunk 2077"
        assert candidate.save_path == str(root / "CD Projekt Red" / "Cyberpunk 2077")

    def test_missing_root_yields_nothing(self, tmp_path):
        assert scan_flat_style(str(tmp_path / "nope"), "My Games") == []


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

class TestScanPublisherStyle:
    def test_finds_games_under_a_known_publisher(self, tmp_path, write_save_tree):
        documents = write_save_tree(tmp_path, {
            "CAPCOM/OnimushaWotS/backup/slot.dat": "a",
        })
        candidate = only(scan_publisher_style(str(documents)))
        assert candidate.name == "OnimushaWotS"  # not "backup", not "CAPCOM"
        assert candidate.save_path == str(documents / "CAPCOM" / "OnimushaWotS")
        assert candidate.source == "Capcom"
        assert candidate.confidence == "high"

    def test_resolves_a_save_subfolder_under_the_game(self, tmp_path, write_save_tree):
        documents = write_save_tree(tmp_path, {
            "Bethesda/Skyrim Special Edition/Saves/slot.sav": "a",
        })
        candidate = only(scan_publisher_style(str(documents)))
        assert candidate.name == "Skyrim Special Edition"
        assert candidate.save_path.endswith(
            os.path.join("Skyrim Special Edition", "Saves"))

    def test_several_games_under_one_publisher(self, tmp_path, write_save_tree):
        documents = write_save_tree(tmp_path, {
            "Koei Tecmo/Nioh 2/slot.sav": "a",
            "Koei Tecmo/Wo Long/slot.sav": "b",
        })
        assert names_of(scan_publisher_style(str(documents))) == ["Nioh 2", "Wo Long"]

    def test_ignores_non_publisher_folders(self, tmp_path, write_save_tree):
        documents = write_save_tree(tmp_path, {"Hades/slot.sav": "a"})
        assert scan_publisher_style(str(documents)) == []

    def test_ignores_publisher_folders_with_no_save_data(self, tmp_path):
        (tmp_path / "CAPCOM" / "OnimushaWotS").mkdir(parents=True)
        assert scan_publisher_style(str(tmp_path)) == []


class TestScanDocumentsStyle:
    def test_results_are_low_confidence(self, tmp_path, write_save_tree):
        documents = write_save_tree(tmp_path, {"Hades/slot.sav": "a"})
        candidate = only(scan_documents_style(str(documents)))
        assert candidate.name == "Hades"
        assert candidate.source == "Documents"
        # Documents holds all sorts of non-game folders, so these are always
        # shown for review rather than trusted outright.
        assert candidate.confidence == "low"

    @pytest.mark.parametrize("excluded", [
        "My Games", "My Pictures", "OneDrive", "GitHub", "Notion", "obsidian",
        "steam", "node_modules", "Saved Games", "Windows",
    ])
    def test_blocklisted_folders_are_skipped(self, tmp_path, write_save_tree, excluded):
        write_save_tree(tmp_path, {f"{excluded}/something.sav": "a"})
        assert scan_documents_style(str(tmp_path)) == []

    def test_publisher_folders_are_left_to_the_publisher_scanner(self, tmp_path, write_save_tree):
        write_save_tree(tmp_path, {"CAPCOM/OnimushaWotS/backup/slot.dat": "a"})
        assert scan_documents_style(str(tmp_path)) == []

    def test_empty_folders_are_skipped(self, tmp_path):
        (tmp_path / "Hades").mkdir()
        assert scan_documents_style(str(tmp_path)) == []

    def test_missing_root_yields_nothing(self, tmp_path):
        assert scan_documents_style(str(tmp_path / "nope")) == []


# ---------------------------------------------------------------------------
# Linux XDG folders
# ---------------------------------------------------------------------------

class TestScanXdgStyle:
    def test_each_folder_is_a_candidate(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"Hollow Knight/user.dat": "a"})
        candidate = only(scan_xdg_style(str(root), {"steam"}, "Local Data"))
        # Native Linux games put saves straight in their own top-level folder,
        # so the folder itself is the save folder.
        assert candidate.save_path == str(root / "Hollow Knight")
        assert candidate.name == "Hollow Knight"
        assert candidate.source == "Local Data"
        assert candidate.confidence == "low"

    def test_blocklisted_folders_are_skipped(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {
            "trash/files/x.sav": "a",
            "flatpak/app/x.sav": "b",
            "Hades/slot.sav": "c",
        })
        assert names_of(scan_xdg_style(str(root), {"trash", "flatpak"}, "Local Data")) == ["Hades"]

    def test_blocklist_is_case_insensitive(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"Trash/x.sav": "a"})
        assert scan_xdg_style(str(root), {"trash"}, "Config") == []

    def test_empty_folders_are_skipped(self, tmp_path):
        (tmp_path / "Hades").mkdir()
        assert scan_xdg_style(str(tmp_path), set(), "Config") == []

    def test_ships_with_blocklists_for_the_linux_desktop_defaults(self):
        # A desktop's ~/.local/share is mostly not games.
        assert {"steam", "flatpak", "trash", "lutris"} <= \
            game_scanner._XDG_DATA_EXCLUDE
        assert {"autostart", "dconf", "pulse"} <= game_scanner._XDG_CONFIG_EXCLUDE


# ---------------------------------------------------------------------------
# AppData
# ---------------------------------------------------------------------------

class TestScanAppdataStyle:
    def test_only_folders_with_a_save_subfolder_are_offered(self, tmp_path, write_save_tree):
        appdata = write_save_tree(tmp_path, {
            "SomeGame/Saves/slot.sav": "a",
            "OtherGame/Config/settings.ini": "b",
        })
        candidate = only(scan_appdata_style(str(appdata)))
        assert candidate.name == "SomeGame"
        assert candidate.save_path == str(appdata / "SomeGame" / "Saves")
        assert candidate.source == "AppData"
        assert candidate.confidence == "low"

    def test_save_subfolder_is_found_at_the_second_level(self, tmp_path, write_save_tree):
        appdata = write_save_tree(tmp_path, {
            "SomeGame/data/SaveData/slot.sav": "a",
        })
        candidate = only(scan_appdata_style(str(appdata)))
        assert candidate.save_path.endswith(os.path.join("data", "SaveData"))

    @pytest.mark.parametrize("excluded", [
        "Google", "Microsoft", "Packages", "Programs", "Temp", "NVIDIA",
        "Docker", "Spotify", "Adobe", "Epic Games",
    ])
    def test_appdata_blocklist_is_applied(self, tmp_path, write_save_tree, excluded):
        appdata = write_save_tree(tmp_path, {f"{excluded}/Saves/slot.sav": "a"})
        assert scan_appdata_style(str(appdata)) == []

    def test_save_folder_without_files_is_skipped(self, tmp_path):
        (tmp_path / "SomeGame" / "Saves").mkdir(parents=True)
        assert scan_appdata_style(str(tmp_path)) == []

    def test_profile_folders_alone_are_not_enough(self, tmp_path, write_save_tree):
        # Tried and reverted: Adobe and several browsers use "Profile" for
        # their own settings, which produced real false positives.
        appdata = write_save_tree(tmp_path, {
            "Adobe/Something/Profile/prefs.dat": "a",
        })
        assert scan_appdata_style(str(appdata)) == []

    def test_missing_root_yields_nothing(self, tmp_path):
        assert scan_appdata_style(str(tmp_path / "nope")) == []


class TestFindSaveSubfolder:
    def test_finds_a_named_folder(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"SomeGame/Saves/slot.sav": "a"})
        assert _find_save_subfolder(str(root)) == str(root / "SomeGame" / "Saves")

    def test_respects_the_depth_limit(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"SomeGame/a/b/Saves/slot.sav": "a"})
        assert _find_save_subfolder(str(root), max_depth=2) is None
        assert _find_save_subfolder(str(root), max_depth=3) == \
            str(root / "SomeGame" / "a" / "b" / "Saves")

    def test_returns_none_when_nothing_looks_save_related(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"SomeGame/Config/settings.ini": "a"})
        assert _find_save_subfolder(str(root)) is None


# ---------------------------------------------------------------------------
# Steam
# ---------------------------------------------------------------------------

def make_steam_install(root, apps=None, userdata=None, compatdata=None):
    """Build a minimal Steam tree.

    apps     -- {appid: name} written as appmanifest files
    userdata -- {steamid: {appid: {subfolder: {filename: contents}}}}
    compatdata -- {appid: {relative folders under pfx/drive_c/users/steamuser}}
    """
    steam = Path(root)
    steamapps = steam / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    for appid, name in (apps or {}).items():
        (steamapps / f"appmanifest_{appid}.acf").write_text(
            ACF_TEMPLATE.format(appid=appid, name=name), encoding="utf-8")
    for steamid, apps_dir in (userdata or {}).items():
        for appid, folders in apps_dir.items():
            for folder, files in folders.items():
                write_files(steam / "userdata" / steamid / appid / folder, files)
    for appid, folders in (compatdata or {}).items():
        for relative, files in folders.items():
            write_files(
                steam / "steamapps" / "compatdata" / appid / "pfx" / "drive_c"
                / "users" / "steamuser" / relative,
                files,
            )
    return steam


class TestReadSteamAppNames:
    def test_reads_the_name_field(self, tmp_path):
        steam = make_steam_install(tmp_path / "Steam", apps={"220": "Half-Life 2"})
        assert _read_steam_app_names(str(steam)) == {"220": "Half-Life 2"}

    def test_reads_several_installs(self, tmp_path):
        steam = make_steam_install(tmp_path / "Steam", apps={
            "220": "Half-Life 2",
            "570": "Dota 2",
            "1091500": "Cyberpunk 2077",
        })
        assert names_of and set(_read_steam_app_names(str(steam))) == {"220", "570", "1091500"}

    def test_ignores_unrelated_files_in_steamapps(self, tmp_path):
        steam = make_steam_install(tmp_path / "Steam", apps={"220": "Half-Life 2"})
        (steam / "steamapps" / "libraryfolders.vdf").write_text("junk", encoding="utf-8")
        (steam / "steamapps" / "appmanifest_999.txt").write_text("junk", encoding="utf-8")
        assert _read_steam_app_names(str(steam)) == {"220": "Half-Life 2"}

    def test_skips_manifests_without_a_name(self, tmp_path):
        steam = make_steam_install(tmp_path / "Steam", apps={"220": "Half-Life 2"})
        (steam / "steamapps" / "appmanifest_555.acf").write_text(
            '"AppState" { "appid" "555" }', encoding="utf-8")
        assert set(_read_steam_app_names(str(steam))) == {"220"}

    def test_missing_steamapps_folder_yields_nothing(self, tmp_path):
        assert _read_steam_app_names(str(tmp_path)) == {}

    def test_names_with_punctuation_are_read_whole(self, tmp_path):
        steam = make_steam_install(tmp_path / "Steam", apps={"620": "Portal 2"})
        assert _read_steam_app_names(str(steam))["620"] == "Portal 2"


class TestResolveAppidName:
    def test_non_numeric_folder_is_not_an_appid(self):
        assert _resolve_appid_name("Common", {}) is None

    def test_resolves_through_the_cache(self):
        assert _resolve_appid_name("220", {"220": "Half-Life 2"}) == "Half-Life 2"

    def test_unknown_appid_resolves_to_nothing(self):
        assert _resolve_appid_name("12345", {}) is None

    @pytest.mark.parametrize("appid", sorted(_NON_GAME_APPIDS))
    def test_steam_internal_ids_are_rejected(self, appid):
        assert _resolve_appid_name(appid, {appid: "Steam Client"}) is None

    def test_without_a_steam_install_nothing_resolves(self, monkeypatch):
        monkeypatch.setattr(game_scanner, "_find_steam_root", lambda: None)
        assert _resolve_appid_name("220") is None

    def test_without_a_steam_install_an_internal_id_is_rejected(self, monkeypatch):
        monkeypatch.setattr(game_scanner, "_find_steam_root", lambda: None)
        assert _resolve_appid_name("7") is None


class TestScanSteamUserdata:
    def test_resolved_app_with_cloud_saves(self, tmp_path):
        steam = make_steam_install(
            tmp_path / "Steam",
            apps={"1091500": "Cyberpunk 2077"},
            userdata={"123": {"1091500": {"remote": {"slot.dat": "a"}}}},
        )
        candidate = only(scan_steam_userdata(str(steam)))
        assert candidate.name == "Cyberpunk 2077"
        assert candidate.save_path == str(steam / "userdata" / "123" / "1091500" / "remote")
        assert candidate.source == "Steam"
        assert candidate.confidence == "high"

    def test_falls_back_to_the_app_folder_when_there_is_no_remote(
            self, tmp_path):
        steam = make_steam_install(
            tmp_path / "Steam",
            apps={"220": "Half-Life 2"},
            userdata={"123": {"220": {"": {"save.sav": "a"}}}},
        )
        candidate = only(scan_steam_userdata(str(steam)))
        assert candidate.save_path == str(steam / "userdata" / "123" / "220")

    def test_appid_with_no_resolvable_name_is_never_shown(self, tmp_path):
        # Steam never writes an appmanifest for its own internal IDs, so
        # requiring a resolved name is simultaneously how a genuine game is
        # identified and how junk folders get filtered out.
        steam = make_steam_install(
            tmp_path / "Steam", apps={}, userdata={"123": {"999999": {"remote": {"x": "a"}}}}
        )
        assert scan_steam_userdata(str(steam)) == []

    @pytest.mark.parametrize("appid", ["5", "7"])
    def test_steam_internal_ids_are_skipped_even_with_a_manifest(self, tmp_path, appid):
        steam = make_steam_install(
            tmp_path / "Steam",
            apps={appid: "Steam Client"},
            userdata={"123": {appid: {"remote": {"x": "a"}}}},
        )
        assert scan_steam_userdata(str(steam)) == []

    def test_non_numeric_app_folders_are_skipped(self, tmp_path):
        steam = make_steam_install(
            tmp_path / "Steam",
            apps={"220": "Half-Life 2"},
            userdata={"123": {"Common": {"remote": {"x": "a"}}}},
        )
        assert scan_steam_userdata(str(steam)) == []

    def test_empty_app_folders_are_skipped(self, tmp_path):
        steam = make_steam_install(
            tmp_path / "Steam", apps={"220": "Half-Life 2"}, userdata={"123": {"220": {"remote": {}}}}
        )
        assert scan_steam_userdata(str(steam)) == []

    def test_every_steam_account_is_scanned(self, tmp_path):
        steam = make_steam_install(
            tmp_path / "Steam",
            apps={"220": "Half-Life 2", "570": "Dota 2"},
            userdata={
                "111": {"220": {"remote": {"a": "x"}}},
                "222": {"570": {"remote": {"b": "x"}}},
            },
        )
        assert names_of(scan_steam_userdata(str(steam))) == ["Dota 2", "Half-Life 2"]


class TestScanProtonPrefixes:
    def _prefix(self, tmp_path, appid, name, folders):
        return make_steam_install(
            tmp_path / "Steam",
            apps={appid: name},
            compatdata={appid: folders},
        )

    def test_finds_a_game_in_my_games(self, tmp_path):
        steam = self._prefix(tmp_path, "1091500", "Cyberpunk 2077", {
            os.path.join("Documents", "My Games", "CD Projekt Red", "Cyberpunk 2077"):
                {"slot.dat": "a"},
        })
        candidate = only(scan_proton_prefixes(str(steam)))
        assert candidate.name == "Cyberpunk 2077"  # the real name, not a folder name
        assert candidate.save_path.endswith(
            os.path.join("My Games", "CD Projekt Red", "Cyberpunk 2077"))
        assert candidate.source == "Proton"
        assert candidate.confidence == "high"

    def test_finds_a_game_in_saved_games(self, tmp_path):
        steam = self._prefix(tmp_path, "1245620", "ELDEN RING", {
            os.path.join("Saved Games", "FromSoftware", "ELDEN RING"): {"save.dat": "a"},
        })
        candidate = only(scan_proton_prefixes(str(steam)))
        assert candidate.name == "ELDEN RING"
        assert candidate.save_path.endswith(os.path.join("Saved Games", "FromSoftware"))

    def test_falls_back_to_plain_documents(self, tmp_path):
        steam = self._prefix(tmp_path, "620", "Portal 2", {
            os.path.join("Documents", "My Weird Game"): {"slot.sav": "a"},
        })
        candidate = only(scan_proton_prefixes(str(steam)))
        assert candidate.save_path.endswith(os.path.join("Documents", "My Weird Game"))

    def test_falls_back_to_appdata(self, tmp_path):
        steam = self._prefix(tmp_path, "105600", "Terraria", {
            os.path.join("AppData", "Local", "Terraria", "Saves"): {"world.sav": "a"},
        })
        candidate = only(scan_proton_prefixes(str(steam)))
        assert candidate.save_path.endswith(os.path.join("Terraria", "Saves"))

    def test_my_games_wins_over_documents(self, tmp_path):
        steam = self._prefix(tmp_path, "999", "Two Candidate Game", {
            os.path.join("Documents", "My Games", "Game"): {"slot.sav": "a"},
            os.path.join("Documents", "Other Game"): {"slot.sav": "a"},
        })
        candidate = only(scan_proton_prefixes(str(steam)))
        assert candidate.save_path.endswith(os.path.join("My Games", "Game"))

    def test_prefix_without_a_user_folder_is_skipped(self, tmp_path):
        steam = make_steam_install(
            tmp_path / "Steam", apps={"999": "No Prefix Game"}, compatdata={"999": {}}
        )
        assert scan_proton_prefixes(str(steam)) == []

    def test_prefix_with_no_save_data_is_skipped(self, tmp_path):
        steam = self._prefix(tmp_path, "999", "Empty Game", {
            os.path.join("Documents", "My Games", "Game"): {},
        })
        assert scan_proton_prefixes(str(steam)) == []

    def test_unresolvable_appid_is_never_shown(self, tmp_path):
        steam = make_steam_install(tmp_path / "Steam", apps={}, compatdata={
            "999999": {os.path.join("Documents", "My Games", "Game"): {"a": "x"}},
        })
        assert scan_proton_prefixes(str(steam)) == []

    def test_excluded_documents_folders_do_not_become_candidates(self, tmp_path):
        steam = self._prefix(tmp_path, "999", "Only Junk", {
            os.path.join("Documents", "Steam"): {"a": "x"},
        })
        assert scan_proton_prefixes(str(steam)) == []


class TestSteamRoots:
    def test_windows_default_locations(self):
        roots = _default_steam_roots("C:/Users/x", platform="win32")
        assert roots == [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]

    def test_linux_native_flatpak_and_snap_locations(self):
        home = os.path.join(os.sep, "home", "x")
        assert _default_steam_roots(home, platform="linux") == [
            os.path.join(home, ".steam", "steam"),
            os.path.join(home, ".local", "share", "Steam"),
            os.path.join(home, ".var", "app", "com.valvesoftware.Steam",
                         ".local", "share", "Steam"),
            os.path.join(home, "snap", "steam", "common", ".local", "share", "Steam"),
        ]

    def test_macos_location(self):
        home = os.path.join(os.sep, "Users", "x")
        assert _default_steam_roots(home, platform="darwin") == [
            os.path.join(home, "Library", "Application Support", "Steam")
        ]

    def test_every_install_location_is_considered(self):
        # Someone can have a native install alongside a Flatpak or Snap one,
        # and discovery scans all of them rather than stopping at the first.
        assert len(_default_steam_roots("/home/x", platform="linux")) == 4

    def test_defaults_to_the_running_platform(self):
        # game_scanner.sys may be swapped by a fixture; compare against the
        # platform the module itself is running as.
        assert _default_steam_roots("/home/x") == \
            _default_steam_roots("/home/x", game_scanner.sys.platform)


class TestFindSteamRoot:
    def test_finds_a_program_files_install(self, tmp_path, pretend_windows, monkeypatch):
        pretend_windows()
        make_steam_install(tmp_path / "Steam")
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
        assert _find_steam_root() == str(tmp_path / "Steam")

    def test_falls_back_to_the_x86_program_files(self, tmp_path, pretend_windows, monkeypatch):
        pretend_windows()
        make_steam_install(tmp_path / "Steam")
        monkeypatch.delenv("PROGRAMFILES", raising=False)
        monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
        assert _find_steam_root() == str(tmp_path / "Steam")

    def test_requires_a_steamapps_directory(self, tmp_path, pretend_windows, monkeypatch):
        pretend_windows()
        (tmp_path / "Steam").mkdir()
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
        monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path))
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "nowhere"))
        assert _find_steam_root() is None

    def test_linux_home_install(self, tmp_path, pretend_windows, monkeypatch):
        # pretend_windows(platform=...) is how the Linux branch gets exercised
        # from a Windows or macOS machine too.
        pretend_windows(platform="linux")
        make_steam_install(tmp_path / ".local" / "share" / "Steam")
        monkeypatch.setattr(os.path, "expanduser", lambda _path: str(tmp_path))
        assert _find_steam_root() == str(tmp_path / ".local" / "share" / "Steam")

    def test_returns_none_when_steam_is_absent(self, tmp_path, pretend_windows, monkeypatch):
        pretend_windows(platform="linux")
        monkeypatch.setattr(os.path, "expanduser", lambda _path: str(tmp_path))
        assert _find_steam_root() is None


class TestResolveEnvPath:
    def test_windows_joins_the_variable(self, tmp_path, pretend_windows, monkeypatch):
        pretend_windows()
        monkeypatch.setenv("APPDATA", str(tmp_path))
        assert _resolve_env_path("APPDATA", "GSE Saves") == str(tmp_path / "GSE Saves")

    def test_windows_without_a_relative_path(self, tmp_path, pretend_windows, monkeypatch):
        pretend_windows()
        monkeypatch.setenv("PUBLIC", str(tmp_path))
        assert _resolve_env_path("PUBLIC") == str(tmp_path)

    def test_unset_variable_resolves_to_nothing(self, pretend_windows, monkeypatch):
        pretend_windows()
        monkeypatch.delenv("APPDATA", raising=False)
        assert _resolve_env_path("APPDATA") is None

    @pytest.mark.skipif(sys.platform == "win32", reason="this asserts the POSIX branch")
    def test_posix_never_resolves_windows_variables(self, monkeypatch):
        monkeypatch.setenv("APPDATA", "/tmp/appdata")
        assert _resolve_env_path("APPDATA") is None


# ---------------------------------------------------------------------------
# piracy / emulator save folders
# ---------------------------------------------------------------------------

class TestIsEmptyPlaytimeOnly:
    def test_only_playtime_files_is_empty(self, tmp_path, write_save_tree):
        folder = write_save_tree(tmp_path, {"playtime.txt": "120"})
        assert _is_empty_playtime_only(str(folder)) is True

    def test_nested_playtime_files_still_counts_as_empty(self, tmp_path, write_save_tree):
        # Real saves live elsewhere; the marker appears wherever the emulator
        # touched the tree.
        folder = write_save_tree(tmp_path, {
            "a/playtime.txt": "1", "b/c/playtime.txt": "2",
        })
        assert _is_empty_playtime_only(str(folder)) is True

    def test_a_real_save_makes_it_a_candidate(self, tmp_path, write_save_tree):
        folder = write_save_tree(tmp_path, {"playtime.txt": "1", "slot.sav": "a"})
        assert _is_empty_playtime_only(str(folder)) is False

    def test_a_real_save_in_a_subfolder_counts(self, tmp_path, write_save_tree):
        folder = write_save_tree(tmp_path, {"playtime.txt": "1", "data/slot.sav": "a"})
        assert _is_empty_playtime_only(str(folder)) is False

    def test_completely_empty_folder_is_empty(self, tmp_path):
        (tmp_path / "empty").mkdir()
        assert _is_empty_playtime_only(str(tmp_path / "empty")) is True

    def test_missing_folder_is_not_treated_as_playtime_only(self, tmp_path):
        assert _is_empty_playtime_only(str(tmp_path / "nope")) is False

    def test_terminates_on_a_link_cycle(self, self_referencing_dir):
        assert _is_empty_playtime_only(str(self_referencing_dir)) in (True, False)


class TestScanPiracySaves:
    @pytest.fixture
    def windows_env(self, tmp_path, monkeypatch, pretend_windows):
        """A fully redirected Windows environment.

        Every variable these scanners read is redirected, not just the one a
        given test is about: anything left pointing at the real APPDATA would
        otherwise scan the machine's actual emulator save folders.
        """
        pretend_windows()
        for name in ("APPDATA", "LOCALAPPDATA", "PUBLIC"):
            monkeypatch.setenv(name, str(tmp_path / name))
        # A Steam install, so numeric AppID folders can be resolved to names.
        monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
        monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "no-programdata"))
        make_steam_install(tmp_path / "Steam", apps={"1091500": "Cyberpunk 2077"})
        return tmp_path

    def test_finds_a_grouped_game_and_labels_the_group(
            self, tmp_path, windows_env, write_save_tree):
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / "Cyberpunk", {"slot.dat": "a"})
        candidate = only(scan_piracy_saves())
        assert candidate.name == "Cyberpunk"
        assert candidate.source == "Goldberg Fork (piracy)"
        assert candidate.confidence == "high"

    def test_resolves_a_numeric_folder_through_a_steam_manifest(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / "1091500" / "Save",
                        {"slot.dat": "a"})
        candidate = only(scan_piracy_saves())
        assert candidate.name == "Cyberpunk 2077"
        assert candidate.save_path.endswith(os.path.join("1091500", "Save"))

    def test_unknown_numeric_folder_keeps_its_folder_name(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / "999999", {"slot.dat": "a"})
        assert only(scan_piracy_saves()).name == "999999"

    def test_hidden_group_folder_is_scanned(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "APPDATA" / ".1911" / "Some Game", {"slot.dat": "a"})
        assert only(scan_piracy_saves()).source == "Razor1911 (piracy)"

    def test_hidden_folders_are_skipped_for_other_groups(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / ".hidden", {"slot.dat": "a"})
        assert scan_piracy_saves() == []

    @pytest.mark.parametrize("folder", ["Settings", "Config", "Logs", "Backups", "Profiles"])
    def test_config_only_folders_are_skipped(self, windows_env, write_save_tree, folder):
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / folder, {"prefs.ini": "a"})
        assert scan_piracy_saves() == []

    @pytest.mark.parametrize("appid", ["5", "7", "0"])
    def test_steam_internal_appids_are_skipped(self, windows_env, write_save_tree, appid):
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / appid, {"slot.dat": "a"})
        assert scan_piracy_saves() == []

    def test_playtime_only_folder_is_skipped(self, windows_env, write_save_tree):
        # The emulator's own tracking file is not save data; the real saves
        # are somewhere else.
        write_save_tree(windows_env / "APPDATA" / "GSE Saves" / "Some Game",
                        {"playtime.txt": "0"})
        assert scan_piracy_saves() == []

    def test_local_appdata_group_is_scanned(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "LOCALAPPDATA" / "skidrow" / "Some Game", {"slot.dat": "a"})
        assert only(scan_piracy_saves()).source == "SkidRow (piracy)"

    def test_public_documents_group_is_scanned(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "PUBLIC" / "Documents" / "OnlineFix" / "Some Game",
                        {"slot.dat": "a"})
        assert only(scan_piracy_saves()).source == "OnlineFix (piracy)"

    def test_steam_coedx_group_is_scanned(self, windows_env, write_save_tree):
        write_save_tree(windows_env / "APPDATA" / "Steam" / "CODEX" / "Some Game",
                        {"slot.dat": "a"})
        assert only(scan_piracy_saves()).source == "CODEX (piracy)"

    def test_missing_group_folders_are_simply_skipped(self, windows_env):
        assert scan_piracy_saves() == []

    def test_nothing_when_the_variables_are_unset(self, windows_env, monkeypatch):
        for name in ("APPDATA", "LOCALAPPDATA", "PUBLIC"):
            monkeypatch.delenv(name, raising=False)
        assert scan_piracy_saves() == []


class TestScanTenokeAndUniverse:
    def test_finds_a_tenoke_release(
            self, tmp_path, pretend_windows, write_save_tree):
        pretend_windows()
        root = write_save_tree(tmp_path, {"Games/Cool Game/TENOKE/slot.dat": "a"})
        candidate = only(scan_tenoke_and_universe([str(root)]))
        assert candidate.name == "Cool Game"
        assert candidate.source == "TENOKE (piracy)"
        assert candidate.save_path == str(root / "Games" / "Cool Game" / "TENOKE")

    @pytest.mark.parametrize("marker", ["UniverseLANData", "UniverseLAN"])
    def test_finds_a_universelan_release(
            self, tmp_path, pretend_windows, write_save_tree, marker):
        pretend_windows()
        root = write_save_tree(tmp_path, {f"Games/Cool Game/{marker}/slot.dat": "a"})
        assert only(scan_tenoke_and_universe([str(root)])).source == "UniverseLAN (piracy)"

    def test_release_without_save_data_is_skipped(
            self, tmp_path, pretend_windows, write_save_tree):
        pretend_windows()
        root = write_save_tree(tmp_path, {"Games/Cool Game/TENOKE/.keep": ""})
        assert scan_tenoke_and_universe([str(root)]) == []

    def test_markets_further_down_the_tree_are_skipped(
            self, tmp_path, pretend_windows, write_save_tree):
        # Depth-limited so a large library doesn't take minutes to walk.
        pretend_windows()
        root = write_save_tree(tmp_path, {"a/b/c/d/Games/Deep/TENOKE/slot.dat": "x"})
        assert scan_tenoke_and_universe([str(root)]) == []

    def test_missing_roots_are_skipped(self, tmp_path, pretend_windows):
        pretend_windows()
        assert scan_tenoke_and_universe([str(tmp_path / "D:\\Games")]) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="asserts the non-Windows branch")
    def test_does_nothing_off_windows(self, tmp_path, write_save_tree):
        root = write_save_tree(tmp_path, {"Games/Cool Game/TENOKE/slot.dat": "a"})
        assert scan_tenoke_and_universe([str(root)]) == []


# ---------------------------------------------------------------------------
# the whole discovery pass
# ---------------------------------------------------------------------------

class TestDiscoverCandidates:
    @pytest.fixture
    def world(self, tmp_path, write_save_tree):
        """A home directory containing one of everything the scanner looks at."""
        home = tmp_path / "home"
        steam = make_steam_install(
            home / ".local" / "share" / "Steam",
            apps={"1091500": "Cyberpunk 2077", "620": "Portal 2"},
            userdata={"123": {"1091500": {"remote": {"slot.dat": "steam save"}}}},
            compatdata={"620": {
                os.path.join("Documents", "My Games", "Portal"): {"slot.sav": "proton save"},
            }},
        )
        write_save_tree(home, {
            os.path.join("Documents", "My Games", "Hades", "slot.sav"): "a",
            os.path.join("Saved Games", "Hollow Knight", "save.dat"): "b",
            os.path.join("Documents", "CAPCOM", "OnimushaWotS", "backup", "slot.dat"): "c",
            os.path.join("Documents", "Some Indie Game", "data.sav"): "d",
            os.path.join("Documents", "My Games"): None,      # excluded from Documents
            os.path.join("AppData", "Local", "SomeGame", "Saves", "s.sav"): "e",
            os.path.join("AppData", "Local", "Google", "Chrome", "Saves", "s.sav"): "no",
            os.path.join(".local", "share", "Hades Native", "save.dat"): "f",
            os.path.join(".local", "share", "trash", "x"): "no",
            os.path.join(".config", "dconf", "x"): "no",
        })
        return home, steam

    def _discover(self, world, existing=()):
        home_dir, steam = world
        return discover_candidates(
            set(existing),
            home=str(home_dir),
            appdata_local=str(home_dir / "AppData" / "Local"),
            steam_roots=[str(steam)],
            xdg_data_home=str(home_dir / ".local" / "share"),
            xdg_config_home=str(home_dir / ".config"),
        )

    def test_finds_games_across_every_location(self, world):
        candidates = self._discover(world)
        found = names_of(candidates)
        for expected in [
            "Hades",              # My Games
            "Hollow Knight",      # Saved Games
            "OnimushaWotS",       # Documents publisher folder
            "Some Indie Game",    # plain Documents
            "SomeGame",           # AppData with a Saves subfolder
            "Hades Native",       # ~/.local/share
            "Cyberpunk 2077",     # Steam userdata
            "Portal 2",           # Proton prefix
        ]:
            assert expected in found, f"{expected} missing from {found}"

    def test_confidence_matches_the_location(self, world):
        candidates = {c.name: c for c in self._discover(world)}
        assert candidates["Hades"].confidence == "high"
        assert candidates["Cyberpunk 2077"].confidence == "high"
        assert candidates["Portal 2"].confidence == "high"
        assert candidates["Some Indie Game"].confidence == "low"
        assert candidates["SomeGame"].confidence == "low"
        assert candidates["Hades Native"].confidence == "low"

    def test_sources_are_reported(self, world):
        sources = {c.name: c.source for c in self._discover(world)}
        assert sources["Hades"] == "My Games"
        assert sources["Hollow Knight"] == "Saved Games"
        assert sources["Cyberpunk 2077"] == "Steam"
        assert sources["Portal 2"] == "Proton"
        assert sources["SomeGame"] == "AppData"
        assert sources["Hades Native"] == "Local Data"

    def test_blocklisted_folders_never_surface(self, world):
        found = names_of(self._discover(world))
        assert "Chrome" not in found
        assert "trash" not in found
        assert "dconf" not in found
        assert "My Games" not in found

    def test_already_tracked_games_are_excluded(self, world):
        home, _steam = world
        tracked = str(home / "Documents" / "My Games" / "Hades")
        found = names_of(self._discover(world, existing=[tracked]))
        assert "Hades" not in found
        assert "Hollow Knight" in found

    def test_tracked_paths_are_compared_case_insensitively_where_it_matters(self, world):
        home, _steam = world
        tracked = str(home / "Documents" / "My Games" / "Hades").upper()
        if tracked == str(home / "Documents" / "My Games" / "Hades"):
            pytest.skip("filesystem path has no case to fold on this platform")
        assert "Hades" not in names_of(self._discover(world, existing=[tracked]))

    def test_no_duplicate_save_paths(self, world):
        candidates = self._discover(world)
        keys = [os.path.normcase(os.path.normpath(c.save_path)) for c in candidates]
        assert len(keys) == len(set(keys))

    def test_every_candidate_has_existing_content(self, world):
        for candidate in self._discover(world):
            assert _has_any_files(candidate.save_path), candidate.save_path

    def test_an_empty_home_produces_nothing(self, tmp_path):
        assert discover_candidates(
            set(),
            home=str(tmp_path),
            appdata_local=str(tmp_path / "nothing"),
            steam_roots=[str(tmp_path / "no-steam")],
            xdg_data_home=str(tmp_path / "no-data"),
            xdg_config_home=str(tmp_path / "no-config"),
        ) == []

    def test_missing_directories_are_not_an_error(self, tmp_path):
        # Nothing on this machine has to exist for the scan to succeed.
        assert discover_candidates(
            set(),
            home=str(tmp_path / "no-such-home"),
            appdata_local=str(tmp_path / "no-such-appdata"),
            steam_roots=[str(tmp_path / "no-such-steam")],
            xdg_data_home=str(tmp_path / "no-such-data"),
            xdg_config_home=str(tmp_path / "no-such-config"),
        ) == []

    def test_candidate_is_a_plain_value_object(self, tmp_path, write_save_tree):
        home = write_save_tree(tmp_path / "home", {
            os.path.join("Documents", "My Games", "Hades", "slot.sav"): "a",
        })
        candidate = only(discover_candidates(
            set(), home=str(home),
            appdata_local=str(home / "AppData"), steam_roots=[],
            xdg_data_home=str(home / ".local"), xdg_config_home=str(home / ".config"),
        ))
        assert isinstance(candidate, ScanCandidate)
        assert candidate == ScanCandidate(
            name="Hades", save_path=str(home / "Documents" / "My Games" / "Hades"),
            source="My Games", confidence="high",
        )


class TestGameScanWorker:
    def test_reports_what_the_scanner_found(self, qapp, monkeypatch):
        expected = [ScanCandidate("Hades", "/saves/hades", "My Games", "high")]
        monkeypatch.setattr(game_scanner, "discover_candidates", lambda paths: expected)

        worker = game_scanner.GameScanWorker({"/already/tracked"})
        captured = []
        worker.scan_finished.connect(captured.append)
        worker.run()

        assert captured == [expected]

    def test_passes_the_existing_paths_through(self, qapp, monkeypatch):
        seen = {}

        def capture(paths):
            seen["paths"] = paths
            return []

        monkeypatch.setattr(game_scanner, "discover_candidates", capture)
        game_scanner.GameScanWorker({"/a", "/b"}).run()
        assert seen["paths"] == {"/a", "/b"}

    def test_reports_a_failure_instead_of_crashing(self, qapp, monkeypatch):
        def exploding(*args, **kwargs):
            raise RuntimeError("scan blew up")

        monkeypatch.setattr(game_scanner, "discover_candidates", exploding)

        worker = game_scanner.GameScanWorker(set())
        errors, results = [], []
        worker.scan_error.connect(errors.append)
        worker.scan_finished.connect(results.append)
        worker.run()

        assert results == []
        assert errors == ["scan blew up"]

    def test_does_not_shadow_qthreads_own_finished_signal(self, qapp):
        # QThread.finished fires only once the OS thread has terminated; callers
        # rely on it to safely drop their reference to the worker.
        worker = game_scanner.GameScanWorker(set())
        assert "finished" not in type(worker).__dict__
        assert hasattr(worker.finished, "connect")