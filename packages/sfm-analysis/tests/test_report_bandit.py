"""Tests for sfm_analysis.report.analyses.bandit and sections.bandit."""


from report_fixtures import bandit_run, exp_row, write_session

from sfm_analysis.report.analyses.bandit import (
    build_trials, choice_node, reversal_curve, side_bias,
    trials_to_criterion, validity_summary, win_stay_lose_shift,
    block_curve,
)
from sfm_analysis.report.loader import load_rows
from sfm_analysis.report.session import split_runs


def _trials(tmp_path, **kw):
    rows = bandit_run(session="B", **kw)
    path = write_session(tmp_path, rows, session="B")
    loaded, _, _ = load_rows(path)
    runs = split_runs(loaded, [], path)
    return build_trials(runs[0])


class TestBuildTrials:
    def test_joins_trial_and_end_rows(self, tmp_path):
        trials = _trials(tmp_path, n_trials=5, block_size=3)
        assert len(trials) == 5
        assert all(t.rich is not None for t in trials)
        assert all(t.valid == 1 for t in trials)

    def test_first_visit_uses_the_later_arm_presented_and_ignores_earlier_presence(self, tmp_path):
        # bandit_run's fixture always has the animal visit the rich arm
        # shortly after both arms present; first_visit_node must equal rich.
        trials = _trials(tmp_path, n_trials=3, block_size=5)
        for t in trials:
            assert t.first_visit_node == t.rich

    def test_presence_before_t_presented_is_ignored(self, tmp_path):
        # Build a run where a presence event fires *before* the second
        # arm_presented row; it must not be picked as first_visit.
        rows = [
            exp_row(0, "session_start", {"experiment": "two_armed_bandit", "nodes": [1, 2], "seed": 1}),
            exp_row(10, "two_armed_bandit_start", {"nodes": [1, 2], "arm_a": 1, "arm_b": 2,
                                                     "block_size": 5, "p_high": 0.9,
                                                     "next_trial_wait": "fixed_delay", "seed": 1}),
            exp_row(1000, "trial", {"trial": 1}, trial=1),
            exp_row(1050, "bandit_trial", {"trial": 1, "block": 0, "rich": 1, "lean": 2,
                                            "fed": 1, "empty": 2, "fed_accepted": 1, "empty_accepted": 1}, trial=1),
        ]
        from report_fixtures import input_changed_row
        rows.append(input_changed_row(1060, 1, 4, True, "MousePresence Detected"))   # too early — before 2nd arm_presented
        rows.append(exp_row(1100, "arm_presented", {"node": 1, "trial": 1, "role": "fed",
                                                       "presented": "pellet", "delivered": 1}, node=1, trial=1))
        rows.append(exp_row(1105, "arm_presented", {"node": 2, "trial": 1, "role": "empty",
                                                       "presented": "empty", "delivered": 0}, node=2, trial=1))
        rows.append(input_changed_row(1200, 2, 4, True, "MousePresence Detected"))   # the real first visit
        rows.append(exp_row(1300, "bandit_trial_end", {"trial": 1, "block": 0, "rich": 1, "lean": 2,
                                                          "fed": 1, "empty": 2, "delivered_node": 1,
                                                          "baited_arms": 1, "taken_node": 1,
                                                          "outcome": "taken", "valid": 1}, trial=1))
        path = write_session(tmp_path, rows, session="C")
        loaded, _, _ = load_rows(path)
        runs = split_runs(loaded, [], path)
        trials = build_trials(runs[0])
        assert len(trials) == 1
        assert trials[0].first_visit_node == 2   # not node 1, despite the earlier presence


class TestChoiceStats:
    def test_p_choose_rich_per_block_matches_hand_computation(self, tmp_path):
        # 6 trials, block_size=3 -> blocks 0 and 1; fixture always chooses rich.
        trials = _trials(tmp_path, n_trials=6, block_size=3)
        points = block_curve(trials)
        assert len(points) == 2
        for p in points:
            assert p.k == p.n == 3
            assert p.p == 1.0

    def test_invalid_trials_excluded_from_choice_stats(self, tmp_path):
        trials = _trials(tmp_path, n_trials=4, block_size=4)
        trials[1].valid = 0
        trials[1].invalid_reason = "fed_arm_halted"
        points = block_curve(trials)
        assert points[0].n == 3   # 4 trials minus the 1 marked invalid
        vs = validity_summary(trials)
        assert vs.invalid_reasons == {"fed_arm_halted": 1}
        assert vs.n_analyzed == 3


class TestWSLS:
    def test_hand_computed_2x2(self):
        # Manually constructed 4-trial sequence. Each transition k->k+1 is
        # classified by whether trial k was itself rewarded (choice == fed):
        #   1->2: trial1 chose rich(=fed)=1, rewarded=True;  trial2 repeats
        #         choice 1 -> win-stay hit.
        #   2->3: trial2 also rewarded=True (chose fed=1);   trial3 repeats
        #         choice 1 -> a SECOND win-stay pair (trial3 itself then
        #         loses, since fed flipped to 2, but that only affects the
        #         3->4 transition, not this one).
        #   3->4: trial3 rewarded=False (chose 1, fed=2);    trial4 switches
        #         to choice 2 -> lose-shift hit.
        from sfm_analysis.report.analyses.bandit import BanditTrial
        seq = [
            BanditTrial(trial=1, rich=1, lean=2, fed=1, empty=2, valid=1, first_visit_node=1),
            BanditTrial(trial=2, rich=1, lean=2, fed=1, empty=2, valid=1, first_visit_node=1),
            BanditTrial(trial=3, rich=2, lean=1, fed=2, empty=1, valid=1, first_visit_node=1),
            BanditTrial(trial=4, rich=2, lean=1, fed=2, empty=1, valid=1, first_visit_node=2),
        ]
        result = win_stay_lose_shift(seq)
        assert (result.win_stay_k, result.win_stay_n) == (2, 2)
        assert (result.lose_shift_k, result.lose_shift_n) == (1, 1)

    def test_chain_breaks_across_invalid_trial_unless_bridged(self):
        from sfm_analysis.report.analyses.bandit import BanditTrial
        seq = [
            BanditTrial(trial=1, rich=1, lean=2, fed=1, empty=2, valid=1, first_visit_node=1),
            BanditTrial(trial=2, rich=1, lean=2, fed=1, empty=2, valid=0),   # invalid — omitted from `analyzed`
            BanditTrial(trial=3, rich=1, lean=2, fed=1, empty=2, valid=1, first_visit_node=1),
        ]
        no_bridge = win_stay_lose_shift(seq, bridge_invalid=False)
        assert no_bridge.win_stay_n == 0 and no_bridge.lose_shift_n == 0  # trial 1->3 not adjacent

        bridged = win_stay_lose_shift(seq, bridge_invalid=True)
        assert bridged.win_stay_n == 1
        assert bridged.win_stay_k == 1


class TestReversalCurve:
    def test_relative_positions_align_to_flip(self, tmp_path):
        trials = _trials(tmp_path, n_trials=12, block_size=3)  # flips at trial 4, 7, 10
        points = reversal_curve(trials, pre=2, post=2)
        rels = {p.rel for p in points}
        assert rels <= {-2, -1, 0, 1, 2}

    def test_trials_to_criterion_on_synthetic_learner(self, tmp_path):
        from sfm_analysis.report.analyses.bandit import BanditTrial
        # Flip at trial 4 (block changes 0->1 there); animal takes 3 trials
        # post-flip to learn (chooses old-rich for 2, then locks onto new-rich).
        seq = [
            BanditTrial(trial=1, block=0, rich=1, lean=2, valid=1, first_visit_node=1),
            BanditTrial(trial=2, block=0, rich=1, lean=2, valid=1, first_visit_node=1),
            BanditTrial(trial=3, block=0, rich=1, lean=2, valid=1, first_visit_node=1),
            BanditTrial(trial=4, block=1, rich=2, lean=1, valid=1, first_visit_node=1),  # still old habit
            BanditTrial(trial=5, block=1, rich=2, lean=1, valid=1, first_visit_node=1),  # still old habit
            BanditTrial(trial=6, block=1, rich=2, lean=1, valid=1, first_visit_node=2),  # learned: streak starts
            BanditTrial(trial=7, block=1, rich=2, lean=1, valid=1, first_visit_node=2),
            BanditTrial(trial=8, block=1, rich=2, lean=1, valid=1, first_visit_node=2),
        ]
        result = trials_to_criterion(seq, streak=3, max_post=6)
        assert result == [2]   # rel=2 (trial 6) is the start of the 3-in-a-row streak


class TestSideBias:
    def test_all_arm_a_choices_gives_bias_one(self):
        from sfm_analysis.report.analyses.bandit import BanditTrial
        seq = [
            BanditTrial(trial=1, rich=1, lean=2, valid=1, first_visit_node=1),
            BanditTrial(trial=2, rich=2, lean=1, valid=1, first_visit_node=1),
            BanditTrial(trial=3, rich=1, lean=2, valid=1, first_visit_node=1),
        ]
        assert side_bias(seq) == 1.0
