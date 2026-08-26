"""
Tests for base-station pellet accounting.

The ledger exists so a run's pellet numbers start at 1 regardless of how long
the node has been powered, and so a dropped CAN frame is detected rather than
silently lost. Both properties are exercised here.
"""

from base_station.pellet_ledger import PelletLedger
from base_station.protocol import CanEvent


def test_session_starts_at_one_however_long_the_node_has_been_up() -> None:
    led = PelletLedger()
    rec = led.witness_event(1, CanEvent.Loaded, 4711)
    assert rec.total == 1
    assert led.presented(1) == 1


def test_reset_returns_every_node_to_zero() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 10)
    led.witness_event(2, CanEvent.Loaded, 99)
    led.reset()
    assert led.presented(1) == 0
    assert led.presented(2) == 0
    # A fresh run re-baselines against wherever the node counter now sits.
    assert led.witness_event(1, CanEvent.Loaded, 11).total == 1


def test_nodes_are_counted_independently() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 100)
    led.witness_event(1, CanEvent.Loaded, 101)
    led.witness_event(2, CanEvent.Loaded, 7)
    assert led.presented(1) == 2
    assert led.presented(2) == 1


def test_non_incrementing_events_do_not_advance_the_count() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 20)
    for ev in (CanEvent.Raising, CanEvent.OnPlate, CanEvent.DomeOpened):
        led.witness_event(1, ev, 20)
    assert led.presented(1) == 1


def test_dropped_loaded_frame_is_recovered_by_the_next_frame() -> None:
    """The Loaded for pellet 2 never arrives; DomeOpened carries the counter."""
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 20)          # pellet 1, seen
    rec = led.witness_event(1, CanEvent.DomeOpened, 21)  # pellet 2's Loaded lost
    assert rec.missed == 1
    assert rec.had_gap
    assert led.presented(1) == 2


def test_heartbeat_recovers_a_run_of_lost_frames() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 5)
    recs = led.witness_heartbeat(1, presented=9, taken=0)
    assert recs["presented"].missed == 4
    assert led.presented(1) == 5


def test_heartbeat_with_nothing_new_changes_nothing() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 5)
    rec = led.witness_heartbeat(1, presented=5, taken=0)["presented"]
    assert rec.missed == 0
    assert rec.delta == 0
    assert led.presented(1) == 1


def test_taken_uses_its_own_counter() -> None:
    """PelletTaken carries takenCount_, every other event carries pelletCount_."""
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 300)
    led.witness_event(1, CanEvent.PelletTaken, 12)
    assert led.presented(1) == 1
    assert led.taken(1) == 1


def test_node_reboot_rebaselines_instead_of_inventing_pellets() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 500)
    rec = led.witness_event(1, CanEvent.Loaded, 1)  # node power-cycled
    assert rec.restarted
    assert rec.total == 2                            # one more pellet, not 65037
    assert led.tally(1).restarts == 1


def test_double_witnessing_the_same_frame_is_idempotent() -> None:
    """
    The GUI log path and the experiment normalizer share one ledger and both
    witness every frame, in whichever order dispatch runs them.
    """
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 8)
    led.witness_event(1, CanEvent.Loaded, 8)
    assert led.presented(1) == 1


def test_counter_wrap_is_not_read_as_a_reboot() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 65535)
    rec = led.witness_event(1, CanEvent.Loaded, 0)
    assert not rec.restarted
    assert rec.missed == 0
    assert led.presented(1) == 2


def test_tally_reports_gaps_and_totals_together() -> None:
    led = PelletLedger()
    led.witness_event(1, CanEvent.Loaded, 1)
    led.witness_event(1, CanEvent.DomeOpened, 3)  # counts 2 and 3 both lost
    t = led.tally(1)
    assert (t.presented, t.missed_presented) == (3, 2)
    assert led.node_ids() == [1]
