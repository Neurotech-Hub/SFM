"""The service installer and the units it writes. Nothing here touches the real systemd."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("bash") is None, reason="bash + Linux")

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
INSTALL = DEPLOY / "install-gui.sh"


def run(*args, home: Path):
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    return subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, env=env, timeout=60)


def test_user_install_writes_two_units_and_nothing_else(tmp_path) -> None:
    units = tmp_path / "units"
    r = run("--unit-dir", str(units), home=tmp_path)
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in units.iterdir()) == ["sfm-engine.service", "sfm-gateway.service"]
    assert not (tmp_path / ".config" / "autostart").exists(), "no desktop autostart without --kiosk"
    for p in units.iterdir():
        text = p.read_text()
        assert not re.search(r"@[A-Z_]+@", text), f"{p.name} still has a placeholder"
        assert "User=" not in text and "WantedBy=default.target" in text


def test_the_units_run_the_right_modules_with_a_shared_socket(tmp_path) -> None:
    units = tmp_path / "units"
    run("--unit-dir", str(units), "--port", "9090", "--python", sys.executable, home=tmp_path)
    engine = (units / "sfm-engine.service").read_text()
    gateway = (units / "sfm-gateway.service").read_text()
    assert f"ExecStart={sys.executable} -m base_station.service --socket %t/sfm/engine.sock" in engine
    assert f"ExecStart={sys.executable} -m base_station.gateway --port 9090 --engine-socket %t/sfm/engine.sock" in gateway
    assert str(DEPLOY.parent) in engine and f"WorkingDirectory={DEPLOY.parent}" in gateway
    assert "Restart=on-failure" in engine and "KillSignal=SIGTERM" in engine
    assert "Restart=always" in gateway
    # Restarting one must not stop the other.
    assert "Wants=sfm-engine.service" in gateway and "Requires=" not in gateway and "BindsTo=" not in gateway
    # Real-time priority is opt-in: nothing about it unless --realtime is given.
    exec_line = next(l for l in engine.splitlines() if l.startswith("ExecStart="))
    assert "--realtime" not in exec_line and "\nLimitRTPRIO" not in engine and "\nCPUSchedulingPolicy=" not in engine


def test_system_install_runs_as_the_user_and_starts_at_boot(tmp_path) -> None:
    units = tmp_path / "units"
    r = run("--system", "--unit-dir", str(units), home=tmp_path)
    assert r.returncode == 0, r.stderr
    for name in ("sfm-engine.service", "sfm-gateway.service"):
        text = (units / name).read_text()
        assert re.search(r"^User=\S+$", text, re.M) and re.search(r"^Group=\S+$", text, re.M)
        assert "WantedBy=multi-user.target" in text


def test_a_system_install_without_root_is_refused(tmp_path) -> None:
    if os.geteuid() == 0:
        pytest.skip("running as root")
    r = run("--system", home=tmp_path)
    assert r.returncode != 0 and "sudo" in r.stderr


def test_dry_run_changes_nothing(tmp_path) -> None:
    units = tmp_path / "units"
    r = run("--dry-run", "--kiosk", "--unit-dir", str(units), home=tmp_path)
    assert r.returncode == 0 and "[dry run]" in r.stdout
    assert not units.exists() and not (tmp_path / ".config").exists()


def test_the_kiosk_entry_is_written_only_when_asked_and_removed_by_uninstall(tmp_path) -> None:
    units = tmp_path / "units"
    run("--unit-dir", str(units), "--kiosk", "--port", "8081", home=tmp_path)
    desktop = tmp_path / ".config" / "autostart" / "sfm-kiosk.desktop"
    text = desktop.read_text()
    assert "base_station.kiosk --url http://127.0.0.1:8081/" in text and not re.search(r"@[A-Z_]+@", text)
    run("--uninstall", "--unit-dir", str(units), home=tmp_path)
    assert not desktop.exists() and not list(units.glob("*.service"))


def test_uninstall_leaves_settings_and_logs_alone(tmp_path) -> None:
    sfm = tmp_path / ".sfm"
    sfm.mkdir()
    (sfm / "mac_id_registry.json").write_text("{}")
    units = tmp_path / "units"
    run("--unit-dir", str(units), home=tmp_path)
    r = run("--uninstall", "--unit-dir", str(units), home=tmp_path)
    assert r.returncode == 0 and (sfm / "mac_id_registry.json").exists()


def test_an_unknown_option_is_refused(tmp_path) -> None:
    r = run("--frobnicate", home=tmp_path)
    assert r.returncode == 2


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
@pytest.mark.parametrize("mode", ["user", "system"])
def test_systemd_accepts_the_generated_units(tmp_path, mode) -> None:
    units = tmp_path / "units"
    args = ["--unit-dir", str(units)] + (["--system"] if mode == "system" else [])
    run(*args, "--python", sys.executable, home=tmp_path)
    cmd = ["systemd-analyze"] + (["--user"] if mode == "user" else []) + ["verify", str(units / "sfm-engine.service"), str(units / "sfm-gateway.service")]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    # Unit-file mistakes are what verify exists to find. Ignore only the notice that a
    # unit's [Install] target is not loaded in this isolated check.
    problems = [l for l in (r.stdout + r.stderr).splitlines() if l.strip() and "default.target" not in l and "multi-user.target" not in l]
    assert not problems, "\n".join(problems)


def test_realtime_is_off_unless_asked_and_the_default_run_says_nothing_about_it(tmp_path) -> None:
    r = run("--unit-dir", str(tmp_path / "u"), home=tmp_path)
    assert "real-time" not in r.stdout.lower().split("next steps")[0], r.stdout


def test_realtime_adds_the_engine_option_and_for_a_user_install_prints_the_one_step_needed(tmp_path) -> None:
    units = tmp_path / "u"
    r = run("--unit-dir", str(units), "--realtime", "50", home=tmp_path)
    assert r.returncode == 0, r.stderr
    engine = (units / "sfm-engine.service").read_text()
    assert "base_station.service --socket %t/sfm/engine.sock --realtime 50" in engine
    assert "\nLimitRTPRIO" not in engine, "a user service gets its limit from the login session, not the unit"
    assert "(gateway)" not in engine and "--realtime" not in (units / "sfm-gateway.service").read_text()
    assert "limits.d/90-sfm-realtime.conf" in r.stdout and "rtprio 50" in r.stdout
    assert "normal priority" in r.stdout, "it says what happens until the limit is in place"
    assert not (tmp_path / "etc").exists(), "the script never writes /etc itself"


def test_realtime_in_a_system_service_sets_the_limit_in_the_unit(tmp_path) -> None:
    units = tmp_path / "u"
    r = run("--system", "--unit-dir", str(units), "--realtime", "60", home=tmp_path)
    engine = (units / "sfm-engine.service").read_text()
    assert "--realtime 60" in engine and "\nLimitRTPRIO=60\n" in engine
    assert "limits.d" not in r.stdout


@pytest.mark.parametrize("bad", ["0x", "abc", "100", "-5", ""])
def test_a_bad_realtime_priority_is_refused(tmp_path, bad) -> None:
    r = run("--unit-dir", str(tmp_path / "u"), "--realtime", bad, home=tmp_path)
    assert r.returncode == 2 and "1 to 99" in r.stderr
    assert not (tmp_path / "u").exists()


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="no systemd-analyze")
def test_systemd_accepts_a_system_unit_with_the_realtime_limit(tmp_path) -> None:
    units = tmp_path / "units"
    run("--system", "--unit-dir", str(units), "--realtime", "50", "--python", sys.executable, home=tmp_path)
    r = subprocess.run(["systemd-analyze", "verify", str(units / "sfm-engine.service"), str(units / "sfm-gateway.service")],
                       capture_output=True, text=True, timeout=60)
    problems = [l for l in (r.stdout + r.stderr).splitlines() if l.strip() and "multi-user.target" not in l]
    assert not problems, "\n".join(problems)
