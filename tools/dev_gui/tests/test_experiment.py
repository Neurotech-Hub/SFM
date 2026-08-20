"""Tests for the headless experiment engine and free-feeding template."""

from __future__ import annotations

from types import SimpleNamespace

from base_station.experiment import EventKind, Experiment, ExperimentContext, NodeEvent
from base_station.experiment.events import EventNormalizer
from base_station.experiment.templates.fixed_and_random import build as build_fixed_and_random
from base_station.experiment.templates.free_feeding import build as build_free_feeding
from base_station.experiment.templates.probability_delivery import (
    build as build_probability_delivery,
)
from base_station.protocol import (
    CanCmd,
    CanEvent,
    DispenseState,
    HeartbeatPayload,
    InputId,
    ServiceStatus,
    build_event_frame,
    build_heartbeat_frame,
)


def _msg(arb_id: int, data: bytes):
    return SimpleNamespace(arbitration_id=arb_id, data=data)


def _resolve_since(runner, mark: int, ts: float) -> None:
    """
    Satisfy the sequential sync gate + take wait for commands sent since
    ``mark``: LOADED on each Dispense, NO_FEED_PRESENTED on each
    DispenseNoFeed, then PELLET_TAKEN on each fed node (separate inject so
    the take is not missed while the script is still waiting on presentation).
    """
    fed = []
    presented = []
    for (node, cmd, _) in runner.ctx.commands_sent[mark:]:
        if cmd == CanCmd.Dispense:
            fed.append(node)
            presented.append(NodeEvent(EventKind.LOADED, node_id=node, timestamp=ts))
        elif cmd == CanCmd.DispenseNoFeed:
            presented.append(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=node, timestamp=ts))
    if presented:
        runner.inject(presented)
    if fed:
        runner.inject([
            NodeEvent(EventKind.PELLET_TAKEN, node_id=n, timestamp=ts) for n in fed
        ])


def _fire_bnc_and_resolve(runner, channel: int, ts: float) -> None:
    """
    Inject a BNC IN rising edge, then resolve whichever nodes it commanded
    (fed + mimic) so the sequential loop can arm the next BNC wait.
    """
    before = len(runner.ctx.commands_sent)
    runner.inject(NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=ts,
                            data={"channel": channel, "edge": "rising", "high": True}))
    _resolve_since(runner, before, ts + 0.001)


# ---------------------------------------------------------------------------
# EventNormalizer
# ---------------------------------------------------------------------------

def test_normalizer_loaded() -> None:
    norm = EventNormalizer()
    arb, data = build_event_frame(2, CanEvent.Loaded, b"\x05\x00")
    events = norm.frame_to_events(_msg(arb, data), now=100.0)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == EventKind.LOADED
    assert ev.node_id == 2
    # Node counter reads 5; this is the run's first pellet, so the session
    # number is 1. That decoupling is the whole point of the ledger.
    assert ev.data.get("session_pellets") == 1


def test_normalizer_dome_opened() -> None:
    norm = EventNormalizer()
    arb, data = build_event_frame(1, CanEvent.DomeOpened, b"\x03\x00\x01")
    events = norm.frame_to_events(_msg(arb, data), now=1.0)
    assert events[0].kind == EventKind.DOME_OPENED
    assert events[0].node_id == 1
    assert events[0].data.get("session_pellets") == 0
    assert events[0].data.get("pellet_present") is True


def test_normalizer_pellet_taken() -> None:
    norm = EventNormalizer()
    arb, data = build_event_frame(2, CanEvent.PelletTaken, b"\x04\x00\x01")
    events = norm.frame_to_events(_msg(arb, data), now=1.0)
    assert events[0].kind == EventKind.PELLET_TAKEN
    assert events[0].data.get("session_taken") == 1
    assert events[0].data.get("dome_open") is True


def test_normalizer_derives_dome_closed_from_dome_sensor() -> None:
    norm = EventNormalizer()
    # Dome sensor open
    arb, data = build_event_frame(
        3, CanEvent.InputChanged, bytes([InputId.Dome, 1])
    )
    opened = norm.frame_to_events(_msg(arb, data), now=10.0)
    kinds = [e.kind for e in opened]
    assert EventKind.DOME_OPENED in kinds
    assert EventKind.PG_CHANGED in kinds

    # Dome sensor clear
    arb, data = build_event_frame(
        3, CanEvent.InputChanged, bytes([InputId.Dome, 0])
    )
    closed = norm.frame_to_events(_msg(arb, data), now=11.0)
    kinds = [e.kind for e in closed]
    assert EventKind.DOME_CLOSED in kinds
    assert any(e.node_id == 3 for e in closed if e.kind == EventKind.DOME_CLOSED)


def test_normalizer_node_online_offline() -> None:
    norm = EventNormalizer(online_timeout_s=5.0)
    hb = HeartbeatPayload(
        dispense_state=DispenseState.Idle,
        mouse_presence=False,
        pellet=False,
        load_position=True,
        dome_open=False,
        fault_code=ServiceStatus.Ok,
    )
    arb, data = build_heartbeat_frame(1, hb)
    events = norm.frame_to_events(_msg(arb, data), now=0.0)
    assert any(e.kind == EventKind.NODE_ONLINE for e in events)

    stale = norm.check_staleness(now=6.0)
    assert len(stale) == 1
    assert stale[0].kind == EventKind.NODE_OFFLINE
    assert stale[0].node_id == 1


def test_normalizer_presence_changed() -> None:
    norm = EventNormalizer()
    arb, data = build_event_frame(
        1, CanEvent.InputChanged, bytes([InputId.MousePresence, 1])
    )
    events = norm.frame_to_events(_msg(arb, data), now=1.0)
    assert events[0].kind == EventKind.PRESENCE_CHANGED
    assert events[0].data["active"] is True


def test_normalizer_fault() -> None:
    norm = EventNormalizer()
    arb, data = build_event_frame(1, CanEvent.Fault, bytes([ServiceStatus.Jam]))
    events = norm.frame_to_events(_msg(arb, data), now=1.0)
    assert events[0].kind == EventKind.FAULT
    assert events[0].data["fault_code"] == ServiceStatus.Jam


def test_normalizer_set_online_timeout() -> None:
    norm = EventNormalizer(online_timeout_s=5.0)
    hb = HeartbeatPayload(
        dispense_state=DispenseState.Idle,
        mouse_presence=False,
        pellet=False,
        load_position=True,
        dome_open=False,
        fault_code=ServiceStatus.Ok,
    )
    arb, data = build_heartbeat_frame(1, hb)
    norm.frame_to_events(_msg(arb, data), now=0.0)
    assert norm.check_staleness(now=6.0)  # offline under 5s timeout
    # Bring back online, then widen timeout so 6s silence is fine.
    norm.frame_to_events(_msg(arb, data), now=10.0)
    norm.set_online_timeout(180.0)
    assert norm.check_staleness(now=16.0) == []


# ---------------------------------------------------------------------------
# ExperimentContext
# ---------------------------------------------------------------------------

def test_context_dispense_records_command() -> None:
    ctx = ExperimentContext(nodes=[1, 2])
    ctx.begin(now=0.0)
    assert ctx.dispense(1) is True
    assert ctx.commands_sent[-1] == (1, CanCmd.Dispense, b"")
    assert ctx.counter("pellets") == 0
    ctx.incr("pellets")
    assert ctx.counter("pellets") == 1


def test_context_no_feed_dispense_sends_bare_command() -> None:
    """DispenseNoFeed carries no payload: the node's raise is triggered by a
    peer's Raising event on the bus, not by a dwell time the base station
    picks. Nothing about the command can encode when the plate comes up."""
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1, feed=False) is True
    node, cmd, payload = ctx.commands_sent[-1]
    assert (node, cmd, payload) == (1, CanCmd.DispenseNoFeed, b"")


def test_dispense_vetoed_when_pellet_already_on_plate() -> None:
    """Global rule: a command that would deliver (or re-run the motion) is
    never sent while the node's own plate already has a pellet on it."""
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    ctx.observe_event(NodeEvent(
        EventKind.PG_CHANGED, node_id=1, timestamp=0.0,
        data={"gate": "pellet", "active": True},
    ))

    assert ctx.dispense(1) is False
    assert ctx.dispense(1, feed=False) is False
    assert ctx.commands_sent == []  # neither Dispense nor DispenseNoFeed was sent

    warnings = [e for e in ctx.log_entries if e.name == "dispense_skipped_pellet_present"]
    assert len(warnings) == 2
    assert warnings[0].fields.get("warning") == 1
    assert warnings[0].node_id == 1


def test_dispense_allowed_once_pellet_clears() -> None:
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    ctx.observe_event(NodeEvent(
        EventKind.PG_CHANGED, node_id=1, timestamp=0.0,
        data={"gate": "pellet", "active": True},
    ))
    assert ctx.dispense(1) is False

    ctx.observe_event(NodeEvent(
        EventKind.PG_CHANGED, node_id=1, timestamp=1.0,
        data={"gate": "pellet", "active": False},
    ))
    assert ctx.dispense(1) is True


def test_dispense_vetoed_while_cycle_in_flight() -> None:
    """
    Global rule: a second dispense to the same node is never sent while its
    previous cycle hasn't resolved yet — the plate-occupied check alone can't
    catch this window, since the pellet hasn't reached the sensor yet.
    """
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)

    assert ctx.dispense(1) is True
    assert ctx.is_dispensing(1) is True
    assert not ctx.pellet_on_plate(1)  # plate not occupied yet — distinct signal

    ctx.commands_sent.clear()
    assert ctx.dispense(1) is False  # vetoed: still in flight
    assert ctx.commands_sent == []

    warnings = [e for e in ctx.log_entries if e.name == "dispense_skipped_in_flight"]
    assert len(warnings) == 1
    assert warnings[0].fields.get("warning") == 1
    assert warnings[0].node_id == 1


def test_dispense_allowed_after_loaded_clears_in_flight() -> None:
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1) is True
    assert ctx.is_dispensing(1) is True

    ctx.observe_event(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert ctx.is_dispensing(1) is False

    ctx.observe_event(NodeEvent(  # a real pellet must be taken before re-dispensing
        EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0,
    ))
    assert ctx.dispense(1) is True


def test_dispense_no_feed_in_flight_cleared_by_no_feed_presented() -> None:
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1, feed=False) is True
    assert ctx.is_dispensing(1) is True
    assert ctx.dispense(1, feed=False) is False  # still in flight

    ctx.observe_event(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=1, timestamp=1.0))
    assert ctx.is_dispensing(1) is False
    assert ctx.dispense(1, feed=False) is True


def test_dispense_in_flight_cleared_by_feed_skipped() -> None:
    """FeedSkipped means firmware never ran a motion — no further completion
    event is coming, so the in-flight veto must not lock the node forever."""
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1) is True
    assert ctx.is_dispensing(1) is True

    ctx.observe_event(NodeEvent(EventKind.FEED_SKIPPED, node_id=1, timestamp=1.0))
    assert ctx.is_dispensing(1) is False


def test_dispense_in_flight_cleared_by_fault() -> None:
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1) is True
    assert ctx.is_dispensing(1) is True

    ctx.observe_event(NodeEvent(
        EventKind.FAULT, node_id=1, timestamp=1.0,
        data={"fault_code": ServiceStatus.Jam},
    ))
    assert ctx.is_dispensing(1) is False


def test_dispense_in_flight_cleared_by_node_offline() -> None:
    """A lost node's in-flight cycle must not stay stuck forever if it
    reconnects without ever emitting a resolving event."""
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1) is True
    assert ctx.is_dispensing(1) is True

    ctx.observe_event(NodeEvent(EventKind.NODE_OFFLINE, node_id=1, timestamp=1.0))
    assert ctx.is_dispensing(1) is False


def test_recover_node_clears_in_flight_defensively() -> None:
    """recover_node() clears is_dispensing() even without a prior FAULT event
    (e.g. a fault that predates this session) — a recovered node must never
    stay permanently veto-locked out of dispense()."""
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    assert ctx.dispense(1) is True
    assert ctx.is_dispensing(1) is True

    ctx.halt_node(1)  # halted directly, no FAULT event observed
    ctx.recover_node(1)
    assert ctx.is_dispensing(1) is False
    assert ctx.dispense(1) is True


def test_broadcast_dispense_falls_back_when_node_in_flight() -> None:
    ctx = ExperimentContext(nodes=[1, 2])
    ctx.begin(now=0.0)
    ctx.dispense(1)  # node 1 now mid-cycle
    ctx.commands_sent.clear()

    ok = ctx.broadcast_dispense()
    assert ok is False  # node 1 was vetoed
    sent_nodes = [n for (n, cmd, _payload) in ctx.commands_sent if cmd == CanCmd.Dispense]
    assert sent_nodes == [2]  # only the clear node actually got a command


def test_broadcast_dispense_true_broadcast_marks_every_node_in_flight() -> None:
    """The true single-frame broadcast path must ALSO mark every node as
    dispensing — bypassing it would defeat the in-flight veto for a
    subsequent per-node dispense() call right after a broadcast."""
    ctx = ExperimentContext(nodes=[1, 2])
    ctx.begin(now=0.0)
    assert ctx.broadcast_dispense() is True
    assert ctx.is_dispensing(1) is True
    assert ctx.is_dispensing(2) is True

    ctx.commands_sent.clear()
    assert ctx.dispense(1) is False


def test_broadcast_dispense_falls_back_to_unicast_when_one_node_occupied() -> None:
    """A true broadcast can't selectively withhold from one node, so when
    any node's plate is occupied, broadcast_dispense() must fall back to
    per-node dispense() calls (each carrying its own veto) instead of
    silently feeding the occupied node too."""
    ctx = ExperimentContext(nodes=[1, 2])
    ctx.begin(now=0.0)
    ctx.observe_event(NodeEvent(
        EventKind.PG_CHANGED, node_id=1, timestamp=0.0,
        data={"gate": "pellet", "active": True},
    ))

    ok = ctx.broadcast_dispense()
    assert ok is False  # node 1 was vetoed
    sent_nodes = [n for (n, cmd, _payload) in ctx.commands_sent if cmd == CanCmd.Dispense]
    assert sent_nodes == [2]  # only the clear node actually got a command

    partial = [e for e in ctx.log_entries if e.name == "broadcast_dispense_partial_pellet_present"]
    assert len(partial) == 1
    assert partial[0].fields.get("occupied") == [1]


def test_broadcast_dispense_true_broadcast_when_all_clear() -> None:
    ctx = ExperimentContext(nodes=[1, 2])
    ctx.begin(now=0.0)
    assert ctx.broadcast_dispense() is True
    assert ctx.commands_sent[-1] == (0, CanCmd.Dispense, b"")


def test_context_after_timer_fires() -> None:
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    fired = []
    ctx.after(2.0, lambda: fired.append(True))
    ctx.tick_timers(1.0)
    assert fired == []
    ctx.tick_timers(2.0)
    assert fired == [True]
    # One-shot: should not fire again
    ctx.tick_timers(4.0)
    assert fired == [True]


def test_context_every_timer_repeats() -> None:
    ctx = ExperimentContext(nodes=[1])
    ctx.begin(now=0.0)
    count = []
    ctx.every(1.0, lambda: count.append(1))
    ctx.tick_timers(1.0)
    ctx.tick_timers(2.0)
    ctx.tick_timers(2.5)
    assert len(count) == 2


# ---------------------------------------------------------------------------
# Experiment / Runner
# ---------------------------------------------------------------------------

def test_runner_start_end_callbacks() -> None:
    exp = Experiment(nodes=[1], name="t")
    started = []
    ended = []

    @exp.on_start
    def _s(ctx):
        started.append(ctx.nodes)

    @exp.on_end
    def _e(ctx):
        ended.append(True)

    exp.end_after(seconds=5)
    runner = exp.make_runner()
    runner.start(now=0.0)
    assert started == [[1]]
    assert runner.is_active

    runner.step(now=4.0)
    assert not runner.is_finished
    runner.step(now=5.0)
    assert runner.is_finished
    assert ended == [True]


def test_runner_end_after_pellets() -> None:
    exp = Experiment(nodes=[1])
    exp.end_after(pellets=2)
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert not runner.is_finished
    assert runner.ctx.counter("pellets") == 1

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=2.0))
    assert runner.is_finished
    assert runner.ctx.counter("pellets") == 2


def test_runner_event_handler() -> None:
    exp = Experiment(nodes=[1])
    seen = []

    @exp.on_pellet_taken
    def _a(ctx, ev):
        seen.append(ev.node_id)

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=7, timestamp=1.0))
    assert seen == [7]


def test_start_when_defers_activation() -> None:
    exp = Experiment(nodes=[1])
    ready = {"ok": False}
    started = []

    exp.start_when(lambda ctx: ready["ok"])

    @exp.on_start
    def _s(ctx):
        started.append(True)

    runner = exp.make_runner()
    runner.start(now=0.0)
    assert not runner.is_active
    assert started == []

    runner.step(now=1.0)
    assert not runner.is_active

    ready["ok"] = True
    runner.step(now=2.0)
    assert runner.is_active
    assert started == [True]


def test_on_session_start_fires_once_immediately() -> None:
    exp = Experiment(nodes=[1])
    fired = []
    runner = exp.make_runner()
    runner.ctx.on_session_start = lambda: fired.append(True)

    runner.start(now=0.0)
    assert runner.is_active
    assert fired == [True]

    runner.step(now=1.0)
    runner.step(now=2.0)
    assert fired == [True]  # still exactly once


def test_on_session_start_fires_once_at_deferred_activation() -> None:
    exp = Experiment(nodes=[1])
    ready = {"ok": False}
    fired = []
    exp.start_when(lambda ctx: ready["ok"])

    runner = exp.make_runner()
    runner.ctx.on_session_start = lambda: fired.append(True)
    runner.start(now=0.0)
    assert not runner.is_active
    assert fired == []  # not yet — start_when hasn't been satisfied

    runner.step(now=1.0)
    assert fired == []

    ready["ok"] = True
    runner.step(now=2.0)
    assert runner.is_active
    assert fired == [True]

    runner.step(now=3.0)
    assert fired == [True]  # still exactly once


def test_on_session_start_error_is_caught_and_logged() -> None:
    """A raising callback must not prevent on_start handlers from running."""
    exp = Experiment(nodes=[1])
    started = []

    @exp.on_start
    def _s(ctx):
        started.append(True)

    runner = exp.make_runner()

    def _boom():
        raise RuntimeError("camera offline")

    runner.ctx.on_session_start = _boom
    runner.start(now=0.0)

    assert runner.is_active
    assert started == [True]
    errors = [e for e in runner.ctx.log_entries if e.name == "callback_error"]
    assert len(errors) == 1
    assert "camera offline" in errors[0].fields["error"]


# ---------------------------------------------------------------------------
# Free-feeding template
# ---------------------------------------------------------------------------

def test_free_feeding_dispenses_all_nodes_on_start() -> None:
    exp = build_free_feeding(nodes=[1, 2, 3], reload_delay_s=2.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)

    dispenses = [
        (n, cmd) for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == [(1, CanCmd.Dispense), (2, CanCmd.Dispense), (3, CanCmd.Dispense)]


def test_free_feeding_reloads_after_pellet_taken() -> None:
    exp = build_free_feeding(nodes=[1], reload_delay_s=2.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)
    # The on_start dispense is still in flight until it resolves — complete
    # it (a real pellet reaches the plate) before continuing.
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=0.5))
    runner.ctx.commands_sent.clear()

    runner.inject(NodeEvent(EventKind.DOME_OPENED, node_id=1, timestamp=1.0,
                            data={"pellet_present": True}))
    assert runner.ctx.counter("dome_openings") == 1
    # No reload yet — pellet not taken.
    assert runner.ctx.commands_sent == []

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0,
                            data={"dome_open": True}))
    assert runner.ctx.counter("pellets_taken") == 1
    # Delay not elapsed.
    assert runner.ctx.commands_sent == []

    runner.step(now=4.0)  # 2s after take
    dispenses = [
        (n, cmd) for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == [(1, CanCmd.Dispense)]


def test_free_feeding_presence_clear_gates_reload() -> None:
    exp = build_free_feeding(
        nodes=[1], next_trial_wait="presence_clear", iti_quiet_s=0.0, seconds=60,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=0.5))
    runner.inject(NodeEvent(
        EventKind.PRESENCE_CHANGED, node_id=1, timestamp=0.6,
        data={"active": True, "source": "event"},
    ))
    runner.ctx.commands_sent.clear()

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0,
                            data={"dome_open": True}))
    assert runner.ctx.commands_sent == []  # still on the pad

    runner.inject(NodeEvent(
        EventKind.PRESENCE_CHANGED, node_id=1, timestamp=3.0,
        data={"active": False, "source": "event"},
    ))
    runner.step(now=3.1)  # after_advance polls on a 0.1s timer
    dispenses = [
        (n, cmd) for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == [(1, CanCmd.Dispense)]


def test_free_feeding_immediate_reload_when_delay_zero() -> None:
    exp = build_free_feeding(nodes=[2], next_trial_wait="fixed_delay", reload_delay_s=0.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)
    # Complete the on_start dispense before the reload cycle.
    runner.inject(NodeEvent(EventKind.LOADED, node_id=2, timestamp=0.5))
    runner.ctx.commands_sent.clear()

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=2, timestamp=1.0))
    dispenses = [
        (n, cmd) for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == [(2, CanCmd.Dispense)]


def test_fault_pauses_whole_free_feeding_session_until_recovered() -> None:
    """A fault on any node pauses reloads on every node until Recover."""
    exp = build_free_feeding(nodes=[1, 2], reload_delay_s=2.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)
    # Complete both on_start dispenses before the reload/fault sequence.
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=0.5))
    runner.inject(NodeEvent(EventKind.LOADED, node_id=2, timestamp=0.5))
    runner.ctx.commands_sent.clear()

    # Schedule a pending reload on node 2, then fault on node 1.
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=2, timestamp=1.0))
    assert not runner.is_finished

    runner.inject(
        NodeEvent(
            EventKind.FAULT,
            node_id=1,
            timestamp=2.0,
            data={"fault_code": ServiceStatus.FeedTimeout},
        )
    )

    assert not runner.is_finished
    assert not runner.ctx.stop_requested
    assert runner.ctx.is_halted(1)
    assert not runner.ctx.is_halted(2)

    # Past node 2's reload delay — session is paused, so node 2 must not reload.
    runner.ctx.commands_sent.clear()
    runner.step(now=3.5)
    dispenses = [
        n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == []

    runner.recover_node(1, now=4.0)
    runner.step(now=4.2)
    dispenses = [
        n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert 2 in dispenses


def test_fault_cancels_faulted_nodes_pending_reload() -> None:
    """The faulted node's own pending reload timer is cancelled on fault."""
    exp = build_free_feeding(nodes=[1], reload_delay_s=2.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)

    # Dome close schedules a reload at t=3.0, then a fault halts node 1.
    runner.inject(NodeEvent(EventKind.DOME_CLOSED, node_id=1, timestamp=1.0))
    runner.inject(
        NodeEvent(
            EventKind.FAULT,
            node_id=1,
            timestamp=2.0,
            data={"fault_code": ServiceStatus.Jam},
        )
    )
    runner.ctx.commands_sent.clear()

    runner.step(now=4.0)  # past the (now-cancelled) reload time
    dispenses = [
        (n, cmd) for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == []
    assert not runner.is_finished


def test_free_feeding_fault_no_reload_after_take() -> None:
    """PelletTaken after a fault must not restart dispensing while halted."""
    exp = build_free_feeding(nodes=[1], reload_delay_s=0.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(
        NodeEvent(
            EventKind.FAULT,
            node_id=1,
            timestamp=1.0,
            data={"fault_code": ServiceStatus.Jam},
        )
    )
    assert runner.ctx.is_halted(1)
    assert not runner.is_finished
    runner.ctx.commands_sent.clear()

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0))
    runner.step(now=3.0)
    dispenses = [
        (n, cmd) for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense
    ]
    assert dispenses == []
    assert runner.ctx.is_halted(1)


def test_recover_node_rearms_faulted_node() -> None:
    """recover_node clears the fault (Recover) and re-dispenses via on_recover."""
    exp = build_free_feeding(nodes=[1], reload_delay_s=0.0, seconds=60)
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(
        NodeEvent(
            EventKind.FAULT,
            node_id=1,
            timestamp=1.0,
            data={"fault_code": ServiceStatus.Jam},
        )
    )
    assert runner.ctx.is_halted(1)
    # While halted, dispense is a no-op.
    assert runner.ctx.dispense(1) is False

    runner.ctx.commands_sent.clear()
    runner.recover_node(1, now=2.0)

    assert not runner.ctx.is_halted(1)
    cmds = [(n, cmd) for (n, cmd, _) in runner.ctx.commands_sent]
    # Recover clears the firmware fault, then on_recover re-dispenses the node.
    assert (1, CanCmd.Recover) in cmds
    assert (1, CanCmd.Dispense) in cmds
    assert cmds.index((1, CanCmd.Recover)) < cmds.index((1, CanCmd.Dispense))


def test_fixed_and_random_roles_fixed_random_off() -> None:
    """node_roles dict: fixed dispenses every cycle, off never, random via prob."""
    exp = build_fixed_and_random(
        nodes=[1, 2, 3],
        node_roles={1: "fixed", 2: "off", 3: "random"},
        trigger="timer", interval_s=5.0, random_prob=0.0, seconds=60, seed=1,
    )
    runner = exp.make_runner()
    mark = 0
    runner.start(now=0.0)   # immediate first cycle
    _resolve_since(runner, mark, 0.0)
    mark = len(runner.ctx.commands_sent)
    runner.step(now=5.0)    # second cycle
    _resolve_since(runner, mark, 5.0)
    mark = len(runner.ctx.commands_sent)
    runner.step(now=10.0)   # third cycle

    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses.count(1) == 3          # fixed → every cycle
    assert 2 not in dispenses               # off → never
    assert 3 not in dispenses               # random with prob 0 → never (mimics instead)


def test_fixed_and_random_multiple_fixed_roles() -> None:
    """Multiple fixed nodes all dispense every cycle; off excluded."""
    exp = build_fixed_and_random(
        nodes=[1, 2, 3],
        node_roles={1: "fixed", 2: "off", 3: "fixed"},
        trigger="timer", interval_s=5.0, random_prob=0.0, seconds=60, seed=1,
    )
    runner = exp.make_runner()
    mark = 0
    runner.start(now=0.0)
    _resolve_since(runner, mark, 0.0)
    mark = len(runner.ctx.commands_sent)
    runner.step(now=5.0)
    _resolve_since(runner, mark, 5.0)
    mark = len(runner.ctx.commands_sent)
    runner.step(now=10.0)

    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses.count(1) == 3
    assert dispenses.count(3) == 3
    assert 2 not in dispenses


def test_fixed_and_random_fixed_nodes_string_backcompat() -> None:
    """Legacy headless form: fixed_nodes string → those fixed, others random."""
    exp = build_fixed_and_random(
        nodes=[1, 2, 3], fixed_nodes="1", trigger="timer",
        interval_s=5.0, random_prob=0.0, seconds=60, seed=1,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    dispenses = {n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense}
    assert dispenses == {1}  # node 1 fixed; 2 & 3 random at prob 0 → mimic, not feed


def test_fixed_and_random_at_most_one_random_feeds() -> None:
    """prob=1: exactly one Random node feeds (alongside every Fixed); the other mimics."""
    exp = build_fixed_and_random(
        nodes=[1, 2, 3],
        node_roles={1: "fixed", 2: "random", 3: "random"},
        trigger="timer", interval_s=5.0, random_prob=1.0, seconds=60, seed=1,
        mimic=True,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    fed = {n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense}
    mimics = {n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.DispenseNoFeed}
    assert 1 in fed
    random_fed = fed - {1}
    assert len(random_fed) == 1
    assert random_fed <= {2, 3}
    assert mimics == {2, 3} - random_fed


def test_fixed_and_random_random_prob_zero_all_random_mimic() -> None:
    """random_prob=0: every Random node mimics; none of them feed."""
    exp = build_fixed_and_random(
        nodes=[1, 2, 3],
        node_roles={1: "fixed", 2: "random", 3: "random"},
        trigger="timer", interval_s=5.0, random_prob=0.0, seconds=60, seed=1,
        mimic=True,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    fed = {n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense}
    mimics = {n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.DispenseNoFeed}
    assert fed == {1}
    assert mimics == {2, 3}


def test_probability_delivery_weighted_pick() -> None:
    """Weight only on node 2 → every cycle delivers on node 2."""
    exp = build_probability_delivery(
        nodes=[1, 2, 3], probabilities="0,100,0", trigger="timer",
        interval_s=5.0, seconds=60, seed=7,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=5.0)

    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses and set(dispenses) == {2}


def test_probability_delivery_presence_clear_gates_next_cycle() -> None:
    exp = build_probability_delivery(
        nodes=[1, 2], probabilities="100,0",
        next_trial_wait="presence_clear", iti_quiet_s=0.0, seconds=60, seed=1,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    assert [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense] == [1]

    runner.inject(NodeEvent(EventKind.ON_PLATE, node_id=1, timestamp=0.5))
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    runner.step(now=10.0)
    assert len([n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]) == 1

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=11.0))
    assert len([n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]) == 2


def test_probability_delivery_accepts_weight_dict() -> None:
    """GUI form: probabilities as a {node_id: pct} dict routes correctly."""
    exp = build_probability_delivery(
        nodes=[1, 2, 3], probabilities={1: 0, 2: 0, 3: 100}, trigger="timer",
        interval_s=5.0, seconds=60, seed=7,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=5.0)
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses and set(dispenses) == {3}


def test_probability_delivery_zero_weight_node_never_picked() -> None:
    """A 0% node must never be picked, even over many independent draws."""
    exp = build_probability_delivery(
        nodes=[1, 2], probabilities="0,100", trigger="bnc", seed=11,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    for i in range(200):
        _fire_bnc_and_resolve(runner, 0, float(i + 1))
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert len(dispenses) == 200
    assert set(dispenses) == {2}


def test_probability_delivery_is_weighted_random_not_uniform() -> None:
    """
    20% / 80% weights must produce a random draw each cycle (not deterministic
    round-robin, not uniform 50/50) that converges to the configured split
    over many independent trials.
    """
    exp = build_probability_delivery(
        nodes=[1, 2], probabilities="20,80", trigger="bnc", seed=42,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    n_trials = 2000
    for i in range(n_trials):
        _fire_bnc_and_resolve(runner, 0, float(i + 1))
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert len(dispenses) == n_trials

    # Not deterministic: both nodes must appear (a fixed 20/80 split without
    # randomness, or a round-robin, would still pass a naive count check —
    # what we're guarding against is a uniform 50/50 draw or one node winning
    # every single trial).
    counts = {1: dispenses.count(1), 2: dispenses.count(2)}
    assert counts[1] > 0 and counts[2] > 0

    # Converges to the weighted split, not a uniform 50/50 split.
    frac_node2 = counts[2] / n_trials
    assert 0.75 <= frac_node2 <= 0.85, f"node2 fraction {frac_node2} not near 0.80"

    # Consecutive picks are not a fixed pattern (proves per-cycle randomness).
    assert len(set(dispenses[:20])) == 2


def test_probability_delivery_exactly_one_node_per_cycle() -> None:
    """
    Mutual exclusivity: each trigger dispenses on exactly ONE node — never two
    (or zero) nodes at once — even with several eligible nodes at equal weight.
    """
    exp = build_probability_delivery(
        nodes=[1, 2, 3, 4], probabilities="25,25,25,25", trigger="bnc",
        bnc_channel=0, seed=5,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)

    used = set()
    for i in range(500):
        runner.ctx.commands_sent.clear()
        ts = float(i + 1)
        runner.inject(
            NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=ts,
                      data={"channel": 0, "edge": "rising", "high": True})
        )
        dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
        assert len(dispenses) == 1, f"cycle {i}: expected exactly 1 dispense, got {dispenses}"
        used.add(dispenses[0])
        _resolve_since(runner, 0, ts + 0.001)

    # Over 500 equal-weight cycles every node should have been picked at least
    # once (rules out an accidental single-node lock), but never two per cycle.
    assert used == {1, 2, 3, 4}


def test_dispense_veto_pellet_on_plate_is_template_independent() -> None:
    """
    The 'ignore dispense while a pellet is still on the plate' guard lives in
    ExperimentControl.dispense() — so it applies to EVERY template, not just
    two_armed_bandit. Demonstrated here on probability_delivery.
    """
    exp = build_probability_delivery(
        nodes=[1, 2], probabilities="0,100", trigger="bnc", bnc_channel=0, seed=1,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)

    # A pellet is sitting on node 2's plate (sensor mirror updated from events).
    runner.inject(NodeEvent(EventKind.ON_PLATE, node_id=2, timestamp=1.0))
    assert runner.ctx.pellet_on_plate(2)

    # A cycle that targets node 2 must NOT send a dispense (plate occupied).
    runner.ctx.commands_sent.clear()
    runner.inject(NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=2.0,
                            data={"channel": 0, "edge": "rising", "high": True}))
    assert [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense] == []

    # Once the pellet is taken the plate clears and dispensing resumes.
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=2, timestamp=3.0))
    assert not runner.ctx.pellet_on_plate(2)
    runner.ctx.commands_sent.clear()
    runner.inject(NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=4.0,
                            data={"channel": 0, "edge": "rising", "high": True}))
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses == [2]


def test_probability_delivery_sequential_cycle_ignores_bnc_until_take() -> None:
    """
    Probability delivery is a sequential loop: a second BNC while the cycle is
    still waiting for presentation / take is ignored (the script is not
    listening for BNC). After the take, the next rising edge starts a new cycle.
    """
    exp = build_probability_delivery(
        nodes=[1, 2], probabilities="0,100", trigger="bnc", bnc_channel=0, seed=1,
        mimic=False,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=1.0,
                            data={"channel": 0, "edge": "rising", "high": True}))
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses == [2]

    runner.ctx.commands_sent.clear()
    runner.inject(NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=1.5,
                            data={"channel": 0, "edge": "rising", "high": True}))
    assert [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense] == []

    runner.inject(NodeEvent(EventKind.LOADED, node_id=2, timestamp=2.0))
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=2, timestamp=2.5))
    runner.ctx.commands_sent.clear()
    runner.inject(NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=3.0,
                            data={"channel": 0, "edge": "rising", "high": True}))
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses == [2]


def test_fixed_and_random_sequential_cycle_waits_for_take_then_iti() -> None:
    """Fixed+random does not fire a second cycle until the take and ITI complete."""
    exp = build_fixed_and_random(
        nodes=[1], node_roles={1: "fixed"}, trigger="timer", interval_s=1.0, seconds=60,
        mimic=False,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)  # first cycle dispenses immediately

    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses == [1]

    runner.ctx.commands_sent.clear()
    runner.step(now=1.0)
    assert [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense] == []

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.5))
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.6))
    runner.ctx.commands_sent.clear()
    runner.step(now=2.7)
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses == [1]


def test_probability_delivery_bnc_channel_zero_based() -> None:
    """BNC trigger on channel 0; a channel-1 edge is ignored; falling ignored."""
    exp = build_probability_delivery(
        nodes=[1, 2, 3], probabilities="0,0,100", trigger="bnc",
        bnc_channel=0, seconds=60, seed=3,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    # No timer cycles under bnc trigger — nothing dispensed on start.
    assert not [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense]

    # Edge on the wrong channel (1) does nothing.
    runner.inject(
        NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=1.0,
                  data={"channel": 1, "edge": "rising", "high": True})
    )
    assert not [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense]

    # Rising edge on channel 0 delivers.
    runner.inject(
        NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=2.0,
                  data={"channel": 0, "edge": "rising", "high": True})
    )
    dispenses = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert dispenses == [3]

    # Falling edge on channel 0 must not trigger a delivery.
    runner.ctx.commands_sent.clear()
    runner.inject(
        NodeEvent(EventKind.BNC_IN, node_id=0, timestamp=3.0,
                  data={"channel": 0, "edge": "falling", "high": False})
    )
    assert not [c for c in runner.ctx.commands_sent if c[1] == CanCmd.Dispense]


def test_probability_delivery_mimic_on_no_feed_other_active_nodes() -> None:
    """mimic on: one Dispense, DispenseNoFeed on every other weight>0 node; weight 0 silent."""
    exp = build_probability_delivery(
        nodes=[1, 2, 3], probabilities="50,50,0", trigger="timer",
        interval_s=5.0, seconds=60, seed=1, mimic=True,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    fed = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    mimics = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.DispenseNoFeed]
    assert len(fed) == 1
    assert len(mimics) == 1
    assert 3 not in fed and 3 not in mimics
    assert set(fed + mimics) == {1, 2}


def test_probability_delivery_mimic_off_only_fed_command() -> None:
    """mimic off: only the picked node is commanded."""
    exp = build_probability_delivery(
        nodes=[1, 2, 3], probabilities="50,50,0", trigger="timer",
        interval_s=5.0, seconds=60, seed=1, mimic=False,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    fed = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    mimics = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.DispenseNoFeed]
    assert len(fed) == 1
    assert fed[0] in (1, 2)
    assert mimics == []
    assert 3 not in fed


def test_probability_delivery_fault_on_mimic_pauses_session() -> None:
    """A fault on a mimic node pauses the whole session until Recover."""
    exp = build_probability_delivery(
        nodes=[1, 2, 3], probabilities="50,50,0", trigger="timer",
        interval_s=0.1, seconds=60, seed=1, mimic=True,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    mimics = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.DispenseNoFeed]
    assert mimics
    mimic_node = mimics[0]
    before = len(runner.ctx.commands_sent)

    runner.inject(NodeEvent(
        EventKind.FAULT, node_id=mimic_node, timestamp=1.0,
        data={"fault_code": ServiceStatus.Jam},
    ))
    paused = [e for e in runner.ctx.log_entries if e.name == "paused_for_fault"]
    assert paused
    runner.step(now=30.0)
    # Paused means no new DISPENSING. Recover frames may still go out: a mimic
    # holds at the drop position until a peer raises, so an arm left waiting on
    # the faulted node has to be cleared or it rejects the next trial.
    dispenses_after = [
        c for c in runner.ctx.commands_sent[before:]
        if c[1] in (CanCmd.Dispense, CanCmd.DispenseNoFeed)
    ]
    assert dispenses_after == []
    # The faulted node itself is never auto-recovered — that is the operator's
    # call, and clearing it here would silently un-pause the session.
    assert (mimic_node, CanCmd.Recover, b"") not in runner.ctx.commands_sent

    runner.recover_node(mimic_node, now=31.0)
    assert [e for e in runner.ctx.log_entries if e.name == "resumed_after_fault"]
    assert len([c for c in runner.ctx.commands_sent if c[1] in (CanCmd.Dispense, CanCmd.DispenseNoFeed)]) > before


def test_fixed_and_random_fault_on_mimic_pauses_session() -> None:
    """A fault on a mimicking Random node pauses Fixed nodes too."""
    exp = build_fixed_and_random(
        nodes=[1, 2, 3],
        node_roles={1: "fixed", 2: "random", 3: "random"},
        trigger="timer", interval_s=0.1, random_prob=0.0, seconds=60, seed=1,
        mimic=True,
    )
    runner = exp.make_runner()
    runner.start(now=0.0)
    mimics = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.DispenseNoFeed]
    assert set(mimics) == {2, 3}
    before = len(runner.ctx.commands_sent)

    runner.inject(NodeEvent(
        EventKind.FAULT, node_id=2, timestamp=1.0,
        data={"fault_code": ServiceStatus.Jam},
    ))
    assert [e for e in runner.ctx.log_entries if e.name == "paused_for_fault"]
    runner.step(now=30.0)
    # See the probability_delivery twin above: no new dispensing, but node 3 —
    # a healthy mimic still waiting for a peer that faulted — does get a Recover.
    dispenses_after = [
        c for c in runner.ctx.commands_sent[before:]
        if c[1] in (CanCmd.Dispense, CanCmd.DispenseNoFeed)
    ]
    assert dispenses_after == []
    assert (2, CanCmd.Recover, b"") not in runner.ctx.commands_sent
    assert (3, CanCmd.Recover, b"") in runner.ctx.commands_sent

    runner.recover_node(2, now=31.0)
    assert [e for e in runner.ctx.log_entries if e.name == "resumed_after_fault"]
    fed = [n for (n, cmd, _) in runner.ctx.commands_sent if cmd == CanCmd.Dispense]
    assert fed.count(1) >= 2


def test_free_feeding_ends_on_pellet_cap() -> None:
    exp = build_free_feeding(nodes=[1], reload_delay_s=2.0, max_pellets=2)
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert not runner.is_finished
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=2.0))
    assert runner.is_finished
    assert runner.ctx.counter("pellets") == 2


def test_pellet_cap_sums_across_all_nodes() -> None:
    """max_pellets compares an aggregate of LOADED from every node."""
    exp = build_free_feeding(nodes=[1, 2, 3], reload_delay_s=2.0, max_pellets=3)
    runner = exp.make_runner()
    runner.start(now=0.0)

    # One pellet from each of three different nodes → total 3 → cap reached.
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert not runner.is_finished
    runner.inject(NodeEvent(EventKind.LOADED, node_id=2, timestamp=2.0))
    assert not runner.is_finished
    runner.inject(NodeEvent(EventKind.LOADED, node_id=3, timestamp=3.0))
    assert runner.is_finished
    assert runner.ctx.counter("pellets") == 3


# ---------------------------------------------------------------------------
# GUI hosting: ExperimentController + step(messages=...) + on_log
# ---------------------------------------------------------------------------

def test_runner_step_with_messages_no_can_poll() -> None:
    """GUI hosting passes drained frames; runner must not require can.poll_rx()."""
    exp = build_free_feeding(nodes=[1], reload_delay_s=0.0, max_pellets=1)
    runner = exp.make_runner(wire_bnc=False)
    runner.start(now=0.0)
    runner.ctx.commands_sent.clear()

    arb, data = build_event_frame(1, CanEvent.Loaded, b"\x01\x00")
    msg = _msg(arb, data)
    runner.step(now=1.0, messages=[msg])
    assert runner.ctx.counter("pellets") == 1
    assert runner.is_finished


def test_wire_bnc_false_skips_io_callbacks() -> None:
    class FakeIO:
        def __init__(self):
            self.in1 = []
            self.in2 = []

        def on_bnc_in1_edge(self, cb):
            self.in1.append(cb)

        def on_bnc_in2_edge(self, cb):
            self.in2.append(cb)

    io = FakeIO()
    exp = build_free_feeding(nodes=[1], seconds=1)
    runner = exp.make_runner(io=io, wire_bnc=False)
    assert io.in1 == []
    assert io.in2 == []
    # Default wire_bnc=True would register callbacks:
    runner2 = exp.make_runner(io=FakeIO(), wire_bnc=True)
    assert len(runner2.io._bnc_in1_cb if hasattr(runner2.io, "_bnc_in1_cb") else []) >= 0
    # Use a fresh FakeIO to assert registration happened.
    io2 = FakeIO()
    exp.make_runner(io=io2, wire_bnc=True)
    assert len(io2.in1) == 1
    assert len(io2.in2) == 1


def test_controller_step_and_on_log() -> None:
    from base_station.experiment.gui_controller import ExperimentController
    from base_station.experiment.schema import load_experiment_def, DEFAULT_EXPERIMENTS_DIR
    from base_station.log_manager import LogManager

    ff = load_experiment_def(DEFAULT_EXPERIMENTS_DIR / "free_feeding.json")
    log = LogManager(auto_save=False)
    ctrl = ExperimentController()
    assert ctrl.start(
        ff,
        params={"fixed_delay_s": 0, "minutes": 0, "max_pellets": 1},
        nodes=[1],
        log=log,
    )
    assert ctrl.is_running

    arb, data = build_event_frame(1, CanEvent.Loaded, b"\x01\x00")
    ctrl.step(messages=[_msg(arb, data)], now=1.0)
    assert not ctrl.is_running  # finished via pellet cap

    exp_rows = [e for e in log.all_entries() if e.frame_type == "EXPERIMENT"]
    assert any(e.event_name == "session_start" for e in exp_rows)
    assert any(e.event_name == "free_feeding_start" for e in exp_rows)
    # Loaded is a CAN EVENT row only — free_feeding no longer mirrors it.
    assert not any(e.event_name == "loaded" for e in exp_rows)


def test_experiment_commands_appear_under_command_filter() -> None:
    """
    Dispense/Recover commands issued by a running experiment must show up as
    frame_type='COMMAND' rows (same as a manually-clicked button), so filtering
    the event log by 'COMMAND' shows every command regardless of origin — not
    hidden away under a generic 'EXPERIMENT' bucket.
    """
    from base_station.experiment.gui_controller import ExperimentController
    from base_station.experiment.schema import load_experiment_def, DEFAULT_EXPERIMENTS_DIR
    from base_station.log_manager import LogManager

    ff = load_experiment_def(DEFAULT_EXPERIMENTS_DIR / "free_feeding.json")
    log = LogManager(auto_save=False)
    ctrl = ExperimentController()
    assert ctrl.start(
        ff,
        params={"fixed_delay_s": 0, "minutes": 0, "max_pellets": 0},
        nodes=[3],
        log=log,
    )
    assert ctrl.is_running
    ctrl.step(messages=[], now=0.0)  # let on_start's initial dispense flush through

    command_rows = [e for e in log.all_entries() if e.frame_type == "COMMAND"]
    assert len(command_rows) == 1
    row = command_rows[0]
    assert row.event_name == "Dispense"
    assert row.node_id == 3
    assert row.raw_id == 0x103
    assert row.raw_data == bytes([CanCmd.Dispense.value])
    assert row.direction == "TX"

    # It must NOT also appear as a generic EXPERIMENT row (no duplication).
    exp_rows = [e for e in log.all_entries() if e.frame_type == "EXPERIMENT"]
    assert not any(e.event_name == "command" for e in exp_rows)
