#!/usr/bin/env python3
"""Regression tests for orchpiano_finisher.py - the pure-logic edge cases
found while building each phase, so a future change can't silently
reintroduce them. Mirrors the ecosystem's own OrchPianoReductionLogicCheck.cpp
pattern (plain asserts, no test framework dependency) rather than pytest,
to keep this a zero-dependency script like the tool itself.

Run: python test_finisher.py
"""

import os
import tempfile

import mido

from music21 import expressions

from orchpiano_finisher import (
    RawNote,
    _guard_hand_playability,
    _fix_voice_stem_order,
    _group_by_line_and_onset,
    _collapse_octave_tremolos,
    compute_dynamics_marks,
    build_score,
    write_midi,
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


def test_quantize_does_not_invent_tuplets_from_straight_32nd_notes():
    # 2026-09-05: found on a real MC piece ("slack_tide") - build_score()'s
    # p.quantize(inPlace=True, recurse=True) call used music21's own default
    # quarterLengthDivisors, (4, 3) (16th notes or 8th-note triplets). MPL is
    # explicitly restricted to plain power-of-2 rhythms (no "crazy rhythm
    # formations"), but OrchPiano's own capture can genuinely need 32nd-note
    # resolution (quarterLength 0.125) - a value NEITHER divisor reaches. Left
    # to guess note-by-note which of the two ill-fitting grids was numerically
    # closer, quantize() misread long straight 32nd-note runs as scattered
    # eighth-note triplets - confirmed directly against real Finisher output
    # (113 real <tuplet> elements in a piece with none in the source MIDI).
    # Fixed by passing quarterLengthDivisors=(8, 6) explicitly - 32nd notes or
    # 16th-note triplets, the exact floor the user has always set by hand in
    # Dorico's own Requantize dialog for MPL-driven takes. This test builds a
    # plain 32nd-note-grid RH line through the real build_score() and asserts
    # NONE of its notes carry a tuplet - the class of defect a future
    # accidental revert to bare quantize() would reintroduce silently.
    # Real (pitch, start_tick, end_tick) triples lifted directly from
    # OrchPiano_slack_tide.mid's channel 0 (RH lead), ticks 0-13080, tpb=960 -
    # NOT synthesized. A first attempt at this test used 16 hand-built,
    # perfectly uniform 32nd notes and it passed under BOTH the buggy default
    # divisors and the fix - quantize()'s look-ahead handles a uniform run
    # fine, so that fixture didn't actually exercise the bug. Confirmed this
    # real excerpt does discriminate before trusting it: re-running it through
    # the pre-fix bare `p.quantize(inPlace=True, recurse=True)` produces 7
    # spurious tupleted notes; the fix (quarterLengthDivisors=(8, 6)) produces
    # zero. Real, mixed-duration/mixed-onset performance data is what exposed
    # the per-note ping-ponging; synthetic uniform data was not enough.
    real_excerpt = [
        (72, 0, 1080), (76, 0, 1080), (70, 1080, 1440), (68, 2040, 2400),
        (67, 2880, 3360), (68, 2880, 3360), (64, 3360, 3960),
        (75, 3960, 5280), (76, 3960, 5280), (61, 5280, 5760),
        (64, 5760, 6360), (66, 6360, 6720), (61, 6720, 7800),
        (67, 7800, 8640), (78, 7800, 8760), (62, 8760, 9240),
        (69, 9240, 9720), (67, 9720, 10200), (68, 10200, 10560),
        (63, 11520, 13080),
    ]
    notes = [RawNote(channel_offset=0, pitch=p, velocity=80, start_tick=s, end_tick=e)
             for p, s, e in real_excerpt]
    grouped = _group_by_line_and_onset(notes)
    score = build_score(grouped, 960, [(0, "4/4")])
    rh_notes = list(score.parts[0].flatten().notes)
    tupleted = [n for n in rh_notes if n.duration.tuplets]
    check(not tupleted,
          f"quantize: a real MPL-restricted (no true triplets) excerpt produces no "
          f"spurious tuplets (got {len(rh_notes)} notes, {len(tupleted)} with a tuplet)")


def test_hand_playability_same_onset_victim_is_dropped_not_zeroed():
    # 2026-09-05: found by cross-checking Dorico's own MIDI Import Options
    # note total against what write_midi() had actually written - Dorico
    # reported fewer notes (488/272) than were in the file (505/279). Traced
    # to _guard_hand_playability's truncation line, `victim.end_tick =
    # min(victim.end_tick, n.start_tick)`: candidates are always notes
    # already active when n was appended, so victim.start_tick <= n.start_tick
    # by construction - EXCEPT when victim shares n's own onset (a genuine
    # same-instant chord), where victim.start_tick == n.start_tick and the
    # "shorten to end at the new attack" rule sets end_tick == start_tick,
    # a zero-length ghost note that still silently occupies a note-on/off
    # pair downstream (write_midi()) or gets masked by chord-level, not
    # per-note, duration (build_score()'s MusicXML path - why this was never
    # caught there). Fixed by dropping such a note outright instead of
    # truncating it into existence-in-name-only.
    #
    # Fixture: 5 notes attacking at the SAME instant (tick 0), pitches 60-64
    # (span 4) against max_span=3 - forces a violation caused purely by the
    # same-onset chord itself, not by any later attack, the exact shape the
    # bug needed (a genuinely LATER attack never triggers it - candidates
    # only ever start at or before the triggering note by construction).
    notes = [
        RawNote(channel_offset=0, pitch=60 + i, velocity=80, start_tick=0, end_tick=480)
        for i in range(5)
    ]
    _guard_hand_playability(notes, max_span=3, max_notes=8)
    check(len(notes) == 4 and all(n.end_tick > n.start_tick for n in notes),
          f"hand playability: a same-onset over-span note is DROPPED, not left as a "
          f"zero/negative-length ghost (got {len(notes)} notes, durations "
          f"{[n.end_tick - n.start_tick for n in notes]})")


def test_write_midi_merges_voices_and_scales_ticks():
    # 2026-09-05: added after the user's own direct assessment of real
    # Dorico output - music21's MusicXML writer produced useless cross-staff
    # stems, no real up/down-stem voice separation, and almost no logical
    # beaming for this piano-reduction shape. write_midi() bypasses music21's
    # notation model entirely and writes a plain 2-track (RH/LH) MIDI file,
    # trusting Dorico's own more mature MIDI-import engine to choose voices/
    # stems/beaming - trading away <dynamics> marks and explicit lead/
    # secondary voice tagging, a trade the user explicitly accepted.
    #
    # This test checks two things a silent regression could break: (1) voice
    # 1 and voice 2 notes on the same hand both land on that hand's single
    # MIDI channel (the whole point - Dorico re-derives voices on its own),
    # and (2) notation_scale multiplies tick positions/durations directly
    # (ticks_per_beat held fixed), the MIDI-domain equivalent of Bitwig's
    # Content Scaling - simpler than the MusicXML route since there is no
    # notated-grid model to interact badly with.
    tpb = 960
    notes = [
        RawNote(channel_offset=0, pitch=72, velocity=80, start_tick=0, end_tick=480),   # RH voice 1
        RawNote(channel_offset=1, pitch=60, velocity=80, start_tick=480, end_tick=960),  # RH voice 2
        RawNote(channel_offset=3, pitch=48, velocity=80, start_tick=0, end_tick=960),    # LH voice 1
    ]
    grouped = _group_by_line_and_onset(notes)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "out.mid")
        write_midi(grouped, tpb, path, notation_scale=2.0)
        out = mido.MidiFile(path)
        by_name = {tr.name: tr for tr in out.tracks}

        def events(track):
            t = 0
            result = []
            for m in track:
                t += m.time
                if m.type in ("note_on", "note_off"):
                    result.append((t, m.type, m.note))
            return sorted(result)

        rh = events(by_name["RH"])
        lh = events(by_name["LH"])
        # Both RH pitches (voice 1 AND voice 2) must appear on the one RH
        # track, and every tick must be exactly doubled (scale=2.0).
        rh_ok = rh == [(0, "note_on", 72), (960, "note_off", 72),
                        (960, "note_on", 60), (1920, "note_off", 60)]
        lh_ok = lh == [(0, "note_on", 48), (1920, "note_off", 48)]
        check(out.ticks_per_beat == tpb and rh_ok and lh_ok,
              f"write_midi: voices merge per hand and ticks scale exactly (got RH={rh}, LH={lh})")


def test_notation_scale_doubles_offsets_and_durations():
    # 2026-09-05: --notation-scale replicates the manual Dorico "Requantize
    # then double durations" dance via music21's augmentOrDiminish(), applied
    # AFTER quantize() rather than by pre-scaling the tick-to-quarterLength
    # conversion - an earlier pre-scale attempt looked correct in an isolated
    # simulation but produced wrong (often 3x, not 2x) durations against real
    # output, because quantize()'s adaptive look-ahead does not commute with
    # a pre-scale. This builds a real score both ways and diffs the actual
    # (offset, duration) pairs, not just the ratio.
    tpb = 960
    notes = [
        RawNote(channel_offset=0, pitch=60, velocity=80, start_tick=0, end_tick=tpb),
        RawNote(channel_offset=0, pitch=64, velocity=80, start_tick=tpb, end_tick=2 * tpb),
        RawNote(channel_offset=3, pitch=48, velocity=80, start_tick=0, end_tick=2 * tpb),
    ]
    grouped = _group_by_line_and_onset(notes)

    unscaled = build_score(grouped, tpb, [(0, "4/4")], notation_scale=1.0)
    scaled = build_score(grouped, tpb, [(0, "4/4")], notation_scale=2.0)

    def offsets_and_durations(score):
        pairs = []
        for n in score.flatten().notes:
            pairs.append((round(float(n.offset), 4), round(float(n.duration.quarterLength), 4)))
        return sorted(pairs)

    unscaled_pairs = offsets_and_durations(unscaled)
    scaled_pairs = offsets_and_durations(scaled)
    doubled_expected = sorted((round(o * 2, 4), round(d * 2, 4)) for o, d in unscaled_pairs)

    check(scaled_pairs == doubled_expected,
          f"notation scale: every offset/duration doubles exactly (unscaled={unscaled_pairs}, "
          f"scaled={scaled_pairs}, expected={doubled_expected})")


def test_collapse_octave_tremolo_run_becomes_two_notes():
    # A real shape (2026-09-06): OrchPiano's Phase 5c-2d octave-tremolo plays
    # A2 (57) alternating with A3 (69) every 16th note (240 ticks @ tpb=960)
    # on the LH lead line - 8 hits = 4 full cycles, well past the 4-hit floor.
    tpb = 960
    step = tpb // 4
    notes = []
    for i in range(8):
        pitch = 57 if i % 2 == 0 else 69
        notes.append(RawNote(channel_offset=3, pitch=pitch, velocity=100,
                             start_tick=i * step, end_tick=(i + 1) * step))
    grouped = _group_by_line_and_onset(notes)
    collapsed = _collapse_octave_tremolos(grouped, tpb)

    line = grouped[("LH", 1)]
    pitches = [g[0].pitch for g in line]
    check(collapsed == 1 and pitches == [57, 69],
          f"collapse_octave_tremolos: 8-hit run -> exactly 2 notes, low then high (got {pitches})")
    check(line[0][0].end_tick == line[1][0].start_tick == 4 * step,
          "collapse_octave_tremolos: the two notes meet exactly at the run's midpoint")
    check(line[0][0].tremolo_pair_id is not None
          and line[0][0].tremolo_pair_id == line[1][0].tremolo_pair_id,
          "collapse_octave_tremolos: both replacement notes share one tremolo_pair_id")


def test_collapse_octave_tremolo_ignores_short_or_non_octave_runs():
    tpb = 960
    step = tpb // 4
    # Only 2 hits - below the 4-hit floor, a real repeated note shouldn't be
    # mistaken for a genuine roll from this little evidence.
    short_run = [
        RawNote(channel_offset=3, pitch=57, velocity=100, start_tick=0, end_tick=step),
        RawNote(channel_offset=3, pitch=69, velocity=100, start_tick=step, end_tick=2 * step),
    ]
    grouped = _group_by_line_and_onset(short_run)
    collapsed = _collapse_octave_tremolos(grouped, tpb)
    check(collapsed == 0 and len(grouped[("LH", 1)]) == 2,
          "collapse_octave_tremolos: a 2-hit run is left alone (below the floor)")

    # A climbing run (57,69,81,...) differs by 12 at every step but never
    # actually alternates back - must NOT be swept into a tremolo collapse.
    climb = [
        RawNote(channel_offset=3, pitch=57 + 12 * i, velocity=100,
               start_tick=i * step, end_tick=(i + 1) * step)
        for i in range(5)
    ]
    grouped2 = _group_by_line_and_onset(climb)
    collapsed2 = _collapse_octave_tremolos(grouped2, tpb)
    check(collapsed2 == 0 and len(grouped2[("LH", 1)]) == 5,
          "collapse_octave_tremolos: a continuously climbing run is NOT a tremolo, left alone")


def test_collapse_octave_tremolo_gets_a_tremolo_spanner_in_the_score():
    tpb = 960
    step = tpb // 4
    notes = []
    for i in range(8):
        pitch = 57 if i % 2 == 0 else 69
        notes.append(RawNote(channel_offset=3, pitch=pitch, velocity=100,
                             start_tick=i * step, end_tick=(i + 1) * step))
    grouped = _group_by_line_and_onset(notes)
    _collapse_octave_tremolos(grouped, tpb)
    score = build_score(grouped, tpb, [(0, "4/4")])

    spanners = list(score.recurse().getElementsByClass(expressions.TremoloSpanner))
    check(len(spanners) == 1, f"tremolo spanner: exactly one TremoloSpanner in the score (got {len(spanners)})")
    if spanners:
        spanned_pitches = sorted(n.pitch.midi for n in spanners[0].getSpannedElements())
        check(spanned_pitches == [57, 69],
              f"tremolo spanner: joins the low (57) and high (69) notes (got {spanned_pitches})")
        check(spanners[0].numberOfMarks == 2, "tremolo spanner: 2 marks (16th-note tremolo)")


if __name__ == "__main__":
    test_hand_playability_new_note_owns_extreme()
    test_hand_playability_same_onset_victim_is_dropped_not_zeroed()
    test_voice_stem_swap_resolves_single_vs_single()
    test_dynamics_hysteresis_ignores_brief_blip()
    test_quantize_does_not_invent_tuplets_from_straight_32nd_notes()
    test_write_midi_merges_voices_and_scales_ticks()
    test_notation_scale_doubles_offsets_and_durations()
    test_collapse_octave_tremolo_run_becomes_two_notes()
    test_collapse_octave_tremolo_ignores_short_or_non_octave_runs()
    test_collapse_octave_tremolo_gets_a_tremolo_spanner_in_the_score()

    if failures == 0:
        print("\nAll tests passed.")
    else:
        print(f"\n{failures} test(s) FAILED.")
        raise SystemExit(1)
