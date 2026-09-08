#!/usr/bin/env python3
"""Desktop GUI for orchpiano_finisher.py - lets the user point at any
OrchCapture .mid, inspect/override every setting, and run the Finisher
without touching a terminal. Built 2026-09-08, after enough manual CLI
runs during testing that a GUI earned its keep for trying OTHER pieces
(not just Grieg) without re-typing flags each time.

Zero-dependency by design, matching orchpiano_finisher.py's own stated
philosophy (see test_finisher.py's docstring: "a zero-dependency script
like the tool itself") - tkinter ships with a standard CPython install,
so this needs nothing beyond what orchpiano_finisher.py itself already
requires (mido, music21).

Everything here is a thin form-filling layer over process_capture() -
the actual pipeline logic lives in orchpiano_finisher.py and is NOT
duplicated here; see that module for what each setting actually does.

Run: python finisher_gui.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import mido

from orchpiano_finisher import (
    extract_notes,
    extract_time_signatures,
    find_orchpiano_track,
    process_capture,
)


class TextRedirector:
    """Makes a tkinter Text widget usable as a `sys.stdout`/`sys.stderr`
    target via contextlib.redirect_stdout/redirect_stderr - process_capture()
    (and everything it calls) just print()s and writes to sys.stderr like
    the CLI always has; this is the only piece that needed to change to
    show that same output in a GUI log pane instead of a terminal."""

    def __init__(self, widget: tk.Text, tag: str | None = None):
        self.widget = widget
        self.tag = tag

    def write(self, text: str) -> None:
        if not text:
            return
        self.widget.configure(state="normal")
        self.widget.insert("end", text, self.tag)
        self.widget.see("end")
        self.widget.configure(state="disabled")
        self.widget.update_idletasks()

    def flush(self) -> None:
        pass


class FinisherGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("OrchPiano Finisher")
        root.geometry("760x720")

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.track_var = tk.StringVar()
        self.channel_base_auto = tk.BooleanVar(value=True)
        self.channel_base_value = tk.StringVar()
        self.time_sig_auto_label = tk.StringVar(value="(load a file to auto-detect)")
        self.time_sig_override = tk.StringVar()
        self.max_hand_span = tk.StringVar(value="14")
        self.max_hand_notes = tk.StringVar(value="4")
        self.notation_scale = tk.StringVar(value="1.0")
        self.output_format = tk.StringVar(value="mid")   # "mid" or "musicxml"
        self.no_dynamics = tk.BooleanVar(value=False)

        self._loaded_mid: mido.MidiFile | None = None
        self._loaded_mid_path: str | None = None
        self._track_choices: list[tuple[str, mido.MidiTrack]] = []  # (label, track)

        self._build_ui()

    # ---- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        # --- Input file ---
        frm_in = ttk.LabelFrame(self.root, text="Captured MIDI file (from OrchCapture)")
        frm_in.pack(fill="x", **pad)
        input_entry = ttk.Entry(frm_in, textvariable=self.input_path)
        input_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=6)
        # A path typed/pasted directly (not via Browse) should still
        # auto-populate the track list/time-signature preview - silently a
        # no-op if it's not yet a valid file (e.g. still mid-edit), not an
        # error dialog on every keystroke.
        input_entry.bind("<FocusOut>", self._on_input_path_edited)
        input_entry.bind("<Return>", self._on_input_path_edited)
        ttk.Button(frm_in, text="Browse...", command=self._browse_input).pack(side="left", padx=(0, 8), pady=6)

        # --- Track selection ---
        frm_track = ttk.LabelFrame(self.root, text="Track (auto-populated once a file is loaded)")
        frm_track.pack(fill="x", **pad)
        self.track_combo = ttk.Combobox(frm_track, textvariable=self.track_var, state="readonly", width=70)
        self.track_combo.pack(side="left", fill="x", expand=True, padx=8, pady=6)

        # --- Channel base ---
        frm_chan = ttk.LabelFrame(self.root, text="Channel base (0-indexed OrchPiano chanBase - 1)")
        frm_chan.pack(fill="x", **pad)
        ttk.Checkbutton(frm_chan, text="Auto-detect (lowest channel used on the track)",
                        variable=self.channel_base_auto, command=self._toggle_channel_base).pack(side="left", padx=8)
        self.channel_base_entry = ttk.Entry(frm_chan, textvariable=self.channel_base_value, width=6, state="disabled")
        self.channel_base_entry.pack(side="left", padx=8)

        # --- Time signature ---
        frm_ts = ttk.LabelFrame(self.root, text="Time signature")
        frm_ts.pack(fill="x", **pad)
        ttk.Label(frm_ts, textvariable=self.time_sig_auto_label).pack(side="left", padx=8)
        ttk.Label(frm_ts, text="Override (blank = use auto-detected):").pack(side="left", padx=(16, 4))
        ttk.Entry(frm_ts, textvariable=self.time_sig_override, width=8).pack(side="left", padx=4)

        # --- Hand-playability assumptions ---
        frm_hand = ttk.LabelFrame(
            self.root,
            text="Assumed OrchPiano settings for THIS take (the capture carries no record of "
                 "the plugin's own parameter state - these must match what OrchPiano was "
                 "actually configured to, or the safety-net guards enforce the wrong limits)")
        frm_hand.pack(fill="x", **pad)
        ttk.Label(frm_hand, text="Max Hand Span (semitones):").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        ttk.Entry(frm_hand, textvariable=self.max_hand_span, width=6).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(frm_hand, text="Notes / Hand (Reduce):").grid(row=0, column=2, sticky="w", padx=(24, 8), pady=4)
        ttk.Entry(frm_hand, textvariable=self.max_hand_notes, width=6).grid(row=0, column=3, sticky="w", pady=4)

        # --- Output format ---
        frm_fmt = ttk.LabelFrame(self.root, text="Output format")
        frm_fmt.pack(fill="x", **pad)
        ttk.Radiobutton(frm_fmt, text="MIDI (recommended - single-channel, recognized by Dorico as one piano)",
                        variable=self.output_format, value="mid", command=self._on_format_change).pack(anchor="w", padx=8, pady=2)
        ttk.Radiobutton(frm_fmt, text="MusicXML (dynamics/tremolo notation, but not the primary path anymore)",
                        variable=self.output_format, value="musicxml", command=self._on_format_change).pack(anchor="w", padx=8, pady=2)
        self.dynamics_check = ttk.Checkbutton(frm_fmt, text="Include velocity-derived dynamics marks (MusicXML only)",
                                              variable=self.no_dynamics, onvalue=False, offvalue=True, state="disabled")
        self.dynamics_check.pack(anchor="w", padx=24, pady=(0, 4))
        ttk.Label(frm_fmt, text="Notation scale (use 2.0 only for an MPL-driven take with a too-fine grid):").pack(anchor="w", padx=8)
        ttk.Entry(frm_fmt, textvariable=self.notation_scale, width=6).pack(anchor="w", padx=8, pady=(0, 6))

        # --- Output file ---
        frm_out = ttk.LabelFrame(self.root, text="Output file")
        frm_out.pack(fill="x", **pad)
        ttk.Entry(frm_out, textvariable=self.output_path).pack(side="left", fill="x", expand=True, padx=(8, 4), pady=6)
        ttk.Button(frm_out, text="Save As...", command=self._browse_output).pack(side="left", padx=(0, 8), pady=6)

        # --- Run ---
        frm_run = ttk.Frame(self.root)
        frm_run.pack(fill="x", **pad)
        ttk.Button(frm_run, text="Run Finisher", command=self._run).pack(side="left", padx=8)
        self.open_folder_btn = ttk.Button(frm_run, text="Open output folder", command=self._open_output_folder, state="disabled")
        self.open_folder_btn.pack(side="left", padx=8)

        # --- Log ---
        frm_log = ttk.LabelFrame(self.root, text="Output log")
        frm_log.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(frm_log, height=16, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=8, pady=6)
        self.log.tag_configure("stderr", foreground="#b45309")
        self.log.tag_configure("error", foreground="#b91c1c")

    # ---- behaviour --------------------------------------------------

    def _toggle_channel_base(self) -> None:
        self.channel_base_entry.configure(state="disabled" if self.channel_base_auto.get() else "normal")

    def _on_format_change(self) -> None:
        is_xml = self.output_format.get() == "musicxml"
        self.dynamics_check.configure(state="normal" if is_xml else "disabled")
        # Keep the output path's extension in sync with the chosen format,
        # if a path is already set.
        path = self.output_path.get()
        if path:
            base, _ext = os.path.splitext(path)
            self.output_path.set(base + (".musicxml" if is_xml else ".mid"))

    def _on_input_path_edited(self, _event=None) -> None:
        path = self.input_path.get().strip()
        if path and os.path.isfile(path) and self._loaded_mid_path != path:
            self._load_input(path)

    def _browse_input(self) -> None:
        start_dir = os.environ.get("TEMP", os.path.expanduser("~"))
        path = filedialog.askopenfilename(
            title="Select a captured MIDI file",
            initialdir=start_dir,
            filetypes=[("MIDI files", "*.mid *.midi"), ("All files", "*.*")],
        )
        if not path:
            return
        self.input_path.set(path)
        self._load_input(path)

    def _load_input(self, path: str) -> None:
        try:
            mid = mido.MidiFile(path)
        except Exception as exc:  # noqa: BLE001 - shown to the user, not swallowed
            messagebox.showerror("Could not read MIDI file", str(exc))
            return
        self._loaded_mid = mid
        self._loaded_mid_path = path

        # Populate the track dropdown: every track, labeled with its name
        # and note count, so a track sharing OrchCapture's own "Grand
        # Piano" naming for BOTH the meta and content track is still easy
        # to tell apart at a glance.
        choices = []
        for i, tr in enumerate(mid.tracks):
            name = next((m.name for m in tr if m.type == "track_name"), None)
            n_notes = sum(1 for m in tr if m.type in ("note_on", "note_off"))
            label = f"{i}: {name!r} ({n_notes} note events)"
            choices.append((label, tr))
        self._track_choices = choices
        self.track_combo["values"] = [c[0] for c in choices]

        # Pre-select using the SAME logic orchpiano_finisher.py's own
        # auto-detect uses (name match, then "the only track with notes"
        # fallback) - see find_orchpiano_track()'s own docstring for why
        # name matching alone isn't enough for a real capture.
        try:
            auto_track = find_orchpiano_track(mid, None)
            auto_idx = mid.tracks.index(auto_track)
            self.track_combo.current(auto_idx)
        except SystemExit:
            # Couldn't auto-pick (e.g. more than one plausible content
            # track) - leave the dropdown for the user to choose from.
            pass

        # Time signature auto-detect preview.
        time_sigs = extract_time_signatures(mid)
        if time_sigs:
            sigs = ", ".join(sig for _, sig in time_sigs)
            self.time_sig_auto_label.set(f"Auto-detected from capture: {sigs}")
        else:
            self.time_sig_auto_label.set("No time-signature meta event found - will default to 4/4")

        # Suggest an output path next to the input file.
        if not self.output_path.get():
            base, _ext = os.path.splitext(path)
            ext = ".musicxml" if self.output_format.get() == "musicxml" else ".mid"
            self.output_path.set(base + "_finished" + ext)

    def _browse_output(self) -> None:
        is_xml = self.output_format.get() == "musicxml"
        ext = ".musicxml" if is_xml else ".mid"
        filetypes = [("MusicXML", "*.musicxml")] if is_xml else [("MIDI files", "*.mid *.midi")]
        start_dir = os.path.dirname(self.output_path.get()) or os.path.dirname(self.input_path.get()) or os.path.expanduser("~")
        path = filedialog.asksaveasfilename(
            title="Save Finisher output as", initialdir=start_dir,
            defaultextension=ext, filetypes=filetypes + [("All files", "*.*")],
        )
        if path:
            self.output_path.set(path)

    def _open_output_folder(self) -> None:
        path = self.output_path.get()
        if path and os.path.exists(path):
            os.startfile(os.path.dirname(path) or ".")  # noqa: S606 - local Windows convenience only

    def _selected_track_arg(self) -> str | None:
        idx = self.track_combo.current()
        if idx < 0:
            return None
        return str(idx)   # process_capture/find_orchpiano_track accept a track INDEX as a string

    def _run(self) -> None:
        input_path = self.input_path.get().strip()
        output_path = self.output_path.get().strip()
        if not input_path:
            messagebox.showwarning("No input file", "Choose a captured MIDI file first.")
            return
        if not output_path:
            messagebox.showwarning("No output file", "Choose where to save the Finisher's output first.")
            return

        try:
            max_span = int(self.max_hand_span.get())
            max_notes = int(self.max_hand_notes.get())
            scale = float(self.notation_scale.get())
        except ValueError:
            messagebox.showerror("Invalid number", "Max Hand Span / Notes per Hand / Notation Scale must be numbers.")
            return

        channel_base = None
        if not self.channel_base_auto.get():
            raw = self.channel_base_value.get().strip()
            if raw:
                try:
                    channel_base = int(raw)
                except ValueError:
                    messagebox.showerror("Invalid number", "Channel base must be a whole number.")
                    return

        time_sig = self.time_sig_override.get().strip() or None

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.open_folder_btn.configure(state="disabled")

        stdout_redirect = TextRedirector(self.log)
        stderr_redirect = TextRedirector(self.log, tag="stderr")
        try:
            with contextlib.redirect_stdout(stdout_redirect), contextlib.redirect_stderr(stderr_redirect):
                process_capture(
                    input_path, output_path,
                    track=self._selected_track_arg(),
                    channel_base=channel_base,
                    time_signature=time_sig,
                    max_hand_span=max_span,
                    max_hand_notes=max_notes,
                    no_dynamics=self.no_dynamics.get(),
                    notation_scale=scale,
                )
        except SystemExit as exc:
            stderr_redirect.write(f"\nERROR: {exc}\n")
            self.log.tag_add("error", "end-2l", "end-1l")
            return
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not a crash
            stderr_redirect.write(f"\nUNEXPECTED ERROR: {exc}\n")
            self.log.tag_add("error", "end-2l", "end-1l")
            return

        self.open_folder_btn.configure(state="normal")


def main() -> None:
    root = tk.Tk()
    FinisherGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
