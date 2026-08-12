"""Tests for base_station.protocol — frame encoding/decoding round-trips."""

import sys
import os
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from base_station.protocol import (
    CanCmd,
    CanEvent,
    InputId,
    DispenseState,
    ServiceStatus,
    CAN_CMD_BASE,
    CAN_STATUS_BASE,
    CAN_EVENT_BASE,
    CAN_ID_ANNOUNCE,
    CAN_ID_ASSIGN,
    CAN_ID_ACK,
    CAN_ID_REJOIN,
    CAN_EVENT_DISPLAY_NAME,
    build_cmd_frame,
    build_assign_frame,
    build_heartbeat_frame,
    build_event_frame,
    parse_heartbeat,
    parse_event,
    parse_input_changed,
    parse_fault_code,
    parse_discovery,
    classify_frame,
    format_mac,
    node_id_from_hb_id,
    node_id_from_event_id,
    HeartbeatPayload,
)


class TestBuildCmdFrame:
    def test_unicast(self):
        arb_id, data = build_cmd_frame(3, CanCmd.Dispense)
        assert arb_id == CAN_CMD_BASE + 3
        assert data[0] == CanCmd.Dispense

    def test_broadcast(self):
        arb_id, data = build_cmd_frame(0, CanCmd.Recover)
        assert arb_id == CAN_CMD_BASE  # 0x100
        assert data[0] == CanCmd.Recover

    def test_assign_id_payload(self):
        arb_id, data = build_cmd_frame(5, CanCmd.AssignId, bytes([7]))
        assert arb_id == CAN_CMD_BASE + 5
        assert data[0] == CanCmd.AssignId
        assert data[1] == 7

    def test_clear_id_broadcast(self):
        arb_id, data = build_cmd_frame(0, CanCmd.ClearId)
        assert arb_id == CAN_CMD_BASE  # 0x100 broadcast
        assert data[0] == CanCmd.ClearId
        assert CanCmd.ClearId == 0x07

    def test_max_8_bytes(self):
        _, data = build_cmd_frame(1, CanCmd.SetConfig, bytes(range(20)))
        assert len(data) <= 8


class TestBuildAssignFrame:
    def test_basic(self):
        mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
        arb_id, data = build_assign_frame(mac, 1)
        assert arb_id == CAN_ID_ASSIGN
        assert data[:6] == mac
        assert data[6] == 1

    def test_various_ids(self):
        mac = bytes([0x11] * 6)
        for nid in [1, 127, 254]:
            _, data = build_assign_frame(mac, nid)
            assert data[6] == nid

    def test_bad_mac_length(self):
        with pytest.raises(AssertionError):
            build_assign_frame(bytes(5), 1)

    def test_bad_node_id(self):
        mac = bytes(6)
        with pytest.raises(AssertionError):
            build_assign_frame(mac, 0)
        with pytest.raises(AssertionError):
            build_assign_frame(mac, 255)


class TestParseHeartbeat:
    def _make_data(self, state=0, presence=0, sensor_bits=0, fault=0):
        return bytes([state, 0, 0, presence, sensor_bits, fault, 0, 0])

    def test_idle(self):
        hb = parse_heartbeat(self._make_data(state=0))
        assert hb is not None
        assert hb.dispense_state == DispenseState.Idle
        assert hb.mouse_presence is False
        assert hb.pellet is False

    def test_sensor_bits(self):
        hb = parse_heartbeat(self._make_data(sensor_bits=0b101))  # pellet + dome
        assert hb.pellet is True
        assert hb.load_position is False
        assert hb.dome_open is True

    def test_presence(self):
        hb = parse_heartbeat(self._make_data(presence=1))
        assert hb.mouse_presence is True

    def test_fault_state(self):
        hb = parse_heartbeat(self._make_data(state=6, fault=2))  # Fault, Jam
        assert hb.dispense_state == DispenseState.Fault
        assert hb.fault_code == ServiceStatus.Jam

    def test_too_short(self):
        assert parse_heartbeat(bytes(3)) is None

    def test_unknown_state_defaults_to_idle(self):
        hb = parse_heartbeat(bytes([0xFF, 0, 0, 0, 0, 0, 0, 0]))
        assert hb is not None
        assert hb.dispense_state == DispenseState.Idle


class TestParseEvent:
    def test_on_plate(self):
        ev = parse_event(bytes([CanEvent.OnPlate]))
        assert ev is not None
        assert ev.event == CanEvent.OnPlate

    def test_pong(self):
        ev = parse_event(bytes([CanEvent.Pong]))
        assert ev.event == CanEvent.Pong

    def test_extra_bytes(self):
        ev = parse_event(bytes([CanEvent.OnPlate, 0x12, 0x00]))
        assert ev.raw_extra == bytes([0x12, 0x00])

    def test_empty(self):
        assert parse_event(b"") is None

    def test_unknown_event(self):
        assert parse_event(bytes([0xFF])) is None

    def test_input_changed(self):
        ev = parse_event(bytes([CanEvent.InputChanged, InputId.Pellet, 1]))
        changed = parse_input_changed(ev)
        assert changed is not None
        assert changed.input_id == InputId.Pellet
        assert changed.active is True

    def test_input_changed_rejects_short_payload(self):
        ev = parse_event(bytes([CanEvent.InputChanged, InputId.LoadPosition]))
        assert parse_input_changed(ev) is None

    def test_phase_events(self):
        for ev_type, code in (
            (CanEvent.Seeking, 0x0D),
            (CanEvent.Lowering, 0x07),
            (CanEvent.Loading, 0x08),
            (CanEvent.Raising, 0x09),
        ):
            assert ev_type == code
            ev = parse_event(bytes([ev_type]))
            assert ev is not None
            assert ev.event == ev_type

    def test_phase_display_names(self):
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.OnPlate] == "Pellet OnPlate"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.Loaded] == "Loaded"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.Seeking] == "Seeking"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.DomeOpened] == "Dome Opened"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.PelletTaken] == "Pellet Taken"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.Lowering] == "Lowering"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.Loading] == "Loading"
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.Raising] == "Raising"

    def test_dome_open_warning_event(self):
        assert CanEvent.DomeOpenWarning == 0x0A
        ev = parse_event(bytes([CanEvent.DomeOpenWarning]))
        assert ev is not None
        assert ev.event == CanEvent.DomeOpenWarning
        assert CAN_EVENT_DISPLAY_NAME[CanEvent.DomeOpenWarning] == "DomeOpenWarning"

    def test_fault_payload_jam_opcode(self):
        # Regression guard: ServiceStatus must match ServiceTypes.h exactly.
        # Commit 4861a97 removed firmware's `Timeout` member (value 2) without
        # updating this mirror, so wire value 2 now means Jam, not Timeout.
        ev = parse_event(bytes([CanEvent.Fault, 2]))
        assert ev is not None
        assert parse_fault_code(ev) == ServiceStatus.Jam

    def test_fault_payload_feed_and_actuator_timeout(self):
        from base_station.protocol import fault_user_message
        assert ServiceStatus.FeedTimeout == 5
        assert ServiceStatus.ActuatorTimeout == 6
        feed = parse_event(bytes([CanEvent.Fault, ServiceStatus.FeedTimeout]))
        act = parse_event(bytes([CanEvent.Fault, ServiceStatus.ActuatorTimeout]))
        assert parse_fault_code(feed) == ServiceStatus.FeedTimeout
        assert parse_fault_code(act) == ServiceStatus.ActuatorTimeout
        assert "pellet" in fault_user_message(ServiceStatus.FeedTimeout).lower()
        assert "actuator" in fault_user_message(ServiceStatus.ActuatorTimeout).lower()

    def test_fault_payload_jam(self):
        ev = parse_event(bytes([CanEvent.Fault, ServiceStatus.Jam]))
        assert parse_fault_code(ev) == ServiceStatus.Jam

    def test_fault_payload_missing_extra(self):
        ev = parse_event(bytes([CanEvent.Fault]))
        assert parse_fault_code(ev) is None

    def test_fault_payload_ignores_non_fault(self):
        ev = parse_event(bytes([CanEvent.Loaded, ServiceStatus.Jam]))
        assert parse_fault_code(ev) is None

    def test_dome_opened_and_pellet_taken_opcodes(self):
        assert CanEvent.DomeOpened == 0x03
        assert CanEvent.PelletTaken == 0x0B
        assert CanEvent.FeedSkipped == 0x0C
        assert ServiceStatus.PelletLost == 4

    def test_parse_event_context_dome_opened(self):
        from base_station.protocol import parse_event_context
        ev = parse_event(bytes([CanEvent.DomeOpened, 0x02, 0x00, 0x01]))
        ctx = parse_event_context(ev)
        assert ctx["pellet_count"] == 2
        assert ctx["pellet_present"] is True

    def test_parse_event_context_pellet_taken(self):
        from base_station.protocol import parse_event_context
        ev = parse_event(bytes([CanEvent.PelletTaken, 0x07, 0x00, 0x00]))
        ctx = parse_event_context(ev)
        assert ctx["pellet_count"] == 7
        assert ctx["dome_open"] is False

    def test_heartbeat_pellets_taken(self):
        data = bytes([0, 3, 0, 0, 0, 0, 5, 0])
        hb = parse_heartbeat(data)
        assert hb.pellets_presented == 3
        assert hb.pellets_taken == 5

    def test_build_heartbeat_roundtrip_taken(self):
        hb = HeartbeatPayload(
            dispense_state=DispenseState.Idle,
            mouse_presence=False,
            pellet=True,
            load_position=False,
            dome_open=False,
            fault_code=ServiceStatus.Ok,
            pellets_presented=9,
            pellets_taken=4,
        )
        _, data = build_heartbeat_frame(1, hb)
        parsed = parse_heartbeat(data)
        assert parsed.pellets_presented == 9
        assert parsed.pellets_taken == 4
        assert parsed.pellet is True


class TestParseDiscovery:
    def test_announce(self):
        mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
        info = parse_discovery(CAN_ID_ANNOUNCE, mac)
        assert info is not None
        assert info["mac"] == mac
        assert info["node_id"] is None

    def test_assign(self):
        mac = bytes([0x11] * 6)
        data = mac + bytes([3])
        info = parse_discovery(CAN_ID_ASSIGN, data)
        assert info["mac"] == mac
        assert info["node_id"] == 3

    def test_rejoin(self):
        mac = bytes([0x22] * 6)
        data = mac + bytes([7])
        info = parse_discovery(CAN_ID_REJOIN, data)
        assert info["node_id"] == 7

    def test_announce_too_short(self):
        assert parse_discovery(CAN_ID_ANNOUNCE, bytes(3)) is None

    def test_assign_too_short(self):
        assert parse_discovery(CAN_ID_ASSIGN, bytes(5)) is None


class TestClassifyFrame:
    def test_heartbeat(self):
        assert classify_frame(CAN_STATUS_BASE + 1) == "HEARTBEAT"
        assert classify_frame(CAN_STATUS_BASE + 9) == "HEARTBEAT"

    def test_event(self):
        assert classify_frame(CAN_EVENT_BASE + 1) == "EVENT"

    def test_command(self):
        assert classify_frame(CAN_CMD_BASE)      == "COMMAND"  # broadcast
        assert classify_frame(CAN_CMD_BASE + 3)  == "COMMAND"

    def test_discovery(self):
        for fid in [CAN_ID_ANNOUNCE, CAN_ID_ASSIGN, CAN_ID_ACK, CAN_ID_REJOIN]:
            assert classify_frame(fid) == "DISCOVERY"

    def test_unknown(self):
        assert classify_frame(0x001) == "UNKNOWN"
        assert classify_frame(0x7FF) == "UNKNOWN"


class TestNodeIdExtraction:
    def test_hb(self):
        assert node_id_from_hb_id(CAN_STATUS_BASE + 5) == 5
        assert node_id_from_hb_id(CAN_STATUS_BASE) is None  # base itself = node 0, invalid

    def test_event(self):
        assert node_id_from_event_id(CAN_EVENT_BASE + 2) == 2


class TestFormatMac:
    def test_format(self):
        mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
        assert format_mac(mac) == "AA:BB:CC:DD:EE:01"

    def test_zeros(self):
        assert format_mac(bytes(6)) == "00:00:00:00:00:00"


class TestServiceStatusMatchesFirmware:
    """
    Regression guard for the ServiceStatus wire-value mismatch (commit
    4861a97 removed firmware's `Timeout` member without updating this
    Python mirror, silently shifting every fault code >= 2 by one).

    Parses the ServiceStatus enum straight out of ServiceTypes.h and checks
    it against the Python mirror value-for-value, so this can't drift again.
    """

    def test_values_match_firmware_header(self):
        import re

        header = (
            Path(__file__).resolve().parents[3]
            / "src" / "services" / "ServiceTypes.h"
        )
        text = header.read_text(encoding="utf-8")
        m = re.search(r"enum class ServiceStatus[^{]*\{([^}]*)\}", text)
        assert m is not None, "ServiceStatus enum not found in ServiceTypes.h"

        firmware_values = {}
        next_value = 0
        for raw_line in m.group(1).splitlines():
            line = raw_line.split("//")[0].strip().rstrip(",")
            if not line:
                continue
            if "=" in line:
                name, value = (p.strip() for p in line.split("="))
                next_value = int(value)
            else:
                name = line
            firmware_values[name] = next_value
            next_value += 1

        python_values = {m.name: m.value for m in ServiceStatus}
        assert python_values == firmware_values


class TestBuildDispenseNoFeed:
    def test_default_dwell(self):
        from base_station.protocol import build_dispense_no_feed, DEFAULT_NO_FEED_DWELL_MS
        payload = build_dispense_no_feed()
        assert len(payload) == 2
        assert payload[0] | (payload[1] << 8) == DEFAULT_NO_FEED_DWELL_MS

    def test_explicit_dwell(self):
        from base_station.protocol import build_dispense_no_feed
        payload = build_dispense_no_feed(1000)
        assert payload[0] | (payload[1] << 8) == 1000

    def test_clamps_below_min(self):
        from base_station.protocol import build_dispense_no_feed, NO_FEED_DWELL_MIN_MS
        payload = build_dispense_no_feed(1)
        assert payload[0] | (payload[1] << 8) == NO_FEED_DWELL_MIN_MS

    def test_clamps_above_max(self):
        from base_station.protocol import build_dispense_no_feed, NO_FEED_DWELL_MAX_MS
        payload = build_dispense_no_feed(999999)
        assert payload[0] | (payload[1] << 8) == NO_FEED_DWELL_MAX_MS

    def test_dispense_no_feed_opcode_and_frame(self):
        from base_station.protocol import CanCmd, build_cmd_frame, build_dispense_no_feed
        assert CanCmd.DispenseNoFeed == 0x08
        arb_id, data = build_cmd_frame(2, CanCmd.DispenseNoFeed, build_dispense_no_feed(6000))
        assert arb_id == CAN_CMD_BASE + 2
        assert data[0] == CanCmd.DispenseNoFeed
        assert data[1] | (data[2] << 8) == 6000


class TestBuildSyncFlash:
    def test_default_duration(self):
        from base_station.protocol import build_sync_flash, DEFAULT_SYNC_FLASH_MS
        payload = build_sync_flash()
        assert len(payload) == 2
        assert payload[0] | (payload[1] << 8) == DEFAULT_SYNC_FLASH_MS

    def test_explicit_duration(self):
        from base_station.protocol import build_sync_flash
        payload = build_sync_flash(750)
        assert payload[0] | (payload[1] << 8) == 750

    def test_clamps_below_min(self):
        from base_station.protocol import build_sync_flash, SYNC_FLASH_MIN_MS
        payload = build_sync_flash(0)
        assert payload[0] | (payload[1] << 8) == SYNC_FLASH_MIN_MS

    def test_clamps_above_max(self):
        from base_station.protocol import build_sync_flash, SYNC_FLASH_MAX_MS
        payload = build_sync_flash(99999)
        assert payload[0] | (payload[1] << 8) == SYNC_FLASH_MAX_MS

    def test_sync_flash_opcode_and_frame(self):
        from base_station.protocol import CanCmd, build_cmd_frame, build_sync_flash
        assert CanCmd.SyncFlash == 0x0A
        arb_id, data = build_cmd_frame(0, CanCmd.SyncFlash, build_sync_flash(500))
        assert arb_id == CAN_CMD_BASE  # broadcast
        assert data[0] == CanCmd.SyncFlash
        assert data[1] | (data[2] << 8) == 500


class TestPresenceCalibration:
    def test_calibrate_presence_opcode(self):
        from base_station.protocol import CanCmd
        assert CanCmd.CalibratePresence == 0x09

    def test_presence_cal_result_opcode(self):
        assert CanEvent.PresenceCalResult == 0x10

    def test_parse_presence_cal_ok(self):
        from base_station.protocol import parse_presence_cal
        # ok=1, threshold=35820 LE32, samples=199 LE16
        extra = bytes([1]) + (35820).to_bytes(4, "little") + (199).to_bytes(2, "little")
        ev = parse_event(bytes([CanEvent.PresenceCalResult]) + extra)
        cal = parse_presence_cal(ev)
        assert cal is not None
        assert cal.ok is True
        assert cal.threshold == 35820
        assert cal.samples == 199

    def test_parse_presence_cal_failed_keeps_threshold(self):
        from base_station.protocol import parse_presence_cal
        extra = bytes([0]) + (35000).to_bytes(4, "little") + (0).to_bytes(2, "little")
        ev = parse_event(bytes([CanEvent.PresenceCalResult]) + extra)
        cal = parse_presence_cal(ev)
        assert cal is not None
        assert cal.ok is False
        assert cal.threshold == 35000

    def test_parse_presence_cal_rejects_wrong_event(self):
        from base_station.protocol import parse_presence_cal
        ev = parse_event(bytes([CanEvent.Loaded, 0x01, 0x00]))
        assert parse_presence_cal(ev) is None

    def test_parse_presence_cal_rejects_short_payload(self):
        from base_station.protocol import parse_presence_cal
        ev = parse_event(bytes([CanEvent.PresenceCalResult, 1, 0x2C]))  # only 2 bytes extra
        assert parse_presence_cal(ev) is None

    def test_parse_event_context_ignores_presence_cal_result(self):
        """Regression guard: a PresenceCalResult payload must never be
        misdecoded as a pellet count just because it has >= 2 extra bytes."""
        from base_station.protocol import parse_event_context
        extra = bytes([1]) + (35820).to_bytes(4, "little") + (199).to_bytes(2, "little")
        ev = parse_event(bytes([CanEvent.PresenceCalResult]) + extra)
        assert parse_event_context(ev) is None

    def test_calibrate_presence_has_purpose_text(self):
        from base_station.protocol import CanCmd, CAN_CMD_PURPOSE
        assert CanCmd.CalibratePresence in CAN_CMD_PURPOSE
        assert CAN_CMD_PURPOSE[CanCmd.CalibratePresence]


class TestConfigApplied:
    def test_config_applied_opcode(self):
        assert CanEvent.ConfigApplied == 0x11

    def test_presence_factor_configtype_opcode(self):
        from base_station.protocol import CONFIG_PRESENCE_FACTOR
        assert CONFIG_PRESENCE_FACTOR == 0x02

    def test_build_setconfig_presence_factor_round_trips_through_config_applied(self):
        from base_station.protocol import (
            build_setconfig_presence_factor,
            parse_config_applied,
            config_applied_factor,
            CONFIG_PRESENCE_FACTOR,
        )
        payload = build_setconfig_presence_factor(10.0)
        assert payload[0] == CONFIG_PRESENCE_FACTOR
        assert len(payload) == 5  # configType(1) + float32(4)

        # Simulate the node echoing the applied value back.
        extra = bytes([CONFIG_PRESENCE_FACTOR, 1]) + payload[1:5]
        ev = parse_event(bytes([CanEvent.ConfigApplied]) + extra)
        applied = parse_config_applied(ev)
        assert applied is not None
        assert applied.config_type == CONFIG_PRESENCE_FACTOR
        assert applied.ok is True
        assert config_applied_factor(applied) == pytest.approx(10.0)

    def test_build_setconfig_presence_factor_clamps_to_firmware_range(self):
        from base_station.protocol import (
            build_setconfig_presence_factor,
            PRESENCE_FACTOR_MIN,
            PRESENCE_FACTOR_MAX,
        )
        import struct
        low = struct.unpack("<f", build_setconfig_presence_factor(-5.0)[1:5])[0]
        high = struct.unpack("<f", build_setconfig_presence_factor(999.0)[1:5])[0]
        assert low == pytest.approx(PRESENCE_FACTOR_MIN)
        assert high == pytest.approx(PRESENCE_FACTOR_MAX)

    def test_parse_config_applied_rejects_wrong_event(self):
        from base_station.protocol import parse_config_applied
        ev = parse_event(bytes([CanEvent.Loaded, 0x01, 0x00]))
        assert parse_config_applied(ev) is None

    def test_parse_config_applied_rejects_short_payload(self):
        from base_station.protocol import parse_config_applied
        ev = parse_event(bytes([CanEvent.ConfigApplied, 1, 1]))  # only 2 bytes extra
        assert parse_config_applied(ev) is None

    def test_parse_event_context_ignores_config_applied(self):
        """Regression guard, same class of bug as PresenceCalResult: a
        ConfigApplied payload must never be misdecoded as a pellet count."""
        from base_station.protocol import parse_event_context
        extra = bytes([2, 1]) + (1092616192).to_bytes(4, "little")  # factor=10.0
        ev = parse_event(bytes([CanEvent.ConfigApplied]) + extra)
        assert parse_event_context(ev) is None
