"""Tests for the sequential @exp.script scheduler (script.py)."""

from __future__ import annotations

from base_station.experiment import EventKind, Experiment, NodeEvent


def test_script_returning_ends_session() -> None:
    exp = Experiment(nodes=[1])
    ran = []

    @exp.script
    def run(ctx):
        ran.append("start")
        yield ctx.wait(1.0)
        ran.append("end")

    runner = exp.make_runner()
    runner.start(now=0.0)
    assert ran == ["start"]
    assert not runner.is_finished

    runner.step(now=2.0)
    assert ran == ["start", "end"]
    assert runner.is_finished
    assert runner.ctx.stop_reason == "script_finished"


def test_script_wait_for_satisfied_by_inject() -> None:
    exp = Experiment(nodes=[1])
    results = []

    @exp.script
    def run(ctx):
        r = yield ctx.wait_for("pellet_taken", node=1, timeout=30.0)
        results.append(r.ok)
        results.append(r.event.node_id)

    runner = exp.make_runner()
    runner.start(now=0.0)
    assert results == []

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.0))
    assert results == [True, 1]
    assert runner.is_finished  # script returned after logging


def test_script_wait_for_timeout() -> None:
    exp = Experiment(nodes=[1])
    results = []

    @exp.script
    def run(ctx):
        r = yield ctx.wait_for("pellet_taken", node=1, timeout=5.0)
        results.append(r.timed_out)
        results.append(r.ok)

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=6.0)
    assert results == [True, False]

    timeouts = [e for e in runner.ctx.log_entries if e.name == "script_timeout"]
    assert len(timeouts) == 1


def test_script_wait_until_polled_on_ticks_with_no_events() -> None:
    exp = Experiment(nodes=[1])
    results = []
    flag = {"ready": False}

    @exp.script
    def run(ctx):
        r = yield ctx.wait_until(lambda c: flag["ready"], timeout=10.0)
        results.append(r.ok)

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=1.0)
    assert results == []

    flag["ready"] = True
    runner.step(now=2.0)
    assert results == [True]


def test_script_fault_aborts_pending_wait_for() -> None:
    exp = Experiment(nodes=[1, 2])
    results = []

    @exp.script
    def run(ctx):
        r = yield ctx.wait_for("pellet_taken", node=1, timeout=30.0)
        results.append(r.faulted)
        results.append(r.faulted_node)
        results.append(r.ok)

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject(NodeEvent(EventKind.FAULT, node_id=1, timestamp=1.0))
    assert results == [True, 1, False]
    assert runner.ctx.is_halted(1)


def test_script_wait_for_already_halted_node_resolves_next_advance() -> None:
    """
    If a node is halted BEFORE a wait_for on it is even armed (so
    on_node_halted never fires for this specific await), the deferred
    is_halted() re-check on the following advance() rescues it instead of
    hanging forever.
    """
    exp = Experiment(nodes=[1])
    results = []

    @exp.script
    def run(ctx):
        ctx.dispense(1)  # no-op while halted, logged
        r = yield ctx.wait_for("pellet_taken", node=1, timeout=30.0)
        results.append(r.faulted)

    runner = exp.make_runner()
    runner.ctx.halt_node(1)  # halted before the script even starts
    runner.start(now=0.0)
    assert results == []  # not caught on the same advance() that armed it

    runner.step(now=1.0)
    assert results == [True]


def test_script_exception_ends_session_and_logs_user_frame() -> None:
    exp = Experiment(nodes=[1])

    @exp.script
    def run(ctx):
        yield ctx.wait(0.0)
        raise ValueError("boom")

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=0.1)

    assert runner.is_finished
    assert runner.ctx.stop_reason == "script_error"
    errors = [e for e in runner.ctx.log_entries if e.name == "script_error"]
    assert len(errors) == 1
    assert errors[0].fields["error"] == "boom"
    assert errors[0].fields["error_type"] == "ValueError"
    assert "test_script.py" in errors[0].fields["at"]


def test_script_advance_cap_bounds_a_runaway_loop() -> None:
    exp = Experiment(nodes=[1])
    counter = {"n": 0}

    @exp.script
    def run(ctx):
        while True:
            counter["n"] += 1
            yield ctx.wait_until(lambda c: True)

    runner = exp.make_runner()
    runner.start(now=0.0)
    assert counter["n"] == 64  # capped, not truly infinite
    assert not runner.is_finished  # session keeps running; script resumes next tick

    cap_logs = [e for e in runner.ctx.log_entries if e.name == "script_advance_cap"]
    assert len(cap_logs) == 1

    runner.step(now=1.0)
    assert counter["n"] == 128
    cap_logs = [e for e in runner.ctx.log_entries if e.name == "script_advance_cap"]
    assert len(cap_logs) == 1  # logged once, not every tick


def test_on_handlers_run_before_script_advances_in_same_batch() -> None:
    exp = Experiment(nodes=[1])
    order = []

    @exp.on_pellet_taken
    def _handler(ctx, ev):
        order.append("handler")

    @exp.script
    def run(ctx):
        yield ctx.wait_for("pellet_taken", node=1)
        order.append("script")

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=1.0))
    assert order == ["handler", "script"]


def test_no_feed_presented_does_not_increment_pellets_counter() -> None:
    exp = Experiment(nodes=[1])
    exp.end_after(pellets=1)
    runner = exp.make_runner()
    runner.start(now=0.0)

    runner.inject(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=1, timestamp=1.0))
    assert runner.ctx.counter("pellets") == 0
    assert not runner.is_finished

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=2.0))
    assert runner.ctx.counter("pellets") == 1
    assert runner.is_finished


def test_quiet_for_ignores_heartbeats_and_own_phase_events() -> None:
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    assert not ctx.quiet_for(5.0, node=1)  # just seeded at begin()

    runner.inject(
        NodeEvent(
            EventKind.HEARTBEAT,
            node_id=1,
            timestamp=6.0,
            data={"pellet": False, "load_position": False, "dome_open": False, "mouse_presence": False},
        )
    )
    assert ctx.quiet_for(5.0, node=1)  # heartbeat is not activity

    runner.inject(NodeEvent(EventKind.LOWERING, node_id=1, timestamp=6.5))
    assert ctx.quiet_for(4.5, node=1)  # our own dispense-phase event is not activity either

    runner.inject(NodeEvent(EventKind.DOME_OPENED, node_id=1, timestamp=7.0))
    assert not ctx.quiet_for(5.0, node=1)  # real animal activity resets the clock


def test_domes_closed_reflects_pg3_state() -> None:
    exp = Experiment(nodes=[1, 2])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    assert ctx.domes_closed()
    runner.inject(NodeEvent(EventKind.DOME_OPENED, node_id=1, timestamp=1.0))
    assert not ctx.domes_closed()
    assert not ctx.domes_closed([1])
    assert ctx.domes_closed([2])

    runner.inject(NodeEvent(EventKind.DOME_CLOSED, node_id=1, timestamp=2.0))
    assert ctx.domes_closed()


def test_next_trial_increments_and_sets_counter() -> None:
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    assert ctx.trial == 0
    assert ctx.next_trial() == 1
    assert ctx.next_trial() == 2
    assert ctx.trial == 2
    assert ctx.counter("trials") == 2


# ---------------------------------------------------------------------------
# Presentation state: presented_pellet / presented_empty / presentation /
# presentation_done / clear_presentation
# ---------------------------------------------------------------------------

def test_loaded_sets_presented_pellet() -> None:
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    assert ctx.presentation(1) == "none"
    assert not ctx.presentation_done(1)

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert ctx.presented_pellet(1) is True
    assert ctx.presented_empty(1) is False
    assert ctx.presentation(1) == "pellet"
    assert ctx.presentation_done(1) is True


def test_no_feed_presented_sets_presented_empty() -> None:
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    runner.inject(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=1, timestamp=1.0))
    assert ctx.presented_pellet(1) is False
    assert ctx.presented_empty(1) is True
    assert ctx.presentation(1) == "empty"
    assert ctx.presentation_done(1) is True


def test_on_plate_does_not_count_as_presented() -> None:
    """ON_PLATE fires mid-Loading, before the raise — must not be mistaken
    for a completed presentation."""
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    runner.inject(NodeEvent(EventKind.ON_PLATE, node_id=1, timestamp=1.0))
    assert ctx.presentation(1) == "none"
    assert ctx.presentation_done(1) is False


def test_dome_opened_does_not_clear_presentation() -> None:
    """A dome bout on an already-presented plate (pellet or empty) must not
    erase what was actually presented — that's the behavioral measure."""
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    runner.inject(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=1, timestamp=1.0))
    runner.inject(NodeEvent(EventKind.DOME_OPENED, node_id=1, timestamp=2.0))
    assert ctx.presented_empty(1) is True


def test_feed_skipped_clears_stale_presentation() -> None:
    """A no-feed cycle that turns out to be occupied (both arms baited) ends
    in a real Loaded — the stale 'empty' reading from FeedSkipped's own
    starting point must not survive into that new Loaded."""
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    runner.inject(NodeEvent(EventKind.NO_FEED_PRESENTED, node_id=1, timestamp=1.0))
    runner.inject(NodeEvent(EventKind.FEED_SKIPPED, node_id=1, timestamp=2.0))
    assert ctx.presentation(1) == "none"
    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=3.0))
    assert ctx.presentation(1) == "pellet"


def test_new_cycle_events_clear_presentation() -> None:
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    for clearing_kind in (
        EventKind.SEEKING, EventKind.LOWERING, EventKind.LOADING,
        EventKind.DWELLING, EventKind.RAISING, EventKind.FAULT,
    ):
        runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
        assert ctx.presentation(1) == "pellet"
        runner.inject(NodeEvent(clearing_kind, node_id=1, timestamp=2.0))
        assert ctx.presentation(1) == "none", f"{clearing_kind} did not clear"


def test_pellet_taken_does_not_clear_presentation() -> None:
    """Deliberately NOT in the auto-clear list: an animal can take a
    fast-presenting arm's pellet before a slower sibling arm finishes
    raising, and that must not un-resolve presentation_done() for the first
    arm. Templates that need a fresh reading each trial call
    clear_presentation() explicitly."""
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert ctx.presentation_done(1) is True

    runner.inject(NodeEvent(EventKind.PELLET_TAKEN, node_id=1, timestamp=2.0))
    assert ctx.presentation(1) == "pellet"
    assert ctx.presentation_done(1) is True


def test_clear_presentation_resets_at_trial_start() -> None:
    exp = Experiment(nodes=[1])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    runner.inject(NodeEvent(EventKind.LOADED, node_id=1, timestamp=1.0))
    assert ctx.presentation(1) == "pellet"
    ctx.clear_presentation(1)
    assert ctx.presentation(1) == "none"
    assert not ctx.presentation_done(1)


# ---------------------------------------------------------------------------
# Presence: presence() / presence_clear()
# ---------------------------------------------------------------------------

def test_presence_and_presence_clear() -> None:
    exp = Experiment(nodes=[1, 2])
    runner = exp.make_runner()
    runner.start(now=0.0)
    ctx = runner.ctx

    assert ctx.presence_clear()  # trivially true before any reading — documented caveat
    runner.inject(NodeEvent(EventKind.PRESENCE_CHANGED, node_id=1, timestamp=1.0,
                             data={"active": True, "source": "event"}))
    assert ctx.presence(1) is True
    assert not ctx.presence_clear()
    assert not ctx.presence_clear([1])
    assert ctx.presence_clear([2])

    runner.inject(NodeEvent(EventKind.PRESENCE_CHANGED, node_id=1, timestamp=2.0,
                             data={"active": False, "source": "event"}))
    assert ctx.presence(1) is False
    assert ctx.presence_clear()


# ---------------------------------------------------------------------------
# kit.next_trial_wait
# ---------------------------------------------------------------------------

def test_kit_next_trial_wait_fixed_delay() -> None:
    from base_station.experiment import kit

    exp = Experiment(nodes=[1])
    fired = []

    @exp.script
    def run(control):
        yield kit.next_trial_wait(control, "fixed_delay", delay_s=2.0)
        fired.append(True)

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=1.0)
    assert fired == []
    runner.step(now=2.5)
    assert fired == [True]


def test_kit_next_trial_wait_presence_clear() -> None:
    from base_station.experiment import kit

    exp = Experiment(nodes=[1, 2])
    fired = []

    @exp.script
    def run(control):
        yield kit.next_trial_wait(control, "presence_clear", nodes=(1, 2))
        fired.append(True)

    runner = exp.make_runner()
    # Prime presence True on node 1 BEFORE starting, so wait_until does not
    # resolve trivially at arm time (presence defaults False until the first
    # reading arrives for a node — see test_presence_and_presence_clear).
    runner.ctx.observe_event(NodeEvent(
        EventKind.PRESENCE_CHANGED, node_id=1, timestamp=0.0,
        data={"active": True, "source": "event"},
    ))
    runner.start(now=0.0)
    assert fired == []  # presence not clear yet

    runner.inject(NodeEvent(EventKind.PRESENCE_CHANGED, node_id=1, timestamp=2.0,
                             data={"active": False, "source": "event"}))
    assert fired == [True]


def test_kit_next_trial_wait_unknown_mode_falls_back_to_fixed_delay() -> None:
    from base_station.experiment import kit

    exp = Experiment(nodes=[1])
    fired = []

    @exp.script
    def run(control):
        yield kit.next_trial_wait(control, "bogus_mode", delay_s=1.0)
        fired.append(True)

    runner = exp.make_runner()
    runner.start(now=0.0)
    runner.step(now=1.5)
    assert fired == [True]
