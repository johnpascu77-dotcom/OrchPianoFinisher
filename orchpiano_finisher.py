#!/usr/bin/env python3
"""OrchPiano Finisher - Phase 1 (merge only, no dynamics).

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

Scope (see Docs/OrchPianoFinisher_Design.md): this phase only merges and
writes notation. It does NOT re-decide melody/bass/hand assignment, does not
add dynamics, and does not run the cross-hand/cross-onset overlap safety net
planned for Phase 2 - it has a narrow staggered-overlap guard only (see
_guard_staggered_overlaps), which is not a substitute for that later work.
Same-onset chords on the lead voice (voice 1 is NOT monophonic - it carries
whatever streamHandVoices() didn't peel off as the secondary voice) are
notated as real chords, never mistaken for overlaps.
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
    args = ap.parse_args()

    mid = mido.MidiFile(args.input_mid)
    track = find_orchpiano_track(mid, args.track)
    raw_notes, channel_base = extract_notes(track, args.channel_base)
    print(f"Read {len(raw_notes)} notes from channel-base {channel_base} "
          f"(1-indexed MIDI ch {channel_base + 1}..{channel_base + 4}).")

    grouped = _group_by_line_and_onset(raw_notes)
    _guard_staggered_overlaps(grouped)

    score = build_score(grouped, mid.ticks_per_beat, args.time_signature)
    score.write("musicxml", fp=args.output_musicxml)
    print(f"Wrote {args.output_musicxml}")


if __name__ == "__main__":
    main()
