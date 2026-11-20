# Presence drift — on-node test procedure

Firmware 1.7.1. Flash the base station from the same tree before the nodes.
Heartbeat byte 3 is still presence 0/1. The new flag bits are in byte 4, and
`PresenceCalResult` no longer carries a threshold.

Do the pad-end hardware fixes (inline resistor, tied-down wire, sealed pad,
cage ground, same 12 V supply) either before step 2 or after step 5. Not
between, or the noise and signal numbers from the two runs cannot be compared.

Turn the trace on per node from the tile button (or `SetConfig` type `0x03`).
It is not stored; a reboot turns it off. Rows land in
`{session}_presence_trace.csv`. Summarize with:

```
python -m sfm_analysis.analysis.presence_drift SESSION.csv
```

## 1. Bench, one node

USB is fine here. Serial monitor at 115200. `pres` prints the live numbers,
`trace` toggles the CAN trace, `factor 1.0` sets the percent.

- Flash 1.7.1 over a 1.7.0 node that has a stored calibration. The old NVS
  keys are deleted and the factor comes up 1.0 (`pres`).
- A finger on the pad gives one present and one clear, with no chatter.
  The edge is about 200 ms behind the touch.
- A single tap on the pad wire does not assert presence.
- Boot or `cal` with a finger held on the pad reads **clear** the whole time
  (the baseline is high, so this is not a detection). After the finger lifts,
  the baseline falls with a 5 s time constant. Most of a large offset is gone
  in about 15 s, not in one time constant.
- Move the pad cable slowly. The baseline follows and presence stays clear.
- A wet tissue on the pad emits `PresenceStuckCandidate` after 15 min, and
  only while the dome is closed. Holding the dome open the whole time emits
  nothing. The baseline does not jump when the event fires.
- Hold the dome open with the pad untouched. On release,
  `DomeBoutSummary` has `touch_seen=0`.
- With the trace enabled, frames arrive at about 1 Hz on `0x400 + nodeId`
  and stop when it is turned off.
- A broadcast `cal` does not drop presence reporting during the 20 s window.
- The developer menu migrates a saved factor of 60 back to 1.0.

## 2. Empty-cage soak

Flash the fleet. No animals. 12 V adapter, not a laptop USB supply. Trace on.
12–24 h.

Pass: zero `MousePresence Detected` events on every node. Record N and the
hourly baseline span from the summary. This is the check for the original
bug (false presence with nobody there).

## 3. 24 h with animals

Trace on, factor 1.0, stuck guard left in shadow mode
(`kPresenceStuckActive` false). Nobody changes the factor or re-baselines.

From the summary, per node: N, S, miss rate, longest presence bout, stuck
candidates, and Pellet Taken while present. Compare percent against counts
across nodes before deciding that one factor fits the fleet.

## 4. Tune

Set the factor to about `N + 0.4 × (S − N)`, in percent. If S is under
about 3 × N, do the hardware fixes before trusting that number.

Leave the stuck guard in shadow mode unless the candidates line up with
something you can name (a wet pad, a cage service) and do not line up with
mice that are resting on the pad. Enabling it is a one-line change of
`kPresenceStuckActive` in `PresenceService.h` and a reflash.

## 5. Acceptance

A fresh 24 h with animals. No re-baseline and no factor change.

- The sensitivity warning bit stays clear.
- The miss rate is no worse than step 3.
- No experiment stalls on presence-clear because the pad stuck or drifted.
- Every stuck candidate is explainable.
- The fraction of Pellet Taken events that fall inside a presence bout is
  stable against step 3.

## 6. Hardware, separate

Pad-end fixes on the current boards, timed as above. Decide whether the
next board moves `PIN_AEI` off GPIO14 so that pin can drive a touch shield,
before the production order. Do that only if the 24 h traces show water
films or cable movement as the main noise.
