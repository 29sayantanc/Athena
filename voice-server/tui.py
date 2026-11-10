"""TUI (Textual User Interface) for configuring and starting the Voice Server.

Uses the `textual` library for a rich terminal UI. Lets the user:
  - Pick TTS language from available languages
  - Pick TTS voice from available voices (filtered by language where possible)
  - Set host / port
  - Toggle auto-echo mode
  - Start the server (with port fallback)
  - View live logs in a scrolling panel
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.logging import TextualHandler
from textual.widgets import (
    Button, Footer, Header, Input, Label, Log, Select, Static, Switch,
)
from textual.reactive import reactive

from config import (
    settings, AVAILABLE_VOICES, AVAILABLE_LANGUAGES, DEFAULT_VOICE_FOR_LANG,
)


class VoiceServerTUI(App):
    """Configuration + launcher TUI for the voice server."""

    TITLE = "Voice Server — Parakeet STT + Pocket TTS"
    CSS = """
    Screen {
        background: $surface;
    }
    #main-container {
        padding: 1 2;
    }
    .title {
        text-align: center;
        text-style: bold;
        color: $accent;
        padding: 1 0;
    }
    .section {
        border: round $accent;
        padding: 1 2;
        margin: 1 0;
    }
    .section-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .field-row {
        height: 3;
        align: left middle;
    }
    .field-label {
        width: 16;
        padding: 1 1 0 0;
        color: $text-muted;
    }
    .field-input {
        width: 1fr;
    }
    Select {
        width: 1fr;
    }
    #log-panel {
        border: round $primary;
        height: 1fr;
        min-height: 10;
    }
    Log {
        height: 1fr;
    }
    #start-btn {
        background: $success;
        color: $text;
        text-style: bold;
    }
    #stop-btn {
        background: $error;
        color: $text;
    }
    .status {
        text-style: bold;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("s", "start_server", "Start", show=True),
        Binding("x", "stop_server", "Stop", show=True),
    ]

    server_running = reactive(False)
    selected_language = reactive(settings.tts_language)
    selected_voice = reactive(settings.tts_voice)
    selected_host = reactive(settings.host)
    selected_port = reactive(settings.port)
    auto_echo = reactive(settings.auto_echo)
    server_thread: Optional[threading.Thread] = None
    server_uvicorn = None
    stop_requested = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="main-container"):
            yield Static("🎙️  Voice Server Configuration", classes="title")

            with Container(classes="section"):
                yield Static("TTS Settings", classes="section-title")
                with Horizontal(classes="field-row"):
                    yield Label("Language:", classes="field-label")
                    yield Select(
                        id="language-select",
                        options=[(lang, lang) for lang in AVAILABLE_LANGUAGES],
                        value=settings.tts_language,
                    )
                with Horizontal(classes="field-row"):
                    yield Label("Voice:", classes="field-label")
                    yield Select(
                        id="voice-select",
                        options=[(v, v) for v in AVAILABLE_VOICES],
                        value=settings.tts_voice,
                    )

            with Container(classes="section"):
                yield Static("Server Settings", classes="section-title")
                with Horizontal(classes="field-row"):
                    yield Label("Host:", classes="field-label")
                    yield Input(value=settings.host, id="host-input", classes="field-input")
                with Horizontal(classes="field-row"):
                    yield Label("Port:", classes="field-label")
                    yield Input(value=str(settings.port), id="port-input", classes="field-input")
                with Horizontal(classes="field-row"):
                    yield Label("Auto-Echo:", classes="field-label")
                    yield Switch(value=settings.auto_echo, id="echo-switch")
                    yield Label("(STT→TTS demo mode)", classes="field-label")

            with Horizontal():
                yield Button("▶ Start Server", id="start-btn", variant="success")
                yield Button("■ Stop Server", id="stop-btn", variant="error")
                yield Static("Status: Stopped", id="status-label", classes="status")

            with Container(id="log-panel"):
                yield Static("Live Logs", classes="section-title")
                yield Log(id="server-log", highlight=True)

        yield Footer()

    def on_mount(self) -> None:
        # Redirect logging to our Log widget
        self.log_widget = self.query_one("#server-log", Log)
        handler = logging.Handler()
        handler.emit = self._log_handler  # type: ignore
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        self._update_voice_options()

    def _log_handler(self, record: logging.LogRecord) -> None:
        try:
            msg = f"{record.levelname} {record.name} | {record.getMessage()}"
            if self.server_running or True:
                self.log_widget.write_line(msg)
        except Exception:
            pass

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "language-select":
            self.selected_language = event.value
            settings.tts_language = event.value
            # Auto-select default voice for language
            default_v = DEFAULT_VOICE_FOR_LANG.get(event.value, AVAILABLE_VOICES[0])
            self.selected_voice = default_v
            settings.tts_voice = default_v
            self._update_voice_options()
            self.query_one("#voice-select", Select).value = default_v
        elif event.select.id == "voice-select":
            self.selected_voice = event.value
            settings.tts_voice = event.value

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "host-input":
            self.selected_host = event.value
            settings.host = event.value
        elif event.input.id == "port-input":
            try:
                self.selected_port = int(event.value)
                settings.port = int(event.value)
            except ValueError:
                pass

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id == "echo-switch":
            self.auto_echo = event.value
            settings.auto_echo = event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start-btn":
            self.action_start_server()
        elif event.button.id == "stop-btn":
            self.action_stop_server()

    def _update_voice_options(self) -> None:
        """Refresh voice dropdown with all available voices."""
        voice_select = self.query_one("#voice-select", Select)
        voice_select.set_options([(v, v) for v in AVAILABLE_VOICES])

    def watch_server_running(self, running: bool) -> None:
        status = self.query_one("#status-label", Static)
        start_btn = self.query_one("#start-btn", Button)
        stop_btn = self.query_one("#stop-btn", Button)
        if running:
            status.update(f"Status: Running on {self.selected_host}:{settings.port}")
            start_btn.disabled = True
            stop_btn.disabled = False
        else:
            status.update("Status: Stopped")
            start_btn.disabled = False
            stop_btn.disabled = True

    def action_start_server(self) -> None:
        if self.server_running:
            return
        self.stop_requested = False
        self.log_widget.write_line("[bold green]Starting server...[/]")
        self.server_thread = threading.Thread(target=self._run_server_thread, daemon=True)
        self.server_thread.start()
        self.server_running = True

    def _run_server_thread(self) -> None:
        """Run the server in a background thread."""
        import server as server_module
        try:
            server_module.run_server(
                host=self.selected_host,
                port=self.selected_port,
                preload=True,
                on_server_created=self._capture_uvicorn_server,
            )
        except Exception as e:
            self.call_from_thread(
                self.log_widget.write_line,
                f"[bold red]Server error: {e}[/]",
            )
        finally:
            self.server_uvicorn = None
            self.call_from_thread(setattr, self, "server_running", False)

    def _capture_uvicorn_server(self, server) -> None:
        self.server_uvicorn = server
        if self.stop_requested:
            server.should_exit = True

    def action_stop_server(self) -> None:
        if not self.server_running:
            return
        self.stop_requested = True
        self.log_widget.write_line("[bold yellow]Stopping server...[/]")
        if self.server_uvicorn is None:
            self.log_widget.write_line("[yellow]Server is still starting; stop requested.[/]")
            return
        self.server_uvicorn.should_exit = True
        self.query_one("#stop-btn", Button).disabled = True


def main():
    app = VoiceServerTUI()
    app.run()


if __name__ == "__main__":
    main()
