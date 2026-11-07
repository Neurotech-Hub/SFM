# Base Station Deployment — CAN HAT bring-up

One-time system configuration to bring up the MCP2515 CAN controller on the
custom base station HAT as a standard SocketCAN `can0` interface. Do this
once per Raspberry Pi before running the SFM Developer GUI (`packages/dev_gui`)
against real hardware.

## Hardware reference

| Signal        | RPi GPIO | Notes                                    |
|---------------|----------|-------------------------------------------|
| SPI CE0       | 8        | MCP2515 chip select                       |
| SPI MISO      | 9        | MCP2515 data in                           |
| SPI MOSI      | 10       | MCP2515 data out                          |
| SPI SCK       | 11       | MCP2515 clock                             |
| MCP2515 INT   | 5        | Active-low interrupt                      |
| Crystal       | —        | 12 MHz (Y1 on schematic)                  |

## Quick setup (recommended)

[setup-can.sh](setup-can.sh) automates everything below: it appends the
device tree overlay, installs the systemd-networkd unit, installs
`can-utils`, and brings up `can0`. On a fresh Pi the overlay only takes
effect after a reboot, so the script reboots itself once automatically —
run it again afterward to confirm everything came up:

```bash
cd packages/dev_gui/deploy

# First run: configures the Pi and reboots automatically if needed
sudo ./setup-can.sh

# After the Pi comes back up, confirm can0 is healthy
sudo ./setup-can.sh --verify
```

`--verify` is read-only and safe to run any time — it checks SPI/overlay
config, the kernel module, `can-utils`, systemd-networkd, and that `can0` is
up at the right bitrate, printing a PASS/FAIL line for each. It exits
non-zero if anything is wrong, which is handy for scripting.

The rest of this document explains what the script does manually, step by
step, for reference or troubleshooting.

## Manual setup (reference / troubleshooting)

### 1. Enable SPI + the MCP2515 device tree overlay

Append [boot-config-can.append.txt](boot-config-can.append.txt) to
`/boot/firmware/config.txt`:

```bash
sudo tee -a /boot/firmware/config.txt < boot-config-can.append.txt
sudo reboot
```

After reboot, confirm the kernel driver probed the chip:

```bash
dmesg | grep -i mcp251x
ls /dev/spi*      # SPI bus present
ip link show can0        # can0 interface exists
```

### 2. Bring up `can0` persistently

Install the systemd-networkd unit [80-can.network](80-can.network):

```bash
sudo cp 80-can.network /etc/systemd/network/80-can.network
sudo systemctl enable --now systemd-networkd
sudo systemctl restart systemd-networkd
ip -details link show can0   # should report state UP, bitrate 250000
```

If you are not using systemd-networkd, bring the interface up manually
instead (not persistent across reboots):

```bash
sudo ip link set can0 up type can bitrate 250000
```

### 3. Verify with `candump` / `cansend`

```bash
sudo apt install can-utils   # if not already installed

# Terminal 1
candump can0

# Terminal 2 (loopback test, no nodes required)
sudo ip link set can0 type can bitrate 250000 loopback on
sudo ip link set can0 up
cansend can0 123#DEADBEEF   # should be echoed back in Terminal 1

# Restore normal (non-loopback) operation
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 250000 loopback off
sudo ip link set can0 up
```

With at least one SFM node powered and connected, `candump can0` should show
`080` (ANNOUNCE) or `083` (REJOIN) discovery frames, followed by `2xx`
heartbeat frames at 1 Hz once the node is assigned an ID.

## Run the Rig Map as services

The Rig Map has two parts that run as separate programs, so that one can restart
without disturbing the other and a browser that closes or crashes never stops a
session:

| Program | What it does | Restarts |
|---|---|---|
| `sfm-engine` | Owns the CAN bus and the BNC lines, runs experiments, writes the log | by systemd after a crash |
| `sfm-gateway` | The web page at `http://127.0.0.1:8080/` (this Pi only) | always |
| `sfm-kiosk` (optional) | Opens the page full screen and restarts the browser if it dies or grows past 1 GB | by itself |

```bash
cd packages/dev_gui/deploy
./install-gui.sh                 # writes the two units for your user (no sudo); changes nothing else
systemctl --user start sfm-engine sfm-gateway
# open http://127.0.0.1:8080/
```

`./install-gui.sh --help` lists the options. Nothing starts at boot and nothing is
added to the desktop unless you ask:

| Option | Effect |
|---|---|
| `--enable` | start both services at login. Add `loginctl enable-linger $USER` to start them at boot |
| `--kiosk` | open the page full screen when the desktop starts (writes `~/.config/autostart/sfm-kiosk.desktop`) |
| `--system` | install system-wide instead of for your user (needs sudo) |
| `--python PATH` | the Python to run (default: `python3` on the PATH). It needs `aiohttp` and `python-can` |
| `--port N` | the page's port (default 8080) |
| `--uninstall` | stop, disable and remove what this script installed. Logs and `~/.sfm` are not touched |
| `--dry-run` | show what would happen, change nothing |

**The old GUI and the engine cannot run together.** They share the CAN bus and the
BNC lines, and the second one to start refuses and says who has them. To use the
old DearPyGui app: `systemctl --user stop sfm-engine`, then `python run.py`.

### What happens when something stops

| What stops | What you see | The session |
|---|---|---|
| The browser closes or crashes | Nothing on the rig. Open it again and it is on the running session | keeps running |
| The gateway crashes | Back in about 3 s. An open page shows "engine not running" briefly, then reconnects | keeps running |
| The engine crashes | Back in about 4 s. The page returns to Connect. Connect again (your choices are remembered) and start the same session name to continue it as the next run | **ends**. Everything logged is on disk; at most the last second of rows can be missing |
| `systemctl stop sfm-engine` | The session is closed properly with a `session_end` row | ends cleanly |

### Logs and settings

```bash
journalctl --user -u sfm-engine -u sfm-gateway -f      # system install: drop --user
```

Settings live in `~/.sfm/` (`mac_id_registry.json`, `bnc_settings.json`,
`node_layout.json`, `gui_settings.json`). Session logs go to the folder chosen on the
Connect step.

### Measuring the load

`bench/load_monitor.py` reports CPU (as a share of the whole machine) and real memory
(PSS) per service and for the browser. Leave it running for a day to look for leaks:

```bash
python -m bench.load_monitor --group engine=unit:sfm-engine --group gateway=unit:sfm-gateway \
    --group browser=match:--user-data-dir=$HOME/.config/sfm-kiosk --interval 30 --duration 0 --out soak.csv
```

## Run the old GUI

Once `can0` is up, the SFM Developer GUI works exactly as documented in
[packages/dev_gui/README.md](../README.md) — no additional configuration is
needed; `CanManager` opens `can0` directly via SocketCAN.

## Interactive hardware validation

For a full pin-by-pin checklist (CAN, AEO, BNC IN/OUT, button), run:

```bash
cd packages/dev_gui
python tests/test_hat.py
```

See [tests/test_hat.py](../tests/test_hat.py) for details.
