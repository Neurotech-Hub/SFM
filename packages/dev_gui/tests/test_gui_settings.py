"""Remembered Connect-step values: checked, saved atomically, never fatal."""

from __future__ import annotations

import json

import pytest

from base_station import gui_settings
from base_station.gui_settings import GuiSettings


def test_values_are_saved_and_come_back(tmp_path) -> None:
    path = tmp_path / "gui.json"
    GuiSettings(path).update({"interface": "can0", "num_nodes": 6, "log_dir": "/media/x/sfm_logs",
                              "auto_save": False, "last_experiment": "free_feeding"})
    again = GuiSettings(path).all()
    assert again == {"interface": "can0", "num_nodes": 6, "log_dir": "/media/x/sfm_logs",
                     "auto_save": False, "last_experiment": "free_feeding"}


def test_update_merges_with_what_is_already_saved(tmp_path) -> None:
    s = GuiSettings(tmp_path / "gui.json")
    s.update({"interface": "can0"})
    s.update({"num_nodes": 3})
    assert s.all() == {"interface": "can0", "num_nodes": 3}


@pytest.mark.parametrize("values", [
    {"interface": "../etc"}, {"interface": ""}, {"interface": 5}, {"interface": "x" * 40},
    {"num_nodes": 0}, {"num_nodes": 13}, {"num_nodes": "3"}, {"num_nodes": True}, {"num_nodes": 2.5},
    {"log_dir": ""}, {"log_dir": "a\0b"}, {"log_dir": "x" * 600},
    {"auto_save": "yes"}, {"auto_save": 1},
    {"last_experiment": "a b"}, {"last_experiment": None},
    {"unknown_key": 1},
])
def test_bad_values_are_refused_and_nothing_is_written(tmp_path, values) -> None:
    path = tmp_path / "gui.json"
    s = GuiSettings(path)
    with pytest.raises(ValueError):
        s.update(values)
    assert s.all() == {} and not path.exists()


def test_one_bad_field_in_an_update_changes_nothing(tmp_path) -> None:
    s = GuiSettings(tmp_path / "gui.json")
    s.update({"interface": "can0"})
    with pytest.raises(ValueError):
        s.update({"interface": "vcan0", "num_nodes": 99})
    assert s.all() == {"interface": "can0"}


def test_a_damaged_file_is_set_aside(tmp_path) -> None:
    path = tmp_path / "gui.json"
    path.write_text("{ nope", encoding="utf-8")
    s = GuiSettings(path)
    assert s.all() == {} and "could not be read" in s.load_problem
    assert (tmp_path / "gui.json.bad").exists()
    s.update({"interface": "can0"})
    assert GuiSettings(path).all() == {"interface": "can0"}


def test_bad_values_in_a_hand_edited_file_are_skipped_one_by_one(tmp_path) -> None:
    path = tmp_path / "gui.json"
    path.write_text(json.dumps({"version": 1, "interface": "can0", "num_nodes": 99,
                                "log_dir": "", "auto_save": True, "extra": 1}), encoding="utf-8")
    assert GuiSettings(path).all() == {"interface": "can0", "auto_save": True}


def test_a_failed_write_keeps_the_old_values(tmp_path, monkeypatch) -> None:
    path = tmp_path / "gui.json"
    s = GuiSettings(path)
    s.update({"interface": "can0"})
    before = path.read_text()
    monkeypatch.setattr(gui_settings.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        s.update({"interface": "vcan0"})
    assert s.all() == {"interface": "can0"} and path.read_text() == before
