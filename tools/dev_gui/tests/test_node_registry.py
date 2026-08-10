"""Tests for base_station.node_registry."""

import sys
import os
import time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from base_station.node_registry import NodeRegistry, NodeState
from base_station.protocol import (
    CanEvent,
    InputId,
    DispenseState,
    ServiceStatus,
    HeartbeatPayload,
    parse_heartbeat,
)


def make_hb(state=DispenseState.Idle, presence=False, pellet=False,
            load_position=False, dome_open=False,
            fault=ServiceStatus.Ok) -> HeartbeatPayload:
    return HeartbeatPayload(
        dispense_state=state,
        mouse_presence=presence,
        pellet=pellet, load_position=load_position, dome_open=dome_open,
        fault_code=fault,
    )


class TestNodeRegistry:
    def test_init_creates_slots(self):
        reg = NodeRegistry(3)
        assert reg.num_nodes() == 3
        for i in range(1, 4):
            node = reg.get(i)
            assert node is not None
            assert node.node_id == i
            assert node.label == f"Node {i}"
            assert node.online is False

    def test_update_heartbeat_marks_online(self):
        reg = NodeRegistry(3)
        reg.update_from_heartbeat(1, make_hb())
        node = reg.get(1)
        assert node.online is True
        assert node.dispense_state == DispenseState.Idle

    def test_heartbeat_updates_state(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(
            1,
            make_hb(
                state=DispenseState.Loaded,
                presence=True,
                load_position=True,
            ),
        )
        node = reg.get(1)
        assert node.dispense_state == DispenseState.Loaded
        assert node.presence is True
        assert node.load_position is True

    def test_heartbeat_age(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        node = reg.get(1)
        age = node.heartbeat_age_s
        assert age is not None
        assert 0 <= age < 0.5

    def test_set_label(self):
        reg = NodeRegistry(2)
        reg.set_label(1, "  Feeder A  ")
        assert reg.get(1).label == "Feeder A"

    def test_set_label_empty_resets_to_default(self):
        reg = NodeRegistry(2)
        reg.set_label(2, "")
        assert reg.get(2).label == "Node 2"

    def test_register_node_sets_mac(self):
        reg = NodeRegistry(3)
        mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x01])
        reg.register_node(1, mac)
        node = reg.get(1)
        assert node.mac == mac
        assert node.discovery_state == "Enabled"
        assert node.mac_str == "AA:BB:CC:DD:EE:01"

    def test_event_updates_dispense_state(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())  # bring online
        reg.update_from_event(1, CanEvent.Loaded)
        assert reg.get(1).dispense_state == DispenseState.Loaded

    def test_phase_events_update_dispense_state(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        seq = [
            (CanEvent.Seeking, DispenseState.Seeking, "SEEKING"),
            (CanEvent.Lowering, DispenseState.Lowering, "LOWERING"),
            (CanEvent.Loading, DispenseState.Loading, "LOADING"),
            (CanEvent.OnPlate, DispenseState.Loading, "LOADING"),
            (CanEvent.Raising, DispenseState.Raising, "RAISING"),
            (CanEvent.Loaded, DispenseState.Loaded, "LOADED"),
            (CanEvent.DomeOpened, DispenseState.Loaded, "LOADED"),
            (CanEvent.PelletTaken, DispenseState.Idle, "IDLE"),
        ]
        for event, state, label in seq:
            reg.update_from_event(1, event)
            node = reg.get(1)
            assert node.dispense_state == state
            assert node.status_label == label

    def test_fault_event(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.Fault, fault_code=ServiceStatus.ActuatorTimeout)
        node = reg.get(1)
        assert node.dispense_state == DispenseState.Fault
        assert node.fault_code == ServiceStatus.ActuatorTimeout

    def test_fault_event_jam(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.Fault, fault_code=ServiceStatus.Jam)
        assert reg.get(1).fault_code == ServiceStatus.Jam

    def test_fault_event_pellet_lost(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.Fault, fault_code=ServiceStatus.PelletLost)
        node = reg.get(1)
        assert node.dispense_state == DispenseState.Fault
        assert node.fault_code == ServiceStatus.PelletLost

    def test_fault_event_feed_timeout(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.Fault, fault_code=ServiceStatus.FeedTimeout)
        assert reg.get(1).fault_code == ServiceStatus.FeedTimeout

    def test_fault_event_actuator_timeout(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.Fault, fault_code=ServiceStatus.ActuatorTimeout)
        assert reg.get(1).fault_code == ServiceStatus.ActuatorTimeout

    def test_feed_skipped_maps_to_raising(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.FeedSkipped)
        assert reg.get(1).dispense_state == DispenseState.Raising

    def test_dome_open_warning_and_clear_on_dome_sensor(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.DomeOpenWarning)
        node = reg.get(1)
        assert node.dome_open_warning is True
        assert node.dispense_state != DispenseState.Fault

        reg.update_from_input(1, InputId.Dome, True)
        assert node.dome_open_warning is True
        reg.update_from_input(1, InputId.Dome, False)
        assert node.dome_open_warning is False

    def test_clear_fault_resets_warning(self):
        reg = NodeRegistry(1)
        reg.update_from_event(1, CanEvent.Fault, fault_code=ServiceStatus.Jam)
        reg.update_from_event(1, CanEvent.DomeOpenWarning)
        reg.clear_fault(1)
        node = reg.get(1)
        assert node.fault_code == ServiceStatus.Ok
        assert node.dispense_state == DispenseState.Idle
        assert node.dome_open_warning is False

    def test_input_event_updates_without_heartbeat(self):
        reg = NodeRegistry(1)
        reg.update_from_input(1, InputId.Pellet, True)
        reg.update_from_input(1, InputId.MousePresence, True)
        node = reg.get(1)
        assert node.pellet is True
        assert node.presence is True
        assert node.online is True

        reg.update_from_input(1, InputId.Pellet, False)
        assert node.pellet is False

    def test_staleness_marks_offline(self):
        reg = NodeRegistry(1)
        reg.set_offline_timeout(0.1)  # very short for test
        reg.update_from_heartbeat(1, make_hb())
        assert reg.get(1).online is True
        time.sleep(0.15)
        newly_offline = reg.check_staleness()
        assert 1 in newly_offline
        assert reg.get(1).online is False

    def test_no_stale_if_recent_heartbeat(self):
        reg = NodeRegistry(1)
        reg.set_offline_timeout(5.0)
        reg.update_from_heartbeat(1, make_hb())
        newly_offline = reg.check_staleness()
        assert 1 not in newly_offline
        assert reg.get(1).online is True

    def test_get_unknown_node_returns_none(self):
        reg = NodeRegistry(3)
        assert reg.get(99) is None

    def test_unknown_node_heartbeat_creates_slot(self):
        reg = NodeRegistry(3)
        reg.update_from_heartbeat(10, make_hb())  # node 10, outside range
        assert reg.get(10) is not None
        assert reg.get(10).online is True

    def test_status_colors(self):
        reg = NodeRegistry(1)
        node = reg.get(1)
        # Offline
        assert node.status_color == (120, 120, 120, 255)
        # Idle (online)
        reg.update_from_heartbeat(1, make_hb(state=DispenseState.Idle))
        assert node.status_color == (60, 200, 80, 255)
        # Fault
        reg.update_from_heartbeat(1, make_hb(state=DispenseState.Fault))
        assert node.status_color == (220, 50, 50, 255)

    def test_all_nodes_order(self):
        reg = NodeRegistry(5)
        ids = [n.node_id for n in reg.all_nodes()]
        assert ids == sorted(ids)

    def test_sensor_bits_property(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb(pellet=True, dome_open=True))
        node = reg.get(1)
        assert node.sensor_bits == 0b101

    # ------------------------------------------------------------------
    # presented_empty (EMPTY status for a no-feed / mimic cycle)
    # ------------------------------------------------------------------

    def test_no_feed_presented_reads_empty_not_loaded(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        node = reg.get(1)
        assert node.dispense_state == DispenseState.Loaded  # wire state unchanged
        assert node.presented_empty is True
        assert node.status_label == "EMPTY"
        assert node.status_color == (220, 200, 50, 255)

    def test_heartbeat_does_not_clobber_empty_flag(self):
        """Regression guard: the next heartbeat must not silently flip EMPTY
        back to LOADED just because it reports dispense_state == Loaded."""
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_heartbeat(1, make_hb(state=DispenseState.Loaded, pellet=False))
        assert reg.get(1).status_label == "EMPTY"

    def test_heartbeat_with_pellet_clears_empty_flag(self):
        """The self-healing backstop: a heartbeat proving a real pellet is on
        the plate clears a stale EMPTY reading."""
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_heartbeat(1, make_hb(state=DispenseState.Loaded, pellet=True))
        assert reg.get(1).presented_empty is False
        assert reg.get(1).status_label == "LOADED"

    def test_heartbeat_off_loaded_state_clears_empty_flag(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_heartbeat(1, make_hb(state=DispenseState.Idle))
        assert reg.get(1).presented_empty is False

    def test_dome_bout_on_empty_plate_keeps_empty(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_event(1, CanEvent.DomeOpened)
        node = reg.get(1)
        assert node.presented_empty is True
        assert node.status_label == "EMPTY"

    @pytest.mark.parametrize("event", [
        CanEvent.Seeking, CanEvent.Lowering, CanEvent.Loading, CanEvent.OnPlate,
        CanEvent.Dwelling, CanEvent.Raising, CanEvent.Loaded,
        CanEvent.FeedSkipped, CanEvent.PelletTaken, CanEvent.Fault,
    ])
    def test_stale_empty_flag_is_cleared_by_next_cycle_event(self, event):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_event(1, event)
        assert reg.get(1).presented_empty is False

    def test_feed_skipped_on_mimic_arm_clears_empty_flag(self):
        """A no-feed cycle that turns out to be occupied (both arms baited)
        must never keep displaying EMPTY — that would show the exact inverse
        of the truth."""
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_event(1, CanEvent.FeedSkipped)
        reg.update_from_event(1, CanEvent.Loaded)
        node = reg.get(1)
        assert node.presented_empty is False
        assert node.status_label == "LOADED"

    def test_pellet_input_clears_empty_flag(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.update_from_input(1, InputId.Pellet, True)
        assert reg.get(1).presented_empty is False

    def test_clear_fault_clears_empty_flag(self):
        reg = NodeRegistry(1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        reg.clear_fault(1)
        assert reg.get(1).presented_empty is False

    def test_going_offline_clears_empty_flag(self):
        reg = NodeRegistry(1)
        reg.set_offline_timeout(0.1)
        reg.update_from_heartbeat(1, make_hb())
        reg.update_from_event(1, CanEvent.NoFeedPresented)
        time.sleep(0.15)
        newly_offline = reg.check_staleness()
        assert 1 in newly_offline
        assert reg.get(1).presented_empty is False
