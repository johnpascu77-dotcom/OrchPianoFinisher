# OrchPiano Finisher

Turns an OrchCapture recording of OrchPiano's 4-channel Reduce output into a finished MIDI file
(the primary path - see `Docs/OrchPianoFinisher_Design.md` §20 for why MusicXML was set aside)
or a MusicXML score, applying a real-time-only safety net OrchPiano's own streaming engine can't
(cross-attack hand-span/count checks, same-onset chord vs. staggered-overlap handling, and - for
MIDI - merging both hands onto a single track/channel so Dorico's own import recognizes it as one
piano instrument instead of two).

## GUI (recommended for trying new pieces)

```
python finisher_gui.py
```

Point it at any OrchCapture `.mid`, and it auto-populates the track list, auto-detects the time
signature, and pre-fills OrchPiano's own default Max Hand Span (14 semitones) / Notes-per-Hand
(4). Override anything the dropdown/fields don't get right for this particular take, then
**Run Finisher** - the log pane shows the same diagnostic messages the CLI prints.

Zero extra dependencies beyond what the CLI tool already needs (`tkinter` ships with a standard
CPython install).

## CLI

```
python orchpiano_finisher.py <input.mid> <output.mid> [options]
```

Run `python orchpiano_finisher.py --help` for the full option list (channel base, time signature
override, hand-span/notes-per-hand assumptions, notation scale, MusicXML dynamics toggle).

## Is this universal, or does it need tweaking per piece?

Mostly universal for anything captured the same way (OrchCapture -> OrchPiano Reduce -> this
tool) - channel-base detection, time-signature detection, and the actual note-safety-net/
single-instrument-MIDI logic all adapt to whatever the capture contains, no code changes needed
between pieces.

Two things are genuinely piece/take-specific, because the capture itself carries no record of
them:

- **Max Hand Span / Notes per Hand** - these must match whatever OrchPiano's own UI sliders were
  actually set to for that take. Wrong values mean the safety net enforces the wrong limits (too
  loose or too strict) relative to what OrchPiano itself already decided. The GUI defaults to
  OrchPiano's own defaults (14 / 4); change them if a given take used something else.
- **Notation scale** - only relevant for an MPL-driven take whose native grid is too fine to read
  cleanly (leave at 1.0 for anything else, including every Bitwig/OrchPiano orchestral capture
  tested so far).

Track auto-detection also improved 2026-09-08: it no longer requires the track to literally be
named "OrchPiano" (real captures are usually named after whatever the Bitwig track is called,
e.g. "Grand Piano") - if exactly one track in the file has any notes, that's used automatically
regardless of its name.
