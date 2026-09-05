#!/usr/bin/env python3
"""Regression tests for orchpiano_finisher.py - the pure-logic edge cases
found while building each phase, so a future change can't silently
reintroduce them. Mirrors the ecosystem's own OrchPianoReductionLogicCheck.cpp
pattern (plain asserts, no test framework dependency) rather than pytest,
to keep this a zero-dependency script like the tool itself.

Run: python test_finisher.py
"""

from orchpiano_finisher import (
    RawNote,
    _guard_hand_playability,
    _fix_voice_stem_order,
    _group_by_line_and_onset,
    compute_dynamics_marks,
)

failures = 0


def check(condition: bool, label: str) -> None:
    global failures
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        failures += 1


def test_hand_playability_new_note_owns_extreme():
    # Phase 2: the exact edge case that crashed the first draft - the
    # brand-new note itself owns the pitch extreme pushing the span over,
    # so it can't be its own truncation victim.
    notes = [
        RawNote(channel_offset=0, pitch=60, velocity=80, start_tick=0, end_tick=1000),
        RawNote(channel_offset=0, pitch=64, velocity=80, start_tick=0, end_tick=1000),
        RawNote(channel_offset=0, pitch=70, velocity=80, start_tick=0, end_tick=1000),
        RawNote(channel_offset=0, pitch=78, velocity=80, start_tick=100, end_tick=1000),
    ]
    _guard_hand_playability(notes, max_span=14, max_notes=8)
    active_at_150 = [n for n in notes if n.start_tick <= 150 < n.end_tick]
    pitches = [n.pitch for n in active_at_150]
    span = max(pitches) - min(pitches) if len(pitches) > 1 else 0
    check(span <= 14, "hand playability: new-note-owns-extreme case doesn't crash and respects span")


def test_voice_stem_swap_resolves_single_vs_single():
    # A high single note peeled off as voice 2 (down-stem) against a lower
    # chord left on voice 1 (up-stem) - the exact bug the user reported.
    notes = [
        RawNote(channel_offset=0, pitch=55, velocity=80, start_tick=0, end_tick=480),
        RawNote(channel_offset=0, pitch=58, velocity=80, start_tick=0, end_tick=480),
        RawNote(channel_offset=1, pitch=72, velocity=80, start_tick=0, end_tick=480),
    ]
    grouped = _group_by_line_and_onset(notes)
    _fix_voice_stem_order(grouped)
    voice1_pitches = [n.pitch for g in grouped[("RH", 1)] for n in g]
    voice2_pitches = [n.pitch for g in grouped[("RH", 2)] for n in g]
    check(voice1_pitches == [72] and voice2_pitches == [55, 58],
          "voice stem swap: higher single note ends up on voice 1 (up-stem)")


def test_dynamics_hysteresis_ignores_brief_blip():
    # A genuine multi-beat-held level change must be marked; a brief
    # sub-beat blip in the middle of a held level must NOT produce its own
    # spurious mark - found empirically: a single-onset confirmation was
    # not enough (real data showed 11 flips across 12 beats before this).
    tpb = 960
    notes = []
    for beat in range(4):
        notes.append(RawNote(channel_offset=0, pitch=60, velocity=50,
                              start_tick=beat * tpb, end_tick=beat * tpb + tpb))
    blip_start = 4 * tpb
    notes.append(RawNote(channel_offset=0, pitch=60, velocity=100,
                          start_tick=blip_start, end_tick=blip_start + tpb // 4))
    notes.append(RawNote(channel_offset=0, pitch=60, velocity=100,
                          start_tick=blip_start + tpb // 4, end_tick=blip_start + tpb // 2))
    for beat in range(5, 10):
        notes.append(RawNote(channel_offset=0, pitch=60, velocity=50,
                              start_tick=beat * tpb, end_tick=beat * tpb + tpb))
    for beat in range(10, 14):
        notes.append(RawNote(channel_offset=0, pitch=60, velocity=100,
                              start_tick=beat * tpb, end_tick=beat * tpb + tpb))

    marks = compute_dynamics_marks(notes, tpb, min_hold_beats=1.0)
    check(marks == [(0.0, "mp"), (10.0, "f")],
          f"dynamics hysteresis: brief blip ignored, genuine change marked (got {marks})")


if __name__ == "__main__":
    test_hand_playability_new_note_owns_extreme()
    test_voice_stem_swap_resolves_single_vs_single()
    test_dynamics_hysteresis_ignores_brief_blip()

    if failures == 0:
        print("\nAll tests passed.")
    else:
        print(f"\n{failures} test(s) FAILED.")
        raise SystemExit(1)
