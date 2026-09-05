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
assignment or note content, does not add dynamics (Phase 3).

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
import sys
from dataclasses import dataclass

import mido
from music21 import chord, clef, instrument, layout, meter, note, stream

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
                    span_truncated += 1
                else:
                    victim = min(candidates, key=lambda a: a.start_tick)
                    count_truncated += 1
                victim.end_tick = min(victim.end_tick, n.start_tick)
                active.remove(victim)
    if span_truncated:
        print(f"NOTE: truncated {span_truncated} note(s) to keep the real (cross-attack) "
              f"hand span <= {max_span} semitones.", file=sys.stderr)
    if count_truncated:
        print(f"NOTE: truncated {count_truncated} note(s) to keep the real (cross-attack) "
              f"hand note-count <= {max_notes}.", file=sys.stderr)


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


def build_score(grouped: dict[tuple[str, int], list[list[RawNote]]], ticks_per_beat: int,
                time_sig: str) -> stream.Score:
    score = stream.Score()

    # PartStaff (not plain Part) + a braced StaffGroup with barTogether=True is
    # what actually notates as one grand staff on import, rather than two
    # separate single-staff instruments - the whole point of this tool.
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
        p.insert(0, meter.TimeSignature(time_sig))

    voices: dict[tuple[str, int], stream.Voice] = {}

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

    for (staff, voice_num), v in sorted(voices.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        parts[staff].insert(0, v)

    for staff in ("RH", "LH"):
        p = parts[staff]
        if not any(isinstance(el, stream.Voice) for el in p):
            # Nothing landed on this hand at all (Hands=Left/Right run) - an
            # empty staff is legitimate, not an error.
            continue
        # Real captured performance timing isn't grid-aligned; MusicXML can
        # only express notated (non-arbitrary-fraction) durations, so snap to
        # the nearest 16th note or 8th-note triplet before notating. This is
        # a display-quantization step, not a musical decision OrchPiano
        # already made - it does not touch pitch, hand, or voice assignment.
        p.quantize(inPlace=True, recurse=True)
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
        score.insert(0, p)

    present_parts = list(score.parts)
    if not present_parts:
        raise SystemExit("No notes ended up on either staff - check --channel-base / input file")

    if len(present_parts) == 2:
        score.insert(0, layout.StaffGroup(present_parts[0], present_parts[1],
                                          name="Piano", symbol="brace", barTogether=True))

    return score


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input_mid", help="Captured MIDI file (e.g. from OrchCapture)")
    ap.add_argument("output_musicxml", help="Output .musicxml path")
    ap.add_argument("--track", default=None,
                     help="Track index or exact track name to read (default: auto-detect by 'OrchPiano' in the name)")
    ap.add_argument("--channel-base", type=int, default=None,
                     help="0-indexed base channel (OrchPiano's chanBase - 1). Default: auto-detect as the lowest channel used on the track.")
    ap.add_argument("--time-signature", default="4/4",
                     help="Time signature for notation purposes (default 4/4) - OrchPiano's captured MIDI carries no time-signature meta event.")
    ap.add_argument("--max-hand-span", type=int, default=14,
                     help="Assumed OrchPiano 'Max Hand Span (st)' for this take, in semitones (default 14, OrchPiano's own default). "
                          "Phase 2 enforces this against real overlapping note timing, not just each onset group's own snapshot.")
    ap.add_argument("--max-hand-notes", type=int, default=4,
                     help="Assumed OrchPiano 'Notes / Hand (Reduce)' for this take (default 4, OrchPiano's own default). "
                          "Enforced the same way as --max-hand-span.")
    args = ap.parse_args()

    mid = mido.MidiFile(args.input_mid)
    track = find_orchpiano_track(mid, args.track)
    raw_notes, channel_base = extract_notes(track, args.channel_base)
    print(f"Read {len(raw_notes)} notes from channel-base {channel_base} "
          f"(1-indexed MIDI ch {channel_base + 1}..{channel_base + 4}).")

    _guard_hand_playability(raw_notes, args.max_hand_span, args.max_hand_notes)
    _report_hand_crossing(raw_notes, mid.ticks_per_beat)

    grouped = _group_by_line_and_onset(raw_notes)
    _fix_voice_stem_order(grouped)
    _guard_staggered_overlaps(grouped)

    score = build_score(grouped, mid.ticks_per_beat, args.time_signature)
    score.write("musicxml", fp=args.output_musicxml)
    print(f"Wrote {args.output_musicxml}")


if __name__ == "__main__":
    main()
