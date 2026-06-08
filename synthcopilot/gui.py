"""SynthCoPilot GUI — dark synthwave control panel for beatmap generation.

Wires directly into the parser, geometry, and rhythm backend modules.
Runs heavy work (librosa analysis, rail generation) on a background thread
to keep the UI responsive, routing all print() output to the live console.
"""

import io
import os
import sys
import threading
import tkinter as tk
from pathlib import Path

import customtkinter as ctk

from synthcopilot.cli import parse_timestamp
from synthcopilot.geometry import generate_rail
from synthcopilot.models import DIFFICULTIES, HAND_LEFT, HAND_RIGHT, Rail
from synthcopilot.parser import cleanup, get_audio_path, load, save
from synthcopilot.rhythm import detect_onsets, snap_notes_to_rail

# -- Synthwave palette --
BG_DARK = "#0b0b14"
BG_PANEL = "#111122"
BG_FRAME = "#161630"
NEON_PURPLE = "#b026ff"
NEON_PINK = "#ff2d95"
NEON_CYAN = "#00f0ff"
NEON_GREEN = "#39ff14"
TEXT_PRIMARY = "#e0e0ff"
TEXT_DIM = "#8888aa"
ENTRY_BG = "#1a1a35"
BORDER_GLOW = "#6a0dad"


class ConsoleRedirector(io.TextIOBase):
    """Routes write() calls to the GUI console textbox, thread-safe."""

    def __init__(self, textbox: ctk.CTkTextbox, app: ctk.CTk):
        super().__init__()
        self._textbox = textbox
        self._app = app

    def write(self, text: str) -> int:
        if text:
            self._app.after(0, self._append, text)
        return len(text) if text else 0

    def _append(self, text: str) -> None:
        self._textbox.configure(state="normal")
        self._textbox.insert("end", text)
        self._textbox.see("end")
        self._textbox.configure(state="disabled")

    def flush(self) -> None:
        pass


class SynthCoPilotApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("SynthCoPilot")
        self.geometry("1120x740")
        self.minsize(960, 640)
        self.configure(fg_color=BG_DARK)

        self._track_data = None
        self._work_dir = None
        self._source_path = None
        self._generating = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._build_workspace()
        self._build_console()
        self._redirect_stdout()

    # ------------------------------------------------------------------ #
    #  Layout builders                                                     #
    # ------------------------------------------------------------------ #

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(self, width=260, corner_radius=0, fg_color=BG_PANEL)
        sidebar.grid(row=0, column=0, rowspan=2, sticky="nsw")
        sidebar.grid_propagate(False)

        logo = ctk.CTkLabel(
            sidebar, text="SYNTH\nCO-PILOT",
            font=ctk.CTkFont(family="Consolas", size=22, weight="bold"),
            text_color=NEON_PURPLE,
        )
        logo.pack(pady=(24, 4))

        tagline = ctk.CTkLabel(
            sidebar, text="BEATMAP CO-PILOT",
            font=ctk.CTkFont(size=10), text_color=TEXT_DIM,
        )
        tagline.pack(pady=(0, 20))

        sep = ctk.CTkFrame(sidebar, height=1, fg_color=BORDER_GLOW)
        sep.pack(fill="x", padx=16, pady=(0, 16))

        self._btn_load = ctk.CTkButton(
            sidebar, text="Load .synth Map",
            fg_color=NEON_PURPLE, hover_color="#8a1cc7",
            text_color="#ffffff", font=ctk.CTkFont(size=13, weight="bold"),
            command=self._load_map,
        )
        self._btn_load.pack(padx=16, pady=(0, 12), fill="x")

        info_frame = ctk.CTkFrame(sidebar, fg_color=BG_FRAME, corner_radius=8)
        info_frame.pack(padx=16, fill="x", pady=(0, 12))

        self._lbl_map_name = ctk.CTkLabel(
            info_frame, text="No map loaded",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=TEXT_PRIMARY,
            anchor="w", wraplength=210,
        )
        self._lbl_map_name.pack(padx=10, pady=(8, 2), anchor="w")

        self._lbl_map_detail = ctk.CTkLabel(
            info_frame, text="",
            font=ctk.CTkFont(size=11), text_color=TEXT_DIM,
            anchor="w", wraplength=210, justify="left",
        )
        self._lbl_map_detail.pack(padx=10, pady=(0, 8), anchor="w")

        diff_label = ctk.CTkLabel(
            sidebar, text="DIFFICULTY", font=ctk.CTkFont(size=10, weight="bold"),
            text_color=TEXT_DIM,
        )
        diff_label.pack(padx=16, anchor="w")

        self._diff_var = ctk.StringVar(value="Expert")
        self._diff_menu = ctk.CTkOptionMenu(
            sidebar, variable=self._diff_var,
            values=list(DIFFICULTIES),
            fg_color=ENTRY_BG, button_color=NEON_PURPLE,
            button_hover_color="#8a1cc7", text_color=TEXT_PRIMARY,
            dropdown_fg_color=BG_FRAME, dropdown_text_color=TEXT_PRIMARY,
            dropdown_hover_color=NEON_PURPLE,
        )
        self._diff_menu.pack(padx=16, pady=(4, 12), fill="x")

        hand_label = ctk.CTkLabel(
            sidebar, text="HAND", font=ctk.CTkFont(size=10, weight="bold"),
            text_color=TEXT_DIM,
        )
        hand_label.pack(padx=16, anchor="w")

        self._hand_var = ctk.StringVar(value="Right")
        self._hand_menu = ctk.CTkOptionMenu(
            sidebar, variable=self._hand_var,
            values=["Right", "Left"],
            fg_color=ENTRY_BG, button_color=NEON_PURPLE,
            button_hover_color="#8a1cc7", text_color=TEXT_PRIMARY,
            dropdown_fg_color=BG_FRAME, dropdown_text_color=TEXT_PRIMARY,
            dropdown_hover_color=NEON_PURPLE,
        )
        self._hand_menu.pack(padx=16, pady=(4, 20), fill="x")

        sep2 = ctk.CTkFrame(sidebar, height=1, fg_color=BORDER_GLOW)
        sep2.pack(fill="x", padx=16, pady=(0, 16))

        self._btn_save = ctk.CTkButton(
            sidebar, text="Save / Export Map",
            fg_color=NEON_PINK, hover_color="#cc2277",
            text_color="#ffffff", font=ctk.CTkFont(size=13, weight="bold"),
            command=self._save_map,
        )
        self._btn_save.pack(padx=16, fill="x", side="bottom", pady=(0, 20))

    def _build_workspace(self) -> None:
        workspace = ctk.CTkFrame(self, fg_color=BG_DARK, corner_radius=0)
        workspace.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_columnconfigure(1, weight=1)
        workspace.grid_rowconfigure(0, weight=0)
        workspace.grid_rowconfigure(1, weight=0)
        workspace.grid_rowconfigure(2, weight=1)

        self._build_geometry_frame(workspace)
        self._build_rhythm_frame(workspace)
        self._build_action_area(workspace)

    def _build_geometry_frame(self, parent) -> None:
        frame = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=12)
        frame.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)

        header = ctk.CTkLabel(
            frame, text="TIME & GEOMETRY",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=NEON_CYAN,
        )
        header.pack(padx=14, pady=(12, 10), anchor="w")

        time_row = ctk.CTkFrame(frame, fg_color="transparent")
        time_row.pack(padx=14, fill="x", pady=(0, 6))
        time_row.grid_columnconfigure(1, weight=1)
        time_row.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(time_row, text="Start", font=ctk.CTkFont(size=11),
                     text_color=TEXT_DIM).grid(row=0, column=0, padx=(0, 4))
        self._entry_start = ctk.CTkEntry(
            time_row, placeholder_text="00:00", width=80,
            fg_color=ENTRY_BG, border_color=BORDER_GLOW, text_color=TEXT_PRIMARY,
        )
        self._entry_start.grid(row=0, column=1, sticky="ew")

        ctk.CTkLabel(time_row, text="End", font=ctk.CTkFont(size=11),
                     text_color=TEXT_DIM).grid(row=0, column=2, padx=(12, 4))
        self._entry_end = ctk.CTkEntry(
            time_row, placeholder_text="00:00", width=80,
            fg_color=ENTRY_BG, border_color=BORDER_GLOW, text_color=TEXT_PRIMARY,
        )
        self._entry_end.grid(row=0, column=3, sticky="ew")

        pos_row = ctk.CTkFrame(frame, fg_color="transparent")
        pos_row.pack(padx=14, fill="x", pady=(0, 6))
        for i in range(8):
            pos_row.grid_columnconfigure(i, weight=1 if i % 2 else 0)

        labels = ["X1", "Y1", "X2", "Y2"]
        defaults = ["0.0", "1.5", "0.0", "1.5"]
        self._pos_entries = []
        for i, (lbl, dflt) in enumerate(zip(labels, defaults)):
            ctk.CTkLabel(pos_row, text=lbl, font=ctk.CTkFont(size=10),
                         text_color=TEXT_DIM).grid(row=0, column=i * 2, padx=(0 if i == 0 else 6, 2))
            entry = ctk.CTkEntry(
                pos_row, width=52, fg_color=ENTRY_BG,
                border_color=BORDER_GLOW, text_color=TEXT_PRIMARY,
            )
            entry.insert(0, dflt)
            entry.grid(row=0, column=i * 2 + 1, sticky="ew")
            self._pos_entries.append(entry)

        ctk.CTkLabel(frame, text="Rail Modifier", font=ctk.CTkFont(size=11),
                     text_color=TEXT_DIM).pack(padx=14, anchor="w", pady=(4, 0))
        self._rail_var = ctk.StringVar(value="smooth")
        self._rail_menu = ctk.CTkOptionMenu(
            frame, variable=self._rail_var,
            values=["smooth", "wave", "spiral", "zigzag"],
            fg_color=ENTRY_BG, button_color=NEON_CYAN,
            button_hover_color="#00b8c2", text_color=TEXT_PRIMARY,
            dropdown_fg_color=BG_FRAME, dropdown_text_color=TEXT_PRIMARY,
            dropdown_hover_color=NEON_CYAN,
        )
        self._rail_menu.pack(padx=14, fill="x", pady=(2, 8))

        self._complexity_val = tk.IntVar(value=0)
        self._add_slider(frame, "Complexity", self._complexity_val, 0, 10, 1)

        self._fade_val = tk.DoubleVar(value=0.15)
        self._add_slider(frame, "Envelope Fade Zone", self._fade_val, 0.0, 0.5, 0.01)

        self._max_vel_val = tk.DoubleVar(value=4.0)
        self._add_slider(frame, "Max Velocity (clamp)", self._max_vel_val, 0.0, 10.0, 0.1)

        self._nodes_val = tk.IntVar(value=16)
        self._add_slider(frame, "Rail Nodes", self._nodes_val, 4, 64, 1)

    def _build_rhythm_frame(self, parent) -> None:
        frame = ctk.CTkFrame(parent, fg_color=BG_PANEL, corner_radius=12)
        frame.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)

        header = ctk.CTkLabel(
            frame, text="RHYTHM & FILTERING",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=NEON_PINK,
        )
        header.pack(padx=14, pady=(12, 10), anchor="w")

        toggle_row = ctk.CTkFrame(frame, fg_color="transparent")
        toggle_row.pack(padx=14, fill="x", pady=(0, 8))

        self._snap_var = ctk.StringVar(value="off")
        self._snap_switch = ctk.CTkSwitch(
            toggle_row, text="Snap to Audio Micro-Beats",
            variable=self._snap_var, onvalue="on", offvalue="off",
            font=ctk.CTkFont(size=12), text_color=TEXT_PRIMARY,
            progress_color=NEON_PINK, button_color=TEXT_PRIMARY,
            button_hover_color=NEON_PINK,
            command=self._on_snap_toggle,
        )
        self._snap_switch.pack(anchor="w")

        self._rhythm_widgets_frame = ctk.CTkFrame(frame, fg_color="transparent")
        self._rhythm_widgets_frame.pack(padx=0, fill="x")

        self._sensitivity_val = tk.DoubleVar(value=1.0)
        self._add_slider(self._rhythm_widgets_frame, "Onset Sensitivity",
                         self._sensitivity_val, 0.1, 3.0, 0.1)

        self._cooldown_val = tk.DoubleVar(value=50.0)
        self._add_slider(self._rhythm_widgets_frame, "Cooldown (ms)",
                         self._cooldown_val, 10.0, 200.0, 1.0)

        self._hand_speed_val = tk.DoubleVar(value=6.0)
        self._add_slider(self._rhythm_widgets_frame, "Max Hand Speed (grid-u/s)",
                         self._hand_speed_val, 0.0, 15.0, 0.1)

        self._set_rhythm_panel_state("disabled")

    def _build_action_area(self, parent) -> None:
        action = ctk.CTkFrame(parent, fg_color="transparent")
        action.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 4))

        self._btn_generate = ctk.CTkButton(
            action, text="GENERATE & INJECT SEQUENCE",
            height=52,
            font=ctk.CTkFont(family="Consolas", size=18, weight="bold"),
            fg_color=NEON_GREEN, hover_color="#2ecc10",
            text_color="#000000", corner_radius=10,
            command=self._generate,
        )
        self._btn_generate.pack(fill="x", pady=(0, 6))

        self._progress = ctk.CTkProgressBar(
            action, height=6, corner_radius=3,
            fg_color=BG_FRAME, progress_color=NEON_CYAN,
        )
        self._progress.pack(fill="x")
        self._progress.set(0)

    def _build_console(self) -> None:
        console_frame = ctk.CTkFrame(self, fg_color=BG_PANEL, corner_radius=0, height=170)
        console_frame.grid(row=1, column=1, sticky="nsew", padx=(4, 0), pady=(0, 0))
        console_frame.grid_propagate(False)
        console_frame.grid_columnconfigure(0, weight=1)
        console_frame.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(console_frame, fg_color="transparent", height=24)
        bar.grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            bar, text="CONSOLE",
            font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
            text_color=NEON_GREEN,
        ).pack(side="left", padx=10, pady=4)

        ctk.CTkButton(
            bar, text="Clear", width=50, height=20,
            font=ctk.CTkFont(size=10), fg_color=BG_FRAME,
            hover_color=BORDER_GLOW, text_color=TEXT_DIM,
            command=self._clear_console,
        ).pack(side="right", padx=10, pady=4)

        self._console = ctk.CTkTextbox(
            console_frame, fg_color=BG_DARK, text_color=NEON_GREEN,
            font=ctk.CTkFont(family="Consolas", size=11),
            corner_radius=0, border_width=0, state="disabled",
            wrap="word",
        )
        self._console.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

    # ------------------------------------------------------------------ #
    #  Widget helpers                                                       #
    # ------------------------------------------------------------------ #

    def _add_slider(self, parent, label: str, variable, from_, to, step) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(padx=14, fill="x", pady=(0, 6))

        is_int = isinstance(variable, tk.IntVar)

        val_label = ctk.CTkLabel(
            row, text=f"{variable.get():.0f}" if is_int else f"{variable.get():.2f}",
            font=ctk.CTkFont(family="Consolas", size=11),
            text_color=NEON_CYAN, width=44, anchor="e",
        )

        def _on_change(value):
            if is_int:
                snapped = int(round(float(value)))
                variable.set(snapped)
                val_label.configure(text=f"{snapped}")
            else:
                snapped = round(float(value) / step) * step
                variable.set(snapped)
                val_label.configure(text=f"{snapped:.2f}")

        ctk.CTkLabel(row, text=label, font=ctk.CTkFont(size=11),
                     text_color=TEXT_DIM).pack(side="left")
        val_label.pack(side="right")

        slider = ctk.CTkSlider(
            parent, from_=from_, to=to, variable=variable,
            command=_on_change, height=14,
            fg_color=BG_FRAME, progress_color=NEON_PURPLE,
            button_color=TEXT_PRIMARY, button_hover_color=NEON_CYAN,
        )
        slider.pack(padx=14, fill="x", pady=(0, 4))

    def _set_rhythm_panel_state(self, state: str) -> None:
        for child in self._rhythm_widgets_frame.winfo_children():
            try:
                child.configure(state=state)
            except (tk.TclError, ValueError):
                pass
            for sub in child.winfo_children():
                try:
                    sub.configure(state=state)
                except (tk.TclError, ValueError):
                    pass

    def _on_snap_toggle(self) -> None:
        enabled = self._snap_var.get() == "on"
        self._set_rhythm_panel_state("normal" if enabled else "disabled")

    # ------------------------------------------------------------------ #
    #  Console / stdout                                                    #
    # ------------------------------------------------------------------ #

    def _redirect_stdout(self) -> None:
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        redirector = ConsoleRedirector(self._console, self)
        sys.stdout = redirector
        sys.stderr = redirector

    def _clear_console(self) -> None:
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def log(self, msg: str) -> None:
        print(msg)

    # ------------------------------------------------------------------ #
    #  File I/O                                                            #
    # ------------------------------------------------------------------ #

    def _load_map(self) -> None:
        path = ctk.filedialog.askopenfilename(
            title="Select .synth Map",
            filetypes=[("Synth Riders Map", "*.synth"), ("ZIP Archive", "*.zip"), ("All Files", "*.*")],
        )
        if not path:
            return

        if self._work_dir:
            cleanup(self._work_dir)
            self._work_dir = None
            self._track_data = None

        try:
            self._track_data, self._work_dir = load(path)
            self._source_path = path
        except Exception as e:
            self.log(f"[ERROR] Failed to load: {e}")
            return

        name = self._track_data.name or Path(path).stem
        self._lbl_map_name.configure(text=name)

        bpm = self._track_data.bpm
        author = self._track_data.author or "Unknown"
        audio = self._track_data.audio_filename or "none"
        diffs_with_data = []
        for d_name, d in self._track_data.difficulties.items():
            total = len(d.notes) + len(d.rails) + len(d.walls)
            if total:
                diffs_with_data.append(f"{d_name} ({len(d.notes)}n/{len(d.rails)}r)")

        detail_lines = [
            f"Author: {author}  |  BPM: {bpm}",
            f"Audio: {audio}",
        ]
        if diffs_with_data:
            detail_lines.append("  ".join(diffs_with_data))
        self._lbl_map_detail.configure(text="\n".join(detail_lines))

        self.log(f"Loaded: {Path(path).name}")
        self.log(f"  BPM={bpm}  Offset={self._track_data.offset}  Audio={audio}")
        for d_name, d in self._track_data.difficulties.items():
            total = len(d.notes) + len(d.rails) + len(d.walls)
            if total:
                self.log(f"  {d_name}: {len(d.notes)} notes, {len(d.rails)} rails, {len(d.walls)} walls")

    def _save_map(self) -> None:
        if not self._track_data or not self._work_dir:
            self.log("[WARN] No map loaded — nothing to save.")
            return

        default_name = Path(self._source_path).stem + "_modified.synth" if self._source_path else "output.synth"
        path = ctk.filedialog.asksaveasfilename(
            title="Save .synth Map",
            defaultextension=".synth",
            initialfile=default_name,
            filetypes=[("Synth Riders Map", "*.synth"), ("All Files", "*.*")],
        )
        if not path:
            return

        try:
            out = save(self._track_data, self._work_dir, path)
            self.log(f"Saved: {out}")
        except Exception as e:
            self.log(f"[ERROR] Save failed: {e}")

    # ------------------------------------------------------------------ #
    #  Generation pipeline                                                 #
    # ------------------------------------------------------------------ #

    def _generate(self) -> None:
        if self._generating:
            return
        if not self._track_data or not self._work_dir:
            self.log("[WARN] Load a .synth map first.")
            return

        start_str = self._entry_start.get().strip()
        end_str = self._entry_end.get().strip()
        if not start_str or not end_str:
            self.log("[WARN] Enter start and end timestamps.")
            return

        try:
            start_sec = parse_timestamp(start_str)
            end_sec = parse_timestamp(end_str)
        except ValueError as e:
            self.log(f"[ERROR] Bad timestamp: {e}")
            return

        if end_sec <= start_sec:
            self.log("[WARN] End time must be after start time.")
            return

        try:
            sx = float(self._pos_entries[0].get())
            sy = float(self._pos_entries[1].get())
            ex = float(self._pos_entries[2].get())
            ey = float(self._pos_entries[3].get())
        except ValueError:
            self.log("[ERROR] Position values must be numbers.")
            return

        params = {
            "start_sec": start_sec,
            "end_sec": end_sec,
            "start_x": sx,
            "start_y": sy,
            "end_x": ex,
            "end_y": ey,
            "rail_type": self._rail_var.get(),
            "complexity": self._complexity_val.get(),
            "fade_zone": self._fade_val.get(),
            "max_velocity": self._max_vel_val.get(),
            "num_nodes": self._nodes_val.get(),
            "difficulty": self._diff_var.get(),
            "hand": HAND_LEFT if self._hand_var.get() == "Left" else HAND_RIGHT,
            "snap_to_audio": self._snap_var.get() == "on",
            "sensitivity": self._sensitivity_val.get(),
            "cooldown": self._cooldown_val.get() / 1000.0,
            "max_hand_speed": self._hand_speed_val.get(),
        }

        self._generating = True
        self._btn_generate.configure(state="disabled", text="GENERATING...")
        self._progress.set(0)

        thread = threading.Thread(target=self._generate_worker, args=(params,), daemon=True)
        thread.start()

    def _generate_worker(self, p: dict) -> None:
        try:
            self._set_progress(0.05)

            start_beat = self._track_data.seconds_to_beats(p["start_sec"])
            end_beat = self._track_data.seconds_to_beats(p["end_sec"])
            start_anchor = (p["start_x"], p["start_y"], start_beat)
            end_anchor = (p["end_x"], p["end_y"], end_beat)

            print(f"Generating {p['rail_type']} rail, complexity={p['complexity']}, "
                  f"nodes={p['num_nodes']}, fade={p['fade_zone']:.2f}")

            self._set_progress(0.15)

            rail_nodes = generate_rail(
                start=start_anchor,
                end=end_anchor,
                num_nodes=p["num_nodes"],
                rail_type=p["rail_type"],
                complexity=p["complexity"],
                fade_zone=p["fade_zone"],
                max_velocity=p["max_velocity"],
            )

            self._set_progress(0.40)
            print(f"Generated {len(rail_nodes)} rail nodes "
                  f"[{start_anchor[2]:.1f} -> {end_anchor[2]:.1f} beats]")
            if p["max_velocity"] > 0:
                print(f"Applied bidirectional velocity clamping (max={p['max_velocity']:.1f})")

            diff = self._track_data.difficulties.get(p["difficulty"])
            if diff is None:
                print(f"[ERROR] Difficulty '{p['difficulty']}' not found in track")
                return

            new_rail = Rail(hand_type=p["hand"], nodes=rail_nodes)
            diff.rails.append(new_rail)
            hand_name = "left" if p["hand"] == HAND_LEFT else "right"
            print(f"Injected rail into {p['difficulty']} ({hand_name} hand)")

            notes_added = 0
            if p["snap_to_audio"]:
                self._set_progress(0.50)
                audio_path = get_audio_path(self._work_dir)
                if audio_path is None:
                    print("[WARN] No audio file in archive — skipping onset detection")
                else:
                    print(f"Analyzing audio: {os.path.basename(audio_path)} "
                          f"[{p['start_sec']:.1f}s - {p['end_sec']:.1f}s]")

                    self._set_progress(0.55)
                    raw_onsets = detect_onsets(
                        audio_path, p["start_sec"], p["end_sec"], p["sensitivity"],
                    )
                    print(f"Detected {len(raw_onsets)} raw transients "
                          f"(sensitivity={p['sensitivity']:.1f})")

                    self._set_progress(0.75)
                    notes = snap_notes_to_rail(
                        raw_onsets, rail_nodes,
                        p["start_sec"], p["end_sec"],
                        self._track_data.bpm, self._track_data.offset,
                        p["hand"],
                        min_gap=p["cooldown"],
                        max_hand_speed=p["max_hand_speed"],
                    )
                    filtered_count = len(raw_onsets) - len(notes)
                    if filtered_count > 0:
                        print(f"Filtered {filtered_count} notes "
                              f"(cooldown={p['cooldown'] * 1000:.0f}ms, "
                              f"max_speed={p['max_hand_speed']:.1f})")
                    diff.notes.extend(notes)
                    notes_added = len(notes)
                    print(f"Snapped {notes_added} notes to audio onsets")

            self._set_progress(1.0)
            total = len(rail_nodes) + notes_added
            print(f"Done — {total} objects injected into {p['difficulty']}")

        except Exception as e:
            print(f"[ERROR] Generation failed: {e}")
        finally:
            self.after(0, self._generation_done)

    def _generation_done(self) -> None:
        self._generating = False
        self._btn_generate.configure(state="normal", text="GENERATE & INJECT SEQUENCE")

    def _set_progress(self, value: float) -> None:
        self.after(0, self._progress.set, value)

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    def destroy(self) -> None:
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr
        if self._work_dir:
            cleanup(self._work_dir)
        super().destroy()


def main():
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")
    app = SynthCoPilotApp()
    app.mainloop()


if __name__ == "__main__":
    main()
