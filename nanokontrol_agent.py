#!/usr/bin/env python3
"""Multi-agent TUI driven by the NanoKONTROL2 MIDI controller.

Per-column controls (columns 1-8):
  S  (CC 32-39)  spawn / kill agent pane  (LED on while active)
  M  (CC 48-55)  move voice focus to that column  (LED on = focused)
  R  (CC 64-71)  clear conversation history
  Fader (CC 0-7) reasoning budget for that column

Transport:
  PLAY  (CC 41)  hold = record, release = transcribe & send to focused agent
  STOP  (CC 42)  abort focused agent's current turn
  q / ESC        quit

LED note: LEDs only respond if the nanoKONTROL2 is in External LED Mode
(set once via KORG KONTROL Editor; this script sends the SysEx on startup).
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import urwid

try:
    import speech_recognition as sr
except ImportError:
    sys.exit("pip install SpeechRecognition")

from agentknit.openai_compat import SubprocessOpenAI

# ── hardware / binary paths ────────────────────────────────────────────────────

MIDI_DEV        = "/dev/snd/midiC1D0"
COMPLETIONS_BIN = str(Path.home() / "bin" / "claude-haiku-completions.py")
AUDIO_DEVICE    = "plughw:0,0"

# ── NanoKONTROL2 CC map ───────────────────────────────────────────────────────

CC_FADERS  = list(range(0,  8))   # CC 0-7
CC_SOLOS   = list(range(32, 40))  # CC 32-39  S buttons
CC_MUTES   = list(range(48, 56))  # CC 48-55  M buttons
CC_RECORDS = list(range(64, 72))  # CC 64-71  R buttons
CC_PLAY    = 41
CC_STOP    = 42

# SysEx: switch nanoKORG into Native (external LED) mode.  The third byte is
# 0x40 | global-MIDI-channel; we don't know the device's global channel, so
# _set_native_mode() broadcasts on all 16.
def _native_mode_sysex(channel: int, enable: bool) -> bytes:
    return bytes([
        0xF0, 0x42, 0x40 | channel, 0x00, 0x01, 0x13, 0x00,
        0x00, 0x00, 0x01 if enable else 0x00, 0xF7,
    ])

# ── Reasoning effort levels ────────────────────────────────────────────────────

EFFORT_LEVELS: list[tuple[str, int | None]] = [
    ("off",     None),
    ("minimal", 512),
    ("low",     1024),
    ("medium",  8000),
    ("high",    16000),
    ("xhigh",   32000),
]


def fader_to_effort(val: int) -> tuple[str, int | None]:
    idx = val * len(EFFORT_LEVELS) // 128
    return EFFORT_LEVELS[min(idx, len(EFFORT_LEVELS) - 1)]


# ── Shell tool ────────────────────────────────────────────────────────────────

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": "Run a bash command and return combined stdout+stderr. Timeout 30 s.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
            },
            "required": ["command"],
        },
    },
}]


def _run_shell(command: str) -> str:
    try:
        r = subprocess.run(
            ["bash", "-c", command], text=True, capture_output=True, timeout=30,
        )
        return (r.stdout + r.stderr).strip()[:4000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: timed out after 30 s"
    except Exception as exc:
        return f"ERROR: {exc}"


# ── Per-column agent state ────────────────────────────────────────────────────

@dataclass
class AgentPane:
    col: int
    messages: list[dict] = field(default_factory=list)
    walker:   urwid.SimpleFocusListWalker = field(
        default_factory=lambda: urwid.SimpleFocusListWalker([]))
    fader:    int  = 64
    busy:     bool = False
    cancel:   bool = False

    def add_line(self, widget: urwid.Widget) -> None:
        self.walker.append(widget)
        try:
            self.walker.set_focus(len(self.walker) - 1)
        except Exception:
            pass


# ── Shared MIDI fd (O_RDWR — device refuses two separate opens) ───────────────

_midi_fd: list[int] = [-1]
_midi_lock = threading.Lock()
_midi_err: list[str] = [""]


def _device_holders() -> str:
    """Return a human description of any process holding MIDI_DEV open."""
    try:
        out = subprocess.run(
            ["lsof", "-t", MIDI_DEV], text=True, capture_output=True, timeout=5,
        ).stdout.split()
        if not out:
            return ""
        names = []
        for pid in out:
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                comm = "?"
            names.append(f"{comm}({pid})")
        return ", ".join(names)
    except Exception:
        return ""


def _midi_open() -> bool:
    """Open the controller read+write. ALSA rawmidi allows only ONE open."""
    try:
        _midi_fd[0] = os.open(MIDI_DEV, os.O_RDWR)
        _midi_err[0] = ""
        return True
    except OSError as exc:
        holders = _device_holders()
        _midi_err[0] = f"Cannot open {MIDI_DEV}: {exc.strerror}"
        if holders:
            _midi_err[0] += f" — held by {holders}. Kill it and restart."
        return False


def _midi_send(data: bytes) -> None:
    fd = _midi_fd[0]
    if fd >= 0:
        with _midi_lock:
            try:
                os.write(fd, data)
            except Exception:
                pass


def _led(cc: int, on: bool) -> None:
    # LED echo must arrive on the device's global MIDI channel, which we
    # can't query — broadcast on all 16, the device ignores the wrong ones.
    val = 127 if on else 0
    for ch in range(16):
        _midi_send(bytes([0xB0 | ch, cc, val]))


def _set_native_mode(enable: bool) -> None:
    for ch in range(16):
        _midi_send(_native_mode_sysex(ch, enable))


# ── MIDI input thread ─────────────────────────────────────────────────────────

_event_q: queue.Queue[dict] = queue.Queue()


def _midi_thread() -> None:
    fd = _midi_fd[0]
    if fd < 0:
        return
    try:
        status = 0
        data: list[int] = []
        while True:
            raw = os.read(fd, 1)
            if not raw:
                continue
            b = raw[0]
            if b & 0x80:
                status = b
                data = []
            else:
                data.append(b)
                if (status & 0xF0) == 0xB0 and len(data) == 2:
                    _event_q.put({"type": "midi_cc", "cc": data[0], "val": data[1]})
                    data = []
    except Exception as exc:
        _event_q.put({"type": "error", "col": None, "text": f"MIDI: {exc}"})


# ── Voice recording ───────────────────────────────────────────────────────────

_rec_proc: list[subprocess.Popen | None] = [None]
_rec_file: list[str | None]              = [None]


def _start_recording() -> None:
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    _rec_proc[0] = subprocess.Popen(
        ["arecord", "-q", "-D", AUDIO_DEVICE,
         "-f", "S16_LE", "-c", "1", "-r", "16000", "-t", "wav", tmp],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _rec_file[0] = tmp


def _stop_and_transcribe(col: int) -> None:
    proc, path = _rec_proc[0], _rec_file[0]
    _rec_proc[0] = _rec_file[0] = None
    if proc:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
    if not path or not os.path.exists(path):
        _event_q.put({"type": "error", "col": col, "text": "No audio captured"})
        return
    try:
        rec = sr.Recognizer()
        with sr.AudioFile(path) as src:
            audio = rec.record(src)
        text = rec.recognize_google(audio)
        _event_q.put({"type": "prompt_ready", "col": col, "text": text})
    except sr.UnknownValueError:
        _event_q.put({"type": "error", "col": col, "text": "Could not understand audio"})
    except Exception as exc:
        _event_q.put({"type": "error", "col": col, "text": f"STT: {exc}"})
    finally:
        try: os.unlink(path)
        except OSError: pass


# ── Agent turn thread ─────────────────────────────────────────────────────────

def _agent_thread(pane: AgentPane, prompt: str) -> None:
    pane.cancel = False
    effort_name, budget = fader_to_effort(pane.fader)
    col = pane.col

    pane.messages.append({"role": "user", "content": prompt})
    _event_q.put({"type": "agent_start", "col": col})

    client  = SubprocessOpenAI(COMPLETIONS_BIN)
    extra   = {"reasoning_effort": str(budget)} if budget else {}

    try:
        while not pane.cancel:
            resp = client.chat.completions.create(
                model="claude-haiku-4-5-20251001",
                messages=list(pane.messages),
                tools=TOOLS,
                tool_choice="auto",
                extra_body=extra or None,
            )
            msg = resp.choices[0].message

            if msg.tool_calls:
                pane.messages.append({
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name,
                                      "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    cmd = json.loads(tc.function.arguments).get("command", "")
                    _event_q.put({"type": "tool_call", "col": col, "cmd": cmd})
                    out = _run_shell(cmd)
                    _event_q.put({"type": "tool_result", "col": col, "output": out})
                    pane.messages.append({
                        "role": "tool", "tool_call_id": tc.id, "content": out,
                    })
            else:
                reply = msg.content or ""
                pane.messages.append({"role": "assistant", "content": reply})
                _event_q.put({"type": "agent_reply", "col": col,
                              "text": reply, "effort": effort_name})
                break

    except Exception as exc:
        _event_q.put({"type": "error", "col": col, "text": str(exc)})

    _event_q.put({"type": "agent_done", "col": col})


# ── Colour palette ────────────────────────────────────────────────────────────

PALETTE = [
    ("header",  "white,bold",     "dark blue"),
    ("footer",  "light gray",     "dark blue"),
    ("focused", "white,bold",     "dark magenta"),
    ("user",    "light cyan",     "default"),
    ("agent",   "light green",    "default"),
    ("rec",     "light red,bold", "default"),
    ("system",  "yellow",         "default"),
    ("dim",     "dark gray",      "default"),
    ("err",     "light red",      "default"),
]

# ── TUI ───────────────────────────────────────────────────────────────────────

def launch_tui() -> None:
    panes:      dict[int, AgentPane] = {}   # col → AgentPane
    focus_col:  list[int | None]     = [None]
    recording:  list[bool]           = [False]

    # ── layout ────────────────────────────────────────────────────────────────
    title_txt  = urwid.Text(" NanoKONTROL2 · claude-haiku-4-5 ", align="center")
    footer_txt = urwid.Text("", wrap="clip")
    placeholder = urwid.Filler(
        urwid.Text(("dim", "Press S on any column to open an agent pane."), align="center"))
    body_pile   = urwid.Pile([("weight", 1, placeholder)])

    frame = urwid.Frame(
        body_pile,
        header=urwid.AttrMap(title_txt, "header"),
        footer=urwid.AttrMap(footer_txt, "footer"),
    )

    def _unhandled_key(key: str | tuple) -> None:
        if key in ("q", "Q", "esc"):
            raise urwid.ExitMainLoop()

    loop = urwid.MainLoop(frame, palette=PALETTE, unhandled_input=_unhandled_key)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _set_footer(text: str, attr: str = "footer") -> None:
        footer_txt.set_text((attr, text))

    def _idle_footer() -> None:
        fc = focus_col[0]
        if fc is not None and fc in panes:
            name, _ = fader_to_effort(panes[fc].fader)
            bar = "█" * (panes[fc].fader * 16 // 128) + "░" * (16 - panes[fc].fader * 16 // 128)
            _set_footer(
                f" Col {fc+1} focused  {bar} {name.upper()}"
                "   [PLAY] Speak  [STOP] Abort  [S] Open/close  [M] Focus  [q] Quit"
            )
        else:
            _set_footer(" [S] Open column  [q] Quit")

    def _rebuild_body() -> None:
        """Rebuild the body Pile from active panes ordered by column."""
        if not panes:
            body_pile.contents[:] = [
                (placeholder, ("weight", 1)),
            ]
            return
        cols_widget = urwid.Columns(
            [("weight", 1, _pane_widget(p)) for p in sorted(panes.values(), key=lambda p: p.col)],
            dividechars=1,
        )
        body_pile.contents[:] = [(cols_widget, ("weight", 1))]

    def _pane_widget(p: AgentPane) -> urwid.Widget:
        fc = focus_col[0]
        name, _ = fader_to_effort(p.fader)
        bar   = "█" * (p.fader * 12 // 128) + "░" * (12 - p.fader * 12 // 128)
        title = f" {p.col+1} [{bar} {name.upper()}]"
        lb    = urwid.ListBox(p.walker)
        box   = urwid.LineBox(lb, title=title)
        if fc == p.col:
            return urwid.AttrMap(box, "focused")
        return box

    def _update_leds() -> None:
        for i in range(8):
            _led(CC_SOLOS[i],  i in panes)
            _led(CC_MUTES[i],  i == focus_col[0])

    def _spawn(col: int) -> None:
        p = AgentPane(col=col)
        p.add_line(urwid.Text(("dim", f"Agent {col+1} ready.")))
        panes[col] = p
        if focus_col[0] is None:
            focus_col[0] = col
        _rebuild_body()
        _update_leds()
        _idle_footer()

    def _kill(col: int) -> None:
        panes.pop(col, None)
        if focus_col[0] == col:
            focus_col[0] = min(panes.keys()) if panes else None
        _rebuild_body()
        _update_leds()
        _idle_footer()

    def _set_focus(col: int) -> None:
        if col not in panes:
            return
        focus_col[0] = col
        _rebuild_body()
        _update_leds()
        _idle_footer()

    # ── event drain ───────────────────────────────────────────────────────────

    tick = [0]

    def _drain(lp: urwid.MainLoop, _: Any = None) -> None:
        # Re-assert LED state every ~2 s so it converges even if the
        # controller missed or dropped an earlier message.
        tick[0] += 1
        if tick[0] % 40 == 0:
            _update_leds()
        try:
            while True:
                ev   = _event_q.get_nowait()
                kind = ev["type"]

                if kind == "midi_cc":
                    cc, val = ev["cc"], ev["val"]

                    if cc in CC_SOLOS and val == 127:
                        col = CC_SOLOS.index(cc)
                        if col in panes:
                            _kill(col)
                        else:
                            _spawn(col)

                    elif cc in CC_MUTES and val == 127:
                        _set_focus(CC_MUTES.index(cc))

                    elif cc in CC_RECORDS and val == 127:
                        col = CC_RECORDS.index(cc)
                        if col in panes:
                            p = panes[col]
                            p.messages.clear()
                            p.walker[:] = [urwid.Text(("dim", "History cleared."))]
                            _rebuild_body()

                    elif cc in CC_FADERS:
                        col = CC_FADERS.index(cc)
                        if col in panes:
                            panes[col].fader = val
                            _rebuild_body()
                            _idle_footer()

                    elif cc == CC_PLAY and val == 127:
                        fc = focus_col[0]
                        if fc is not None and fc in panes and not panes[fc].busy and not recording[0]:
                            recording[0] = True
                            _start_recording()
                            _set_footer(" ● RECORDING … release PLAY to send", "rec")

                    elif cc == CC_PLAY and val == 0:
                        if recording[0]:
                            recording[0] = False
                            fc = focus_col[0]
                            _set_footer(" ⟳ Transcribing …", "system")
                            threading.Thread(
                                target=_stop_and_transcribe,
                                args=(fc,), daemon=True,
                            ).start()

                    elif cc == CC_STOP and val == 127:
                        fc = focus_col[0]
                        if recording[0]:
                            recording[0] = False
                            proc = _rec_proc[0]
                            if proc:
                                proc.send_signal(signal.SIGINT)
                            _rec_proc[0] = None
                        if fc is not None and fc in panes:
                            panes[fc].cancel = True
                        _idle_footer()

                elif kind == "prompt_ready":
                    col, text = ev["col"], ev["text"]
                    if col is not None and col in panes:
                        p = panes[col]
                        p.add_line(urwid.Text(("user", f"You › {text}")))
                        p.busy = True
                        _set_footer(f" ⟳ Agent {col+1} thinking …", "system")
                        threading.Thread(
                            target=_agent_thread, args=(p, text), daemon=True,
                        ).start()

                elif kind == "tool_call":
                    col = ev["col"]
                    if col in panes:
                        panes[col].add_line(urwid.Text(("dim", f"  $ {ev['cmd']}")))

                elif kind == "tool_result":
                    col = ev["col"]
                    if col in panes:
                        for line in ev["output"].splitlines()[:8]:
                            panes[col].add_line(urwid.Text(("dim", f"    {line}")))

                elif kind == "agent_reply":
                    col = ev["col"]
                    if col in panes:
                        effort = ev.get("effort", "")
                        tag    = f"[{effort}] " if effort and effort != "off" else ""
                        panes[col].add_line(urwid.Text(("agent", f"Agent {tag}› {ev['text']}")))

                elif kind == "agent_done":
                    col = ev["col"]
                    if col in panes:
                        panes[col].busy = False
                    _idle_footer()

                elif kind == "error":
                    col = ev.get("col")
                    if col is not None and col in panes:
                        panes[col].busy = False
                        panes[col].add_line(urwid.Text(("err", f"✗ {ev['text']}")))
                    else:
                        footer_txt.set_text(("err", f" ✗ {ev['text']}"))
                    recording[0] = False
                    _idle_footer()

        except queue.Empty:
            pass

        lp.set_alarm_in(0.05, _drain)

    # ── boot ──────────────────────────────────────────────────────────────────
    _idle_footer()

    def _fatal(msg: str) -> None:
        body_pile.contents[:] = [
            (urwid.Filler(urwid.Text(("err", msg), align="center")), ("weight", 1)),
        ]
        _set_footer(" Controller unavailable — [q] Quit", "err")

    if not os.path.exists(MIDI_DEV):
        _fatal(f"MIDI device {MIDI_DEV} not found.\nIs the nanoKONTROL2 plugged in?")
    elif not _midi_open():
        # ALSA rawmidi permits a single open; a leftover process blocks everything.
        _fatal(_midi_err[0])
    else:
        _set_native_mode(True)
        threading.Thread(target=_midi_thread, daemon=True).start()

    loop.set_alarm_in(0.05, _drain)
    loop.run()

    # cleanup: turn off all LEDs, leave native mode, release the device
    if _midi_fd[0] >= 0:
        for i in range(8):
            _led(CC_SOLOS[i], False)
            _led(CC_MUTES[i], False)
        _set_native_mode(False)
        os.close(_midi_fd[0])
        _midi_fd[0] = -1


if __name__ == "__main__":
    launch_tui()
