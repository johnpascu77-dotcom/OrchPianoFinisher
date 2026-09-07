#!/usr/bin/env python3
"""OrchPiano Finisher - Phase 1 (merge) + Phase 2 (playability safety net).

Merges OrchPiano's 4-channel Reduce output (captured via OrchCapture) onto a
2-staff grand-staff MusicXML score, using OrchPiano's own channel->role
mapping as ground truth instead of re-deriving hand/voice structure the way
Dorico's built-in Write > Reduce does.

Channel offsets from OrchPiano's chanBase (see OrchPianoProcessor.cpp
~line 459 and this repo's own Docs/OrchPiano_ReductionRules.md SS12):
    +0  RH lead        -> RH staff, voice 1
    +1  RH secondary   -> RH staff, voice 2
    +2  LH secondary   -> LH staff, voice 2
    +3  LH lead/bass   -> LH staff, voice 1

Scope (see Docs/OrchPianoFinisher_Design.md): this tool packages OrchPiano's
own decision into notation. It does NOT re-decide melody/bass/hand
assignment or note content.

Phase 3 (dynamics, this file): derives <dynamics> marks from note velocity,
not the CC11 that rides through OrchPiano's Reduce mode untouched from
whichever of up to ~25 merged orchestral tracks happened to still be
sounding (see design doc SS3) - not a coherent "how loud is the reduction"
signal. Velocity IS something OrchPiano actually controls (dynamicRecoveryScale
boosts a thinned chord to preserve perceived loudness), so it already
reflects the reduction's own judgment; compute_dynamics_marks buckets it into
pp/p/mp/mf/f/ff with simple hysteresis (a bucket only commits once the next
onset confirms it) so a single outlier chord doesn't produce a spurious mark.

Notation scale (this file, 2026-09-05): MPL's own step grid is finer than what
notates cleanly for some pieces (e.g. "Slack Tide"), needing every note value
doubled to read as 16ths instead of 32nds - previously a manual two-step
Dorico dance (Requantize to a 32nd/16th-triplet floor, then Write > Edit
Duration > Double Durations on everything). `--notation-scale 2` replicates
that exactly via music21's own `Stream.augmentOrDiminish()`, applied AFTER
quantize() (not before): an earlier attempt scaled the EFFECTIVE ticks-per-
beat fed into the tick-to-quarterLength conversion instead, reasoning that
snapping to a grid commutes with a uniform rescale (verified in isolation -
20,000 random-tick trial, zero mismatches) - but that isolated proof modeled
quantize() as an independent per-value snap, when music21's real quantize()
has adaptive look-ahead logic across neighboring notes that does NOT commute
with a pre-scale the same way. Caught by diffing real output, not trusting
the isolated proof: many durations came out 3x instead of 2x on an actual
capture. augmentOrDiminish() sidesteps this entirely by scaling values that
are ALREADY on a clean grid (multiplying an exact eighth-note or triplet
value by 2 stays exactly on a - coarser - exact grid), so there is no
adaptive-quantize interaction to go wrong. Confirmed empirically (not just
by reading its docstring) that it correctly recurses into nested Voice
streams AND scales top-level Dynamic marks. Does not touch
_guard_hand_playability/_guard_staggered_overlaps/_fix_voice_stem_order,
which all work in raw ticks and stay scale-invariant either way.

Phase 2 (this file, see design doc's 2026-09-05 scope-gap finding): OrchPiano's own
per-hand span/voice-count check inside reduceHand() only ever evaluates one
onset group's own snapshot - it has no way to see that a still-sustaining
note from an earlier onset group, recombined with a brand-new attack, can
push what's REALLY sounding in one hand past its own limits even though each
attack was individually fine. _guard_hand_playability walks real note timing
(not the onset-group abstraction) per hand and enforces span/count directly;
_report_hand_crossing flags (never auto-fixes) real hand-crossing, since
deciding whether a crossing passage is a genuine musical gesture needs a
human ear. _guard_staggered_overlaps remains a narrower, separate hygiene
pass within one voice line. Same-onset chords on the lead voice (voice 1 is
NOT monophonic - it carries whatever streamHandVoices() didn't peel off as
the secondary voice) are notated as real chords, never mistaken for overlaps.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

import mido
from music21 import chord, clef, dynamics, expressions, instrument, layout, meter, note, stream

# Roughly equal 1-127 splits into the 6 dynamics the design doc settled on
# (SS3) - not a claim that real dynamics are evenly spaced, just a simple,
# defensible default until real-take testing shows otherwise.
DYNAMIC_BUCKETS = [(21, "pp"), (42, "p"), (63, "mp"), (84, "mf"), (105, "f"), (127, "ff")]

CHANNEL_ROLE = {
    0: ("RH", 1),
    1: ("RH", 2),
    2: ("LH", 2),
    3: ("LH", 1),
}

HAND_OF = {offset: staff for offset, (staff, _voice) in CHANNEL_ROLE.items()}


@dataclass
class RawNote:
    channel_offset: int
    pitch: int
    velocity: int
    start_tick: int
    end_tick: int
    # Set only by _collapse_octave_tremolos: shared between the exactly-2
    # replacement notes of one collapsed octave-tremolo run, so build_score
    # can find the matching pair and join them with a TremoloSpanner.
    tremolo_pair_id: int | None = None


def _has_note_events(tr) -> bool:
    return any(msg.type in ("note_on", "note_off") for msg in tr)


def _pick_among(candidates: list, what: str):
    """Given tracks that all matched a name/keyword, prefer the ones that
    actually contain notes - a capture can legitimately have more than one
    track sharing a name (e.g. an empty marker/tempo track alongside the
    real content track, seen in real OrchCapture output), and picking the
    first match blindly would silently select the empty one."""
    with_notes = [tr for tr in candidates if _has_note_events(tr)]
    if len(with_notes) == 1:
        return with_notes[0]
    if len(with_notes) > 1:
        raise SystemExit(f"Found {len(with_notes)} {what} tracks that contain notes - "
                          "pass --track <index> to disambiguate")
    raise SystemExit(f"Found {what} track(s) but none contain any notes")


def find_orchpiano_track(mid: mido.MidiFile, track_arg: str | None):
    """Locate the OrchPiano track by name (default) or explicit index/name."""
    if track_arg is not None:
        if track_arg.isdigit():
            idx = int(track_arg)
            if not (0 <= idx < len(mid.tracks)):
                raise SystemExit(f"--track {idx} out of range (file has {len(mid.tracks)} tracks)")
            return mid.tracks[idx]
        matches = [tr for tr in mid.tracks
                   if next((m.name for m in tr if m.type == "track_name"), None) == track_arg]
        if not matches:
            raise SystemExit(f"No track named exactly {track_arg!r} found")
        return _pick_among(matches, f"named {track_arg!r}")

    candidates = [tr for tr in mid.tracks
                  if (name := next((m.name for m in tr if m.type == "track_name"), None))
                  and "orchpiano" in name.lower()]
    if not candidates:
        raise SystemExit("No track named 'OrchPiano' found - pass --track <index or exact name>")
    return _pick_among(candidates, "'OrchPiano'-named")


def extract_time_signatures(mid: mido.MidiFile) -> list[tuple[int, str]]:
    """Scan every track (not just the OrchPiano content track) for
    `time_signature` meta events and return (abs_tick, "num/den") pairs in
    tick order. OrchCapture (2026-09-06 fix) writes these into its own
    separate meta track - alongside track-name/tempo/markers - which can
    share the exact same track NAME as the note-data track ("Grand Piano" in
    a real capture, not distinguishable by name), so this deliberately does
    not go through find_orchpiano_track(); it just looks at the whole file.
    An empty capture (host never reported a meter, or an older OrchCapture
    build predating the fix) returns []."""
    found: list[tuple[int, str]] = []
    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == "time_signature":
                found.append((abs_tick, f"{msg.numerator}/{msg.denominator}"))
    found.sort(key=lambda pair: pair[0])
    return found


def extract_notes(track, channel_base: int | None) -> tuple[list[RawNote], int]:
    """Pair note-on/note-off events on the track into RawNotes, keyed to
    channel_base..+3. Returns (notes, resolved_channel_base)."""
    abs_tick = 0
    seen_channels = set()
    events = []  # (abs_tick, msg)
    for msg in track:
        abs_tick += msg.time
        if msg.type in ("note_on", "note_off"):
            seen_channels.add(msg.channel)
            events.append((abs_tick, msg))

    if channel_base is None:
        if not seen_channels:
            raise SystemExit("Track has no note events")
        channel_base = min(seen_channels)

    expected = {channel_base, channel_base + 1, channel_base + 2, channel_base + 3}
    if seen_channels - expected:
        extra = sorted(seen_channels - expected)
        print(
            f"WARNING: track uses channel(s) {extra} outside the expected "
            f"4-channel block {sorted(expected)} for channel-base {channel_base} "
            "(1-indexed MIDI ch " + ",".join(str(c + 1) for c in sorted(expected)) + "). "
            "Events on unexpected channels are ignored - pass --channel-base "
            "explicitly if this file used a different Out Channel Base.",
            file=sys.stderr,
        )

    open_notes: dict[tuple[int, int], list[tuple[int, int]]] = {}  # (chan,pitch) -> [(start_tick, velocity)]
    raw_notes: list[RawNote] = []

    def is_note_off(msg):
        return msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)

    for abs_tick, msg in events:
        if msg.channel not in expected:
            continue
        key = (msg.channel, msg.note)
        if msg.type == "note_on" and msg.velocity > 0:
            open_notes.setdefault(key, []).append((abs_tick, msg.velocity))
        elif is_note_off(msg):
            stack = open_notes.get(key)
            if not stack:
                continue  # note-off with no matching note-on - ignore rather than guess
            start_tick, velocity = stack.pop(0)
            raw_notes.append(
                RawNote(
                    channel_offset=msg.channel - channel_base,
                    pitch=msg.note,
                    velocity=velocity,
                    start_tick=start_tick,
                    end_tick=abs_tick,
                )
            )

    raw_notes.sort(key=lambda n: (n.start_tick, n.channel_offset, n.pitch))
    return raw_notes, channel_base


def _guard_hand_playability(notes: list[RawNote], max_span: int, max_notes: int) -> None:
    """Phase 2 safety net (see module docstring). Per hand (both voice
    channels combined - they sound on the same physical hand), sweeps real
    note timing and enforces `max_span` (semitones) and `max_notes`
    (simultaneous notes) directly against whatever is ACTUALLY sounding at
    each new attack, not against any single onset group's own view of
    itself. When a new attack would push the hand over either limit, the
    conflicting older note(s) are truncated to end at that attack - same
    "favor the newer attack, shorten the older sustain" rule already used by
    _guard_staggered_overlaps, just applied across both of a hand's voices
    together instead of within one voice line. A span violation removes
    whichever extreme (top or bottom) note shrinks the span more; a
    count-only violation removes the oldest-started note. Mutates in place.

    `max_span`/`max_notes` are the caller's assumption about what OrchPiano's
    "Max Hand Span"/"Notes / Hand (Reduce)" were set to for this take - the
    captured MIDI carries no record of the plugin's own parameter state.
    """
    by_hand: dict[str, list[RawNote]] = {"RH": [], "LH": []}
    for n in notes:
        by_hand[HAND_OF[n.channel_offset]].append(n)

    span_truncated = 0
    count_truncated = 0
    dropped = 0
    to_remove: set[int] = set()
    for hand_notes in by_hand.values():
        hand_notes.sort(key=lambda n: n.start_tick)
        active: list[RawNote] = []
        for n in hand_notes:
            active = [a for a in active if a.end_tick > n.start_tick]
            active.append(n)
            while True:
                pitches = [a.pitch for a in active]
                span = (max(pitches) - min(pitches)) if len(active) > 1 else 0
                over_span = span > max_span
                over_count = len(active) > max_notes
                if not (over_span or over_count):
                    break
                candidates = [a for a in active if a is not n]
                if not candidates:
                    break  # the new note alone already violates - nothing left to trim
                if over_span:
                    # hi/lo are extremes of the WHOLE active set (candidates
                    # + the just-added n) - one of them may be n's own
                    # pitch, which candidates can't supply as a victim, so
                    # only compare removal options that candidates can
                    # actually satisfy.
                    hi, lo = max(pitches), min(pitches)
                    hi_ok = any(a.pitch == hi for a in candidates)
                    lo_ok = any(a.pitch == lo for a in candidates)
                    if hi_ok and lo_ok:
                        without_hi = [a for a in candidates if a.pitch != hi]
                        without_lo = [a for a in candidates if a.pitch != lo]
                        def span_of(lst):
                            ps = [a.pitch for a in lst]
                            return (max(ps) - min(ps)) if len(ps) > 1 else 0
                        target = hi if span_of(without_hi) <= span_of(without_lo) else lo
                    elif hi_ok:
                        target = hi
                    else:
                        target = lo
                    victim = next(a for a in candidates if a.pitch == target)
                else:
                    victim = min(candidates, key=lambda a: a.start_tick)
                if victim.start_tick >= n.start_tick:
                    # 2026-09-05: victim shares n's own onset (a genuine
                    # same-instant chord) - candidates are only ever notes
                    # already `active` when n was appended, which by
                    # construction start at or before n, so this is only
                    # ever equality in practice (the >= is defensive). There
                    # is no legitimate "shorten to end before it starts"
                    # resolution for a same-onset sibling; truncating it to
                    # n.start_tick would make end_tick == start_tick, a
                    # zero-length ghost note that still silently occupies a
                    # note-on/off pair downstream. Found by a real cross-
                    # check: Dorico's own MIDI Import Options reported fewer
                    # notes (488/272) than write_midi() had actually written
                    # (505/279) - traced to exactly these phantom notes (44
                    # of them, both hands combined), present since this
                    # function's Phase 2 introduction and previously masked
                    # in the MusicXML path by build_score()'s onset-GROUP
                    # (not per-note) duration, which a chord's other,
                    # legitimately-timed members dominated. Drop it outright
                    # instead of truncating it into existence-in-name-only.
                    to_remove.add(id(victim))
                    dropped += 1
                else:
                    victim.end_tick = min(victim.end_tick, n.start_tick)
                    if over_span:
                        span_truncated += 1
                    else:
                        count_truncated += 1
                active.remove(victim)
    if to_remove:
        notes[:] = [n for n in notes if id(n) not in to_remove]
    if span_truncated:
        print(f"NOTE: truncated {span_truncated} note(s) to keep the real (cross-attack) "
              f"hand span <= {max_span} semitones.", file=sys.stderr)
    if count_truncated:
        print(f"NOTE: truncated {count_truncated} note(s) to keep the real (cross-attack) "
              f"hand note-count <= {max_notes}.", file=sys.stderr)
    if dropped:
        print(f"NOTE: dropped {dropped} same-onset note(s) that would otherwise have been "
              "truncated to zero/negative length (a same-instant sibling attack can't be "
              "\"shortened to end before it starts\").", file=sys.stderr)


def _report_hand_crossing(notes: list[RawNote], ticks_per_beat: int) -> None:
    """Phase 2, flag-only (see module docstring): reports total time the two
    hands' real sounding ranges cross (LH's highest note above RH's lowest)
    and the first few instances by beat position. Never truncates or drops a
    note over this - OrchPiano's own crossoverSlack already permits brief,
    legitimate crossing at the hand-split boundary, and a longer/deeper one
    found here could be a genuine musical gesture (or a real problem) -
    that call needs a human ear, not a heuristic."""
    events = []  # (tick, is_release, hand, pitch) - releases sort before attacks at the same tick
    for n in notes:
        hand = HAND_OF[n.channel_offset]
        events.append((n.start_tick, 0, hand, n.pitch))
        events.append((n.end_tick, 1, hand, n.pitch))
    events.sort(key=lambda e: (e[0], e[1]))

    active = {"RH": set(), "LH": set()}
    crossing_ticks = 0
    was_crossing = False
    prev_tick = None
    first_instances: list[int] = []

    for tick, is_release, hand, pitch in events:
        if was_crossing and prev_tick is not None and tick > prev_tick:
            crossing_ticks += tick - prev_tick
        if is_release:
            active[hand].discard(pitch)
        else:
            active[hand].add(pitch)
        is_crossing = bool(active["RH"]) and bool(active["LH"]) and min(active["RH"]) < max(active["LH"])
        if is_crossing and not was_crossing and len(first_instances) < 5:
            first_instances.append(tick)
        was_crossing = is_crossing
        prev_tick = tick

    if crossing_ticks > 0:
        beats = [round(t / ticks_per_beat, 2) for t in first_instances]
        print(f"NOTE: hands cross (a LH note sounds above RH's lowest concurrent note) for "
              f"{round(crossing_ticks / ticks_per_beat, 2)} beat(s) total across the take; "
              f"first instance(s) at beat {beats} - review in Dorico, NOT auto-corrected.",
              file=sys.stderr)


def _velocity_bucket(velocity: int) -> str:
    for threshold, label in DYNAMIC_BUCKETS:
        if velocity <= threshold:
            return label
    return DYNAMIC_BUCKETS[-1][1]


def compute_dynamics_marks(notes: list[RawNote], ticks_per_beat: int,
                           min_hold_beats: float = 1.0) -> list[tuple[float, str]]:
    """Phase 3 (see module docstring): one velocity-derived dynamics mark per
    genuine, held level change - not per chord, and not per hand (a piano
    dynamic mark applies to the whole instrument). At every distinct onset
    tick across ALL 4 channels, buckets the average velocity of whatever
    attacks at that instant into pp/p/mp/mf/f/ff.

    Hysteresis: a new bucket only commits once it holds continuously for at
    least `min_hold_beats` (or runs to the end of the piece) - checked
    empirically, not assumed: a single extra-onset confirmation was NOT
    enough. Real velocity often sits right at a bucket boundary and jitters
    across it beat-to-beat in a fast passage (observed: 11 bucket flips
    across 12 beats on real data with a 1-onset confirmation), which is
    exactly the "marking every chord" clutter this was supposed to prevent,
    just one onset later. Requiring a real time span filters that out while
    still catching genuine phrase-level dynamic shifts. A short run that
    doesn't hold long enough is skipped entirely (not merged into neighbors)
    rather than guessed at. Returns (offset_in_quarterLength, label) pairs
    at commit points only."""
    by_tick: dict[int, list[int]] = {}
    for n in notes:
        by_tick.setdefault(n.start_tick, []).append(n.velocity)

    ticks = sorted(by_tick)
    buckets = [_velocity_bucket(round(sum(by_tick[t]) / len(by_tick[t]))) for t in ticks]
    min_hold_ticks = min_hold_beats * ticks_per_beat

    marks: list[tuple[float, str]] = []
    committed = None
    i, n = 0, len(buckets)
    while i < n:
        b = buckets[i]
        if b == committed:
            i += 1
            continue
        j = i
        while j < n and buckets[j] == b:
            j += 1
        holds_long_enough = (j == n) or (ticks[j - 1] - ticks[i] >= min_hold_ticks)
        if holds_long_enough:
            marks.append((ticks[i] / ticks_per_beat, b))
            committed = b
            i = j
        else:
            i += 1
    return marks


def _group_by_line_and_onset(notes: list[RawNote]) -> dict[tuple[str, int], list[list[RawNote]]]:
    """Bucket notes into (RH/LH, voice-number) lines, then within each line
    into onset-groups: notes sharing an exact start_tick are one attack (a
    chord if >1 pitch) - this is normal on the "lead" line (voice 1), which
    carries whatever streamHandVoices() didn't peel off as the secondary
    voice, i.e. it is NOT monophonic by design. Grouping by onset BEFORE any
    overlap check is what keeps a real chord from being mistaken for one note
    overlapping another and truncated (an earlier version of this script did
    exactly that - see git history)."""
    by_line: dict[tuple[str, int], list[RawNote]] = {}
    for n in notes:
        staff, voice = CHANNEL_ROLE[n.channel_offset]
        by_line.setdefault((staff, voice), []).append(n)

    grouped: dict[tuple[str, int], list[list[RawNote]]] = {}
    for key, line_notes in by_line.items():
        line_notes.sort(key=lambda n: n.start_tick)
        groups: list[list[RawNote]] = []
        for n in line_notes:
            if groups and groups[-1][0].start_tick == n.start_tick:
                groups[-1].append(n)
            else:
                groups.append([n])
        grouped[key] = groups
    return grouped


def _fix_voice_stem_order(grouped: dict[tuple[str, int], list[list[RawNote]]]) -> None:
    """Notation-only fix - does NOT touch pitch, duration, or hand
    assignment, only which onset-group is labeled notational voice 1
    (up-stem, by convention) vs voice 2 (down-stem). OrchPiano's own
    streamHandVoices() picks the secondary voice by CONTINUITY (closest to
    the line's last pitch, or longest-held), never by register - so voice 1
    (everything it didn't peel off) very often ends up sounding LOWER than
    voice 2 at a given attack, which every notation program still renders
    up-stem/down-stem by voice number regardless of actual pitch. The result
    is exactly the visual mess of up-stem notes sitting below down-stem ones
    that prompted this fix.

    Only swaps attacks that share an EXACT onset tick between voice 1 and
    voice 2 on the same hand - the case that clashes most visibly - and only
    when doing so puts the higher-AVERAGE-pitch attack on top. This is a
    heuristic, not a full crossing-eliminator: a wide chord in one voice
    against a single note in the other can still cross after the swap (the
    chord's own notes span a range no single swap can fully resolve without
    re-splitting which pitches belong to which voice, which would be
    re-deciding OrchPiano's own content - out of scope here). Mutates
    `grouped` in place."""
    swapped = 0
    for staff in ("RH", "LH"):
        key1, key2 = (staff, 1), (staff, 2)
        if key1 not in grouped or key2 not in grouped:
            continue
        groups1, groups2 = grouped[key1], grouped[key2]
        by_tick1 = {g[0].start_tick: i for i, g in enumerate(groups1)}
        by_tick2 = {g[0].start_tick: i for i, g in enumerate(groups2)}
        for tick in sorted(set(by_tick1) & set(by_tick2)):
            i1, i2 = by_tick1[tick], by_tick2[tick]
            g1, g2 = groups1[i1], groups2[i2]
            avg1 = sum(n.pitch for n in g1) / len(g1)
            avg2 = sum(n.pitch for n in g2) / len(g2)
            if avg2 > avg1:
                groups1[i1], groups2[i2] = g2, g1
                swapped += 1
    if swapped:
        print(f"NOTE: swapped stem-direction voice assignment for {swapped} same-instant attack(s) "
              "(the down-stem voice would otherwise have sounded higher).", file=sys.stderr)


def _guard_staggered_overlaps(grouped: dict[tuple[str, int], list[list[RawNote]]]) -> None:
    """Narrow hygiene guard, NOT the Phase-2 cross-hand/cross-onset safety
    net: between two DIFFERENT attacks on the same line (never within one
    onset-group/chord - those share a start_tick by construction and are
    left alone), if the earlier attack's latest release is after the next
    attack's onset, truncate every note in the earlier group to that onset.
    Mutates in place."""
    truncated = 0
    for groups in grouped.values():
        for i in range(len(groups) - 1):
            cur_end = max(n.end_tick for n in groups[i])
            nxt_start = groups[i + 1][0].start_tick
            if cur_end > nxt_start:
                for n in groups[i]:
                    n.end_tick = min(n.end_tick, nxt_start)
                truncated += 1
    if truncated:
        print(f"NOTE: truncated {truncated} staggered same-voice overlap(s) to the next attack "
              "(distinct from same-onset chords, which are never touched here).", file=sys.stderr)


def _collapse_octave_tremolos(grouped: dict[tuple[str, int], list[list[RawNote]]],
                              ticks_per_beat: int) -> int:
    """OrchPiano's own 'RepeatedNote -> octave tremolo' feature (Phase 5c-2d,
    2026-09-06) plays a real, audible run of alternating same-pitch-class
    notes a fixed 16th note apart - correct for playback, but notating each
    strike literally beams out as a long run of individual noteheads
    (confirmed against a real Dorico render - the user's own screenshot
    showed exactly this, contrasted against a published reduction's
    measured-tremolo shorthand). This is a NOTATION-only transform: detect
    such a run and replace it with exactly two notes (the low and high
    pitch, each holding half the run's total span) joined by a music21
    TremoloSpanner - the conventional two-note measured-tremolo engraving.
    MIDI export is unaffected (see main() - this only ever runs on the
    MusicXML path, where the real alternating audio timing isn't needed,
    only its notated shorthand). Mutates `grouped` in place. Returns the
    number of runs collapsed."""
    step_ticks = max(1, ticks_per_beat // 4)   # the 16th-note rate OrchPiano emits at
    tolerance = max(2, step_ticks // 6)        # real timing isn't perfectly exact
    min_run = 4                                # at least 2 full low/high cycles
    collapsed = 0
    next_pair_id = 0

    for groups in grouped.values():
        new_groups: list[list[RawNote]] = []
        i = 0
        while i < len(groups):
            g = groups[i]
            if len(g) != 1:
                new_groups.append(g)
                i += 1
                continue

            run = [g]
            low_ref = g[0].pitch
            high_ref = None
            j = i + 1
            while j < len(groups) and len(groups[j]) == 1:
                prev, cur = run[-1][0], groups[j][0]
                if abs((cur.start_tick - prev.start_tick) - step_ticks) > tolerance:
                    break
                if high_ref is None:
                    if cur.pitch - prev.pitch != 12:
                        break
                    high_ref = cur.pitch
                elif cur.pitch != (low_ref if prev.pitch == high_ref else high_ref):
                    break
                run.append(groups[j])
                j += 1

            if len(run) >= min_run:
                start_tick = run[0][0].start_tick
                end_tick = max(n.end_tick for grp in run for n in grp)
                half = max(1, (end_tick - start_tick) // 2)
                velocity = max(n.velocity for grp in run for n in grp)
                chan = g[0].channel_offset

                low_note = RawNote(chan, low_ref, velocity, start_tick, start_tick + half,
                                   tremolo_pair_id=next_pair_id)
                high_note = RawNote(chan, high_ref, velocity, start_tick + half, end_tick,
                                    tremolo_pair_id=next_pair_id)
                next_pair_id += 1
                collapsed += 1

                new_groups.append([low_note])
                new_groups.append([high_note])
                i = j
            else:
                new_groups.append(g)
                i += 1
        groups[:] = new_groups

    if collapsed:
        print(f"NOTE: collapsed {collapsed} octave-tremolo run(s) into measured-tremolo "
              "notation (two notes + tremolo beam) - MIDI playback timing is unaffected, "
              "this only changes MusicXML notation.", file=sys.stderr)
    return collapsed


def _guard_cross_voice_pitch_overlaps(notes: list[RawNote]) -> int:
    """Truncate any same-pitch overlap regardless of which voice it came
    from. _guard_staggered_overlaps only ever checks within one (staff,
    voice) line; write_midi() merges voice 1 and voice 2 back into one flat
    polyphonic line per hand for MIDI export, which can expose a genuine
    CROSS-voice same-pitch collision neither line's own guard would ever
    see (e.g. voice 1's note still ringing when voice 2 re-attacks the same
    pitch). Two overlapping note-on events for the same pitch on one MIDI
    channel are ambiguous - a receiver (Dorico's importer included) can't
    tell which note-off belongs to which onset, and can silently swallow
    one of the two notes rather than erroring. Found on a real take
    ("slack_tide"): Dorico's own MIDI Import Options reported fewer total
    notes (488/272) than were actually written (505/279) - traced directly
    to 25 RH / 4 LH such overlaps, not a cosmetic discrepancy. Same policy
    as the rest of this file: favor the newer attack, truncate the earlier
    one. Mutates in place, returns the count fixed."""
    by_pitch: dict[int, RawNote] = {}
    truncated = 0
    for n in sorted(notes, key=lambda n: n.start_tick):
        prev = by_pitch.get(n.pitch)
        if prev is not None and prev.end_tick > n.start_tick:
            prev.end_tick = n.start_tick
            truncated += 1
        by_pitch[n.pitch] = n
    return truncated


def write_midi(grouped: dict[tuple[str, int], list[list[RawNote]]], ticks_per_beat: int,
               output_path: str, notation_scale: float = 1.0) -> None:
    """Write the corrected (post safety-net) note data as a plain 2-track
    Standard MIDI File - one track per hand (RH channel 0, LH channel 1),
    voice 1 and voice 2 merged back into one polyphonic line per hand -
    instead of a music21-built MusicXML score.

    2026-09-05: added because music21's MusicXML writer produces genuinely
    poor engraving for this piano-reduction shape once tested against a real
    take - useless cross-staff stems, no real up/down-stem voice separation,
    almost no logical beaming (the user's own direct assessment against real
    Dorico output, not a subjective guess on my part). The underlying merge
    (4 channels -> 2 hands, safety-net-corrected note timing) is still the
    valuable part; music21's own notation choices on top of it were not.
    Re-importing a plain MIDI file lets Dorico's own, more mature MIDI-import
    engine choose voices/stems/beaming itself. Trade-off the user explicitly
    accepted: no velocity-derived <dynamics> marks (MusicXML-only - raw
    velocity is still in the file, just not rendered as text) and no
    explicit lead/secondary voice tagging (merged back into one line per
    hand; Dorico re-derives voices on its own, same as it would for genuine
    performance MIDI).

    notation_scale multiplies tick positions/durations directly, ticks_per_beat
    held fixed - the MIDI-domain equivalent of Bitwig's own Content Scaling
    (50%/200%) the user originally asked about, and far simpler than the
    MusicXML route's augmentOrDiminish-after-quantize dance: there is no
    notated-grid model here for a pre/post scale order to interact badly
    with, so a plain integer multiply is exact.
    """
    out = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    for staff, channel in (("RH", 0), ("LH", 1)):
        notes: list[RawNote] = []
        for voice_num in (1, 2):
            for group in grouped.get((staff, voice_num), []):
                notes.extend(group)
        if not notes:
            # Nothing landed on this hand at all (Hands=Left/Right run) - an
            # empty staff is legitimate, not an error; just skip the track.
            continue

        cross_voice_truncated = _guard_cross_voice_pitch_overlaps(notes)
        if cross_voice_truncated:
            print(f"NOTE: truncated {cross_voice_truncated} cross-voice same-pitch overlap(s) "
                  f"on {staff} before MIDI export (voice 1/voice 2 merge can expose these; "
                  "neither voice's own overlap guard ever sees them alone).", file=sys.stderr)

        # (tick, is_note_on, pitch, velocity) - sorting is_note_on False
        # before True at an equal tick lets a note ending exactly when
        # another of the same pitch begins produce a clean off-then-on pair
        # rather than an ambiguous overlap on one MIDI channel.
        events: list[tuple[int, bool, int, int]] = []
        for n in notes:
            start = round(n.start_tick * notation_scale)
            end = round(n.end_tick * notation_scale)
            if end <= start:
                # A degenerate zero/negative-length note left behind by the
                # cross-voice guard truncating right down to its own onset -
                # nothing audible or notatable to write.
                continue
            events.append((start, True, n.pitch, n.velocity))
            events.append((end, False, n.pitch, 0))
        if not events:
            continue
        events.sort(key=lambda e: (e[0], e[1]))
        track = mido.MidiTrack()
        track.name = staff
        out.tracks.append(track)
        last_tick = 0
        for tick, is_on, pitch, velocity in events:
            delta = tick - last_tick
            last_tick = tick
            track.append(mido.Message("note_on" if is_on else "note_off",
                                       note=pitch, velocity=velocity, time=delta, channel=channel))
    out.save(output_path)


def build_score(grouped: dict[tuple[str, int], list[list[RawNote]]], ticks_per_beat: int,
                time_sigs: list[tuple[int, str]], dynamics_marks: list[tuple[float, str]] = (),
                notation_scale: float = 1.0) -> stream.Score:
    """`time_sigs` is a list of (start_tick, "num/den") pairs, tick-sorted,
    at least one entry (see main() - falls back to a single (0, "4/4") when
    the capture has no time-signature meta event and the user didn't pass
    --time-signature). Each is inserted into both staves at its own
    quarterLength offset so a real meter change notates correctly, not just
    a single signature at the start."""
    score = stream.Score()

    # PartStaff (not plain Part) + a braced StaffGroup with barTogether=True
    # makes music21's own exporter join them into a single MusicXML <part>
    # with <staves>2</staves> (confirmed by inspecting real output -
    # PartStaffExporterMixin.joinPartStaffs(), called automatically from
    # ScoreExporter.parse()) - NOT two separate <part> elements. That part
    # of "notate as one piano" already worked. What was actually still
    # missing (see _add_part_symbol_brace below) is a music21 gap, not a
    # design flaw here.
    parts = {
        "RH": stream.PartStaff(id="RH"),
        "LH": stream.PartStaff(id="LH"),
    }
    parts["RH"].partName = "Piano"
    parts["LH"].partName = "Piano"
    parts["RH"].insert(0, instrument.Piano())
    parts["LH"].insert(0, instrument.Piano())
    parts["RH"].insert(0, clef.TrebleClef())
    parts["LH"].insert(0, clef.BassClef())
    for p in parts.values():
        for tick, sig in time_sigs:
            p.insert(tick / ticks_per_beat, meter.TimeSignature(sig))

    voices: dict[tuple[str, int], stream.Voice] = {}
    # _collapse_octave_tremolos (see main()) marks its two replacement notes
    # with a shared tremolo_pair_id - collect the first one seen per id here,
    # then join it to the second with a TremoloSpanner once both exist.
    pending_tremolo_starts: dict[int, tuple[note.Note, stream.Voice]] = {}

    for key, groups in grouped.items():
        staff, voice_num = key
        voices[key] = stream.Voice(id=str(voice_num))
        for group in groups:
            offset_ql = group[0].start_tick / ticks_per_beat
            # A chord's notes can genuinely release at different times (e.g.
            # a shorter top note over a held lower one); MusicXML/a Chord
            # object can't express per-pitch durations, so Phase 1 uses the
            # longest release in the group. Individual-release fidelity
            # within a chord is a later-phase refinement, not lost data (the
            # source MIDI still has it), just not notated yet.
            duration_ql = max(max(n.end_tick for n in group) - group[0].start_tick, 1) / ticks_per_beat
            if len(group) == 1:
                nn = note.Note(midi=group[0].pitch)
                nn.volume.velocity = group[0].velocity
            else:
                nn = chord.Chord([note.Note(midi=n.pitch) for n in group])
                nn.volume.velocity = max(n.velocity for n in group)
            nn.quarterLength = duration_ql
            voices[key].insert(offset_ql, nn)

            pair_id = group[0].tremolo_pair_id
            if pair_id is not None:
                if pair_id not in pending_tremolo_starts:
                    pending_tremolo_starts[pair_id] = (nn, voices[key])
                else:
                    start_note, start_voice = pending_tremolo_starts.pop(pair_id)
                    ts = expressions.TremoloSpanner()
                    ts.addSpannedElements([start_note, nn])
                    ts.numberOfMarks = 2   # 16th-note tremolo (see _collapse_octave_tremolos)
                    # Spanners live in the stream, referencing elements that
                    # can sit in a DIFFERENT voice than where the spanner
                    # itself is inserted - insert into whichever of the two
                    # voices is this (the later) note's own, matching how
                    # music21's own examples anchor a spanner near its
                    # elements rather than at the score root.
                    start_voice.insert(0, ts)

    for (staff, voice_num), v in sorted(voices.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        parts[staff].insert(0, v)

    # Dynamics apply to the whole piano, not one hand - convention puts the
    # mark under the bass (LH) staff. Fall back to RH only if this take has
    # nothing on LH at all (a Hands=Right-only run).
    dynamics_staff = "LH" if any(isinstance(el, stream.Voice) for el in parts["LH"]) else "RH"
    for offset_ql, label in dynamics_marks:
        parts[dynamics_staff].insert(offset_ql, dynamics.Dynamic(label))

    for staff in ("RH", "LH"):
        p = parts[staff]
        if not any(isinstance(el, stream.Voice) for el in p):
            # Nothing landed on this hand at all (Hands=Left/Right run) - an
            # empty staff is legitimate, not an error.
            continue
        # Real captured performance timing isn't grid-aligned; MusicXML can
        # only express notated (non-arbitrary-fraction) durations, so snap to
        # a grid before notating. This is a display-quantization step, not a
        # musical decision OrchPiano already made - it does not touch pitch,
        # hand, or voice assignment.
        #
        # 2026-09-05: MUST pass quarterLengthDivisors=(8, 6) explicitly - do
        # NOT rely on quantize()'s own default, (4, 3) (16th notes or 8th-
        # note triplets). Found on "slack_tide": MPL's captured onsets
        # genuinely land on a 32nd-note grid (confirmed directly in the raw
        # MIDI with mido - 247 of 784 note-ons sit at exactly a 120-tick/960
        # offset, i.e. quarterLength 0.125, the exact 32nd-note position, NOT
        # a triplet - MPL is explicitly restricted to plain power-of-2
        # rhythms, no "crazy rhythm formations"). (4, 3) has no divisor that
        # reaches 0.125 at all, so quantize() was forced to snap each such
        # note independently to whichever of the 16th grid or the 8th-note-
        # triplet grid happened to be numerically closer - misreading a
        # perfectly plain run of 32nd notes as scattered eighth-note
        # triplets, one note at a time. This produced real, valid-but-absurd
        # tuplet notation (confirmed directly in the exported MusicXML) that
        # a later --notation-scale 2 pass then compounded into even more
        # unusual quarter-note-level triplets - but the doubling only ever
        # exposed a pre-existing quantize() bug, it did not cause it (the
        # SAME spurious triplets, just at the 8th-note level, are already
        # present with notation_scale=1.0). (8, 6) - 32nd notes or 16th-note
        # triplets - is exactly the manual floor the user has always had to
        # set by hand in Dorico's own Requantize dialog for MPL-driven
        # takes; matching it here removes the ambiguity that caused this.
        p.quantize(quarterLengthDivisors=(8, 6), inPlace=True, recurse=True)
        if notation_scale != 1.0:
            # AFTER quantize, never before: quantize()'s grid-choice has
            # adaptive look-ahead across neighboring notes that does not
            # commute with a pre-scale (confirmed by diffing real output,
            # not just reasoning about it - see module docstring). Applied
            # here, every value being scaled is already on a clean grid, so
            # multiplying by an exact factor stays exactly on a grid too -
            # no adaptive interaction left to go wrong. music21's own
            # augmentOrDiminish (verified empirically to recurse into nested
            # Voice streams and to scale top-level Dynamic marks correctly)
            # is the "Edit Duration" step of the manual Dorico workflow this
            # replaces.
            p.augmentOrDiminish(notation_scale, inPlace=True)
        p.makeRests(inPlace=True, fillGaps=True, hideRests=False)
        p.makeMeasures(inPlace=True)
        # makeMeasures alone does NOT split a note that runs past its
        # measure's end into tied fragments - it can leave a single
        # over-long duration sitting in the measure where the note starts.
        # Confirmed by direct inspection: without this call, a real 4.5-beat
        # note (crossing two barlines) survived intact right after
        # makeMeasures but vanished by the time the MusicXML was re-parsed -
        # found by checking the actual data at each pipeline stage, not by
        # trusting a clean run. makeTies splits it into proper tied
        # fragments, one per measure, that MusicXML can actually represent.
        p.makeTies(inPlace=True)
        # 2026-09-07: makeMeasures() (just above) does NOT preserve the
        # Voice.id we set when creating voices[key] - confirmed by direct
        # inspection (a Voice explicitly built with id="1" comes out the
        # other side of makeMeasures with id=0, an int, one fresh small
        # index per measure starting from 0 again for EVERY part). Since RH
        # and LH are each their own PartStaff processed independently right
        # here, both hands' lead voice silently ends up id=0 in every
        # measure, and both hands' secondary voice (if present) ends up
        # id=1 - real, verified collisions once joinPartStaffs() (see
        # build_score's own comment above) merges RH+LH into one <part>,
        # where MusicXML voice numbers must be unique across the WHOLE part,
        # not just within one original staff. music21's own
        # renumberVoicesWithinStaffGroups() does not catch this: it only
        # renumbers ids it can tell are auto-generated memory-location
        # artifacts (very large ints), and treats any already-small integer
        # id, even an accidental duplicate like this, as deliberate and
        # leaves it alone. Fix it ourselves: give RH's per-measure voices
        # 1/2 and LH's 3/4 (offset by 2), assigned in each measure's own
        # voice order (lead inserted before secondary into `p`, above, so
        # this reproduces the same lead/secondary order per measure).
        voice_id_base = 0 if staff == "RH" else 2
        for m in p.getElementsByClass(stream.Measure):
            for i, v in enumerate(m.getElementsByClass(stream.Voice)):
                v.id = voice_id_base + i + 1
        score.insert(0, p)

    present_parts = list(score.parts)
    if not present_parts:
        raise SystemExit("No notes ended up on either staff - check --channel-base / input file")

    if len(present_parts) == 2:
        score.insert(0, layout.StaffGroup(present_parts[0], present_parts[1],
                                          name="Piano", symbol="brace", barTogether=True))

    return score


def _add_part_symbol_brace(xml_path: str) -> None:
    """Patch a real, acknowledged gap in music21's own exporter: joining two
    PartStaffs into one multi-staff MusicXML <part> (see build_score's
    comment - confirmed via PartStaffExporterMixin.joinPartStaffs(), which
    correctly emits a single <part>/<score-part> and <staves>2</staves>)
    never emits the <part-symbol> element that formally declares those
    staves braced into one grand-staff keyboard instrument - the exporter's
    own source literally has "# TODO: part-symbol" left unimplemented right
    where <staves> is written (m21ToXml.py,
    setMxAttributesObjectForStartOfMeasure). Without it, an importer sees
    two correctly-numbered staves but no explicit brace/instrument-grouping
    signal - confirmed to be exactly what made Dorico's import "completely
    unusable" (the user's own words) even though the <part> count was
    already correct. Post-processes the written file directly (there is no
    music21-level hook for this) by inserting
    "<part-symbol>brace</part-symbol>" right after the first <staves> tag,
    per the MusicXML schema's required <attributes> child order (staves,
    then part-symbol, then instruments/clef). No-op if the file has only one
    staff (a Hands=Left/Right-only run - nothing to brace) or already has a
    <part-symbol> (a future music21 fix implementing the TODO above)."""
    with open(xml_path, "r", encoding="utf-8") as f:
        xml_text = f.read()
    if "<part-symbol>" in xml_text or "<staves>" not in xml_text:
        return
    patched, count = re.subn(
        r"(<staves>\d+</staves>)",
        r"\1<part-symbol>brace</part-symbol>",
        xml_text, count=1,
    )
    if count:
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(patched)
        print("NOTE: patched missing <part-symbol>brace</part-symbol> onto the joined "
              "grand-staff part (music21 itself never emits this - see "
              "_add_part_symbol_brace) - this is what tells an importer like Dorico "
              "the two staves are one braced piano instrument, not two independent ones.",
              file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_mid", help="Captured MIDI file (e.g. from OrchCapture)")
    ap.add_argument("output_path",
                     help="Output path. A .mid/.midi extension writes a plain 2-track "
                          "(RH/LH) Standard MIDI File instead of MusicXML - use this when "
                          "music21's own notation (stems/voices/beaming) looks worse than "
                          "letting Dorico's MIDI import decide those on its own; the "
                          "merge/safety-net corrections still apply either way. Any other "
                          "extension writes MusicXML as before.")
    ap.add_argument("--track", default=None,
                     help="Track index or exact track name to read (default: auto-detect by 'OrchPiano' in the name)")
    ap.add_argument("--channel-base", type=int, default=None,
                     help="0-indexed base channel (OrchPiano's chanBase - 1). Default: auto-detect as the lowest channel used on the track.")
    ap.add_argument("--time-signature", default=None,
                     help="Time signature for notation purposes. Default: auto-detect from the "
                          "capture's own time-signature meta event(s) (OrchCapture has embedded "
                          "these since 2026-09-06); falls back to 4/4 with a warning if the "
                          "capture has none (e.g. an older capture, or the host never reported a "
                          "meter). Passing this explicitly overrides auto-detection entirely - "
                          "and only ever produces ONE signature at the start, even if the "
                          "capture itself changed meter mid-piece.")
    ap.add_argument("--max-hand-span", type=int, default=14,
                     help="Assumed OrchPiano 'Max Hand Span (st)' for this take, in semitones (default 14, OrchPiano's own default). "
                          "Phase 2 enforces this against real overlapping note timing, not just each onset group's own snapshot.")
    ap.add_argument("--max-hand-notes", type=int, default=4,
                     help="Assumed OrchPiano 'Notes / Hand (Reduce)' for this take (default 4, OrchPiano's own default). "
                          "Enforced the same way as --max-hand-span.")
    ap.add_argument("--no-dynamics", action="store_true",
                     help="Skip velocity-derived <dynamics> marks (Phase 3). On by default.")
    ap.add_argument("--notation-scale", type=float, default=1.0,
                     help="Multiply every notated duration/position by this factor (default 1.0, "
                          "no change). Use 2.0 when the source's native grid is too fine to read "
                          "cleanly (e.g. an MPL-driven take, where a 16th-note grid otherwise "
                          "notates as 32nds) - replaces the manual Dorico Requantize-then-"
                          "Double-Durations workflow. Does not affect the safety-net guards, "
                          "which work in raw ticks regardless of this setting.")
    args = ap.parse_args()

    mid = mido.MidiFile(args.input_mid)
    track = find_orchpiano_track(mid, args.track)
    raw_notes, channel_base = extract_notes(track, args.channel_base)
    print(f"Read {len(raw_notes)} notes from channel-base {channel_base} "
          f"(1-indexed MIDI ch {channel_base + 1}..{channel_base + 4}).")

    if args.time_signature is not None:
        time_sigs = [(0, args.time_signature)]
    else:
        time_sigs = extract_time_signatures(mid)
        if not time_sigs:
            print("WARNING: no time-signature meta event found in the capture; defaulting to "
                  "4/4. Pass --time-signature to override (or re-capture with a current "
                  "OrchCapture build, which embeds the host's own meter).", file=sys.stderr)
            time_sigs = [(0, "4/4")]
        else:
            print(f"Detected time signature(s) from capture: "
                  f"{', '.join(sig for _, sig in time_sigs)}.")

    _guard_hand_playability(raw_notes, args.max_hand_span, args.max_hand_notes)
    _report_hand_crossing(raw_notes, mid.ticks_per_beat)

    grouped = _group_by_line_and_onset(raw_notes)
    # _fix_voice_stem_order runs BEFORE _guard_staggered_overlaps even on the
    # MIDI path (which otherwise has no use for stem direction) - it swaps
    # which onset-groups sit in the voice-1 vs voice-2 bucket, which changes
    # what _guard_staggered_overlaps considers "the same line" and therefore
    # what it truncates. write_midi() re-merges both buckets per hand anyway,
    # so the swap itself is a no-op for the final MIDI notes, but running the
    # guards in a different order than the MusicXML path would silently
    # change which overlaps get caught.
    _fix_voice_stem_order(grouped)
    _guard_staggered_overlaps(grouped)

    if args.output_path.lower().endswith((".mid", ".midi")):
        # MIDI path skips build_score()/music21 entirely - the dynamics
        # marks are a MusicXML-only concern (<dynamics> text; raw velocity
        # is still in the file either way). It also skips the octave-tremolo
        # notation collapse below - real audio playback needs the actual
        # alternating notes, not their two-note notated shorthand.
        write_midi(grouped, mid.ticks_per_beat, args.output_path, notation_scale=args.notation_scale)
        print(f"Wrote {args.output_path}")
        return

    # MusicXML path only, and only from here on: OrchPiano's octave-tremolo
    # feature (Phase 5c-2d) plays a real alternating run for correct audio,
    # but notating each strike literally beams out as many individual
    # noteheads rather than the conventional two-note tremolo shorthand -
    # collapse it before building the score.
    _collapse_octave_tremolos(grouped, mid.ticks_per_beat)

    # min_hold_beats is divided by the scale so "holds for 1 beat" still
    # means one beat of the FINAL, post-scale notation, not one beat of the
    # denser pre-scale grid - the marks themselves are computed at unscaled
    # offsets here and get carried along by build_score's augmentOrDiminish
    # at the end, same as every note.
    dynamics_marks = [] if args.no_dynamics else compute_dynamics_marks(
        raw_notes, mid.ticks_per_beat, min_hold_beats=1.0 / args.notation_scale)
    if dynamics_marks:
        print(f"Computed {len(dynamics_marks)} dynamics mark(s) from velocity.")

    score = build_score(grouped, mid.ticks_per_beat, time_sigs, dynamics_marks,
                        notation_scale=args.notation_scale)
    score.write("musicxml", fp=args.output_path)
    _add_part_symbol_brace(args.output_path)
    print(f"Wrote {args.output_path}")


if __name__ == "__main__":
    main()
