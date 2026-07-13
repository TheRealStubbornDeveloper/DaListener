from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from platformdirs import user_data_path
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QCloseEvent, QDesktopServices, QFont, QFontDatabase, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QHeaderView, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from .audio import AudioDeviceService, CaptureManager
from .capability import CapabilityService
from .models import (
    CaptureMode, CaptureSelection, QualityMode, SourceKind, Stability, TranscriptEvent,
    TranscriptionLanguage,
)
from .session import SessionController
from .storage import SessionStore, TranscriptExporter


class WindowedTextStream:
    """A tqdm-compatible stream for pythonw, optionally forwarded to the UI."""

    encoding = "utf-8"
    ansi_pattern = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def __init__(self, callback=None):
        self.callback = callback
        self.last_message = ""
        self.last_emitted_at = 0.0

    def write(self, value) -> int:
        text = str(value or "")
        if self.callback:
            clean = self.ansi_pattern.sub("", text).replace("\r", "\n")
            messages = [line.strip() for line in clean.splitlines() if line.strip()]
            if messages:
                message = messages[-1]
                now = time.monotonic()
                if message != self.last_message and now - self.last_emitted_at >= 0.2:
                    self.callback(message)
                    self.last_message = message
                    self.last_emitted_at = now
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


# pythonw.exe deliberately starts without console streams. Some third-party
# downloaders assume stderr always has write(), so provide a safe stream before
# they are imported. The preparation worker replaces it with a UI-forwarding
# instance once the window is ready.
if sys.stderr is None:
    sys.stderr = WindowedTextStream()
if sys.stdout is None:
    sys.stdout = WindowedTextStream()


class UiBridge(QObject):
    transcript = Signal(object)
    status = Signal(str, str)
    level = Signal(str, float)
    model_ready = Signal(object)
    model_error = Signal(str)
    model_log = Signal(str)


class DaListenerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._ensure_font()
        self.setWindowTitle("DaListener")
        self.resize(1120, 760)
        self.setMinimumSize(900, 640)

        self.data_dir = Path(os.environ.get("DALISTENER_DATA_DIR") or user_data_path("DaListener", "DaListener"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self.settings = self._load_settings()
        self.events: dict[SourceKind, dict[str, TranscriptEvent]] = {
            SourceKind.MICROPHONE: {}, SourceKind.SYSTEM: {},
        }
        self.last_session_id: str | None = None
        self.last_saved_folder = self.data_dir
        self.started_at = 0.0
        self.test_capture: CaptureManager | None = None

        self.store = SessionStore(self.data_dir / "sessions.db")
        self.exporter = TranscriptExporter(self.store)
        self.capability_service = CapabilityService(self.data_dir / "capability.json")
        self.report = self.capability_service.inspect()
        self.device_service = AudioDeviceService()
        self.bridge = UiBridge()
        self.bridge.transcript.connect(self._on_transcript)
        self.bridge.status.connect(self._on_status)
        self.bridge.level.connect(self._on_level)
        self.bridge.model_ready.connect(self._on_model_ready)
        self.bridge.model_error.connect(self._on_model_error)
        self.bridge.model_log.connect(self._append_model_log)
        self.controller = SessionController(
            self.store, self.data_dir / "models", self.report.quality_mode.value,
            self.report.model_name, self.bridge.transcript.emit,
            lambda source, text: self.bridge.status.emit(source.value, text),
            lambda source, value: self.bridge.level.emit(source.value, value),
        )

        self._build_ui()
        self._apply_style()
        self._show_capability()
        self._refresh_devices()
        self._refresh_history()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(250)
        QTimer.singleShot(100, self._ensure_consent)

    @staticmethod
    def _ensure_font() -> None:
        font_path = Path(__file__).parent / "assets" / "Inter.ttf"
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            QApplication.instance().setFont(QFont(families[0], 10))

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        title = QLabel("DaListener")
        title.setObjectName("title")
        header.addWidget(title)
        subtitle = QLabel("Private live transcription on your device")
        subtitle.setObjectName("muted")
        header.addWidget(subtitle)
        header.addStretch()
        self.live_label = QLabel("● Ready")
        self.live_label.setObjectName("muted")
        header.addWidget(self.live_label)
        self.timer_label = QLabel("00:00:00")
        self.timer_label.setObjectName("muted")
        header.addWidget(self.timer_label)
        root.addLayout(header)

        cards = QHBoxLayout()
        cards.setSpacing(14)
        capability = self._card()
        cap_layout = QVBoxLayout(capability)
        cap_layout.addWidget(self._eyebrow("EXPECTED PERFORMANCE"))
        self.rating_label = QLabel("Checking…")
        self.rating_label.setObjectName("rating")
        cap_layout.addWidget(self.rating_label)
        self.capability_label = QLabel()
        cap_layout.addWidget(self.capability_label)
        self.details_label = QLabel()
        self.details_label.setObjectName("muted")
        self.details_label.setWordWrap(True)
        cap_layout.addWidget(self.details_label)
        cap_layout.addStretch()
        cards.addWidget(capability, 1)

        sources = self._card()
        source_grid = QGridLayout(sources)
        source_grid.addWidget(self._eyebrow("AUDIO SOURCES"), 0, 0, 1, 3)
        source_grid.addWidget(QLabel("Mode"), 1, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([mode.value for mode in CaptureMode])
        self.mode_combo.setCurrentText(self.settings.get("capture_mode", CaptureMode.BOTH.value))
        self.mode_combo.currentTextChanged.connect(self._update_source_states)
        source_grid.addWidget(self.mode_combo, 1, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh_devices)
        source_grid.addWidget(refresh, 1, 2)
        source_grid.addWidget(QLabel("Microphone"), 2, 0)
        self.mic_combo = QComboBox()
        source_grid.addWidget(self.mic_combo, 2, 1, 1, 2)
        source_grid.addWidget(QLabel("System output"), 3, 0)
        self.output_combo = QComboBox()
        source_grid.addWidget(self.output_combo, 3, 1, 1, 2)
        source_grid.addWidget(QLabel("Language"), 4, 0)
        self.language_combo = QComboBox()
        self.language_labels = {
            "Automatic (English + Tagalog)": TranscriptionLanguage.AUTO,
            "English": TranscriptionLanguage.ENGLISH,
            "Tagalog": TranscriptionLanguage.TAGALOG,
        }
        self.language_combo.addItems(self.language_labels)
        try:
            saved_language = TranscriptionLanguage(
                self.settings.get("language", TranscriptionLanguage.AUTO.value)
            )
        except ValueError:
            saved_language = TranscriptionLanguage.AUTO
        self.language_combo.setCurrentText(next(
            label for label, value in self.language_labels.items() if value == saved_language
        ))
        source_grid.addWidget(self.language_combo, 4, 1, 1, 2)
        source_grid.addWidget(self._eyebrow("MIC"), 5, 0)
        self.mic_level = QProgressBar()
        self.mic_level.setRange(0, 1000)
        self.mic_level.setTextVisible(False)
        source_grid.addWidget(self.mic_level, 5, 1)
        source_grid.addWidget(self._eyebrow("SYSTEM"), 6, 0)
        self.system_level = QProgressBar()
        self.system_level.setRange(0, 1000)
        self.system_level.setTextVisible(False)
        source_grid.addWidget(self.system_level, 6, 1)
        self.test_button = QPushButton("Test selected sources for 3 seconds")
        self.test_button.clicked.connect(self.test_sources)
        source_grid.addWidget(self.test_button, 7, 0, 1, 3)
        cards.addWidget(sources, 1)
        root.addLayout(cards)

        controls = QHBoxLayout()
        self.start_button = QPushButton("Preparing model…")
        self.start_button.setObjectName("primary")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.toggle_session)
        controls.addWidget(self.start_button)
        self.pause_button = QPushButton("Pause")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self.pause)
        controls.addWidget(self.pause_button)
        self.bookmark_button = QPushButton("Bookmark")
        self.bookmark_button.setEnabled(False)
        self.bookmark_button.clicked.connect(self.bookmark)
        controls.addWidget(self.bookmark_button)
        copy_button = QPushButton("Copy last")
        copy_button.clicked.connect(self.copy_last)
        controls.addWidget(copy_button)
        export_button = QPushButton("Export")
        export_button.clicked.connect(self.export)
        controls.addWidget(export_button)
        controls.addStretch()
        self.status_label = QLabel("Preparing local transcription model…")
        self.status_label.setObjectName("muted")
        controls.addWidget(self.status_label)
        root.addLayout(controls)

        self.save_notice = self._card()
        save_notice_layout = QHBoxLayout(self.save_notice)
        save_notice_layout.setContentsMargins(12, 8, 10, 8)
        self.save_notice_label = QLabel()
        self.save_notice_label.setObjectName("muted")
        self.save_notice_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        save_notice_layout.addWidget(self.save_notice_label, 1)
        self.open_folder_button = QPushButton("Open folder")
        self.open_folder_button.clicked.connect(self.open_saved_folder)
        save_notice_layout.addWidget(self.open_folder_button)
        dismiss_notice = QPushButton("Dismiss")
        dismiss_notice.clicked.connect(self.save_notice.hide)
        save_notice_layout.addWidget(dismiss_notice)
        self.save_notice.hide()
        root.addWidget(self.save_notice)

        model_log_card = self._card()
        model_log_layout = QVBoxLayout(model_log_card)
        model_log_layout.setContentsMargins(10, 8, 10, 8)
        model_log_layout.addWidget(self._eyebrow("MODEL PREPARATION LOG"))
        self.model_log = QTextEdit()
        self.model_log.setReadOnly(True)
        self.model_log.setMaximumHeight(118)
        self.model_log.setPlaceholderText("Download and initialization details will appear here.")
        model_log_layout.addWidget(self.model_log)
        root.addWidget(model_log_card)

        tabs = QTabWidget()
        live_tab = QWidget()
        history_tab = QWidget()
        tabs.addTab(live_tab, "Live transcript")
        tabs.addTab(history_tab, "History")
        root.addWidget(tabs, 1)

        live_layout = QVBoxLayout(live_tab)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find text in this transcript")
        self.search.textChanged.connect(self._render_all)
        search_row.addWidget(self.search)
        live_layout.addLayout(search_row)
        splitter = QSplitter(Qt.Horizontal)
        mic_panel, self.mic_text = self._transcript_panel("MICROPHONE / ME", "#58a6ff")
        system_panel, self.system_text = self._transcript_panel("SYSTEM / OUTPUT DEVICE", "#3fb950")
        splitter.addWidget(mic_panel)
        splitter.addWidget(system_panel)
        splitter.setSizes([550, 550])
        live_layout.addWidget(splitter, 1)

        history_layout = QVBoxLayout(history_tab)
        self.history = QTableWidget(0, 3)
        self.history.setHorizontalHeaderLabels(["Started", "Model", "Utterances"])
        self.history.horizontalHeader().setStretchLastSection(False)
        self.history.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.history.setSelectionBehavior(QTableWidget.SelectRows)
        self.history.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history.cellDoubleClicked.connect(self._load_history)
        history_layout.addWidget(self.history)

    def _card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        return card

    def _eyebrow(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("eyebrow")
        return label

    def _transcript_panel(self, title: str, color: str) -> tuple[QFrame, QTextEdit]:
        panel = self._card()
        layout = QVBoxLayout(panel)
        heading = self._eyebrow(title)
        heading.setStyleSheet(f"color: {color}")
        layout.addWidget(heading)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setAcceptRichText(False)
        layout.addWidget(text)
        return panel, text

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #0d1117; color: #e6edf3; font: 10pt 'Inter'; }
            #title { font-size: 22pt; font-weight: 600; }
            #muted, QLabel#muted { color: #8b949e; }
            #rating { color: #3fb950; font-size: 18pt; font-weight: 600; }
            #eyebrow { color: #8b949e; font-size: 9pt; font-weight: 600; }
            #card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
            QPushButton { background: #21262d; border: 1px solid #30363d; border-radius: 6px; padding: 8px 13px; }
            QPushButton:hover { background: #30363d; }
            QPushButton:disabled { color: #6e7681; }
            QPushButton#primary { background: #58a6ff; color: #071018; font-weight: 600; }
            QComboBox, QLineEdit { background: #21262d; border: 1px solid #30363d; border-radius: 5px; padding: 7px; }
            QComboBox QAbstractItemView { background: #21262d; color: #e6edf3; selection-background-color: #1f6feb; }
            QTextEdit { background: #161b22; border: 0; padding: 8px; font-size: 11pt; }
            QTabWidget::pane { border: 1px solid #30363d; }
            QTabBar::tab { background: #161b22; padding: 9px 16px; }
            QTabBar::tab:selected { background: #21262d; color: #58a6ff; }
            QProgressBar { background: #30363d; border: 0; height: 7px; }
            QProgressBar::chunk { background: #58a6ff; }
            QTableWidget { background: #161b22; gridline-color: #30363d; }
            QHeaderView::section { background: #21262d; padding: 7px; border: 0; }
        """)

    def _ensure_consent(self) -> None:
        if not self.settings.get("consent_acknowledged"):
            result = QMessageBox.question(
                self, "Private and responsible listening",
                "DaListener processes audio locally and does not retain raw audio by default.\n\n"
                "You are responsible for notifying participants and following recording and consent laws.\n\n"
                "Continue and prepare the local transcription model?",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if result != QMessageBox.Ok:
                self.close()
                return
            self.settings["consent_acknowledged"] = True
            self._save_settings()
        self._prepare_model_async()

    def _prepare_model_async(self) -> None:
        self.start_button.setText("Preparing model…")
        self.start_button.setEnabled(False)
        self.model_log.clear()
        self.bridge.model_log.emit("Starting local model preparation.")

        def prepare() -> None:
            monitor_stop = threading.Event()
            try:
                def log(message: str) -> None:
                    self.bridge.model_log.emit(message)
                    self.bridge.status.emit(SourceKind.STATUS.value, message)

                # Keep this installed after preparation: restoring pythonw's
                # original None stream would break later tqdm/log writes.
                sys.stderr = WindowedTextStream(log)
                log("Inspecting the selected quality mode and local model cache…")
                monitor = threading.Thread(
                    target=self._monitor_model_preparation,
                    args=(monitor_stop, log),
                    name="model-progress-monitor",
                    daemon=True,
                )
                monitor.start()
                self.controller.prepare(log)
                report = self.report
                if self.controller.engine.finalizer is None:
                    reason = self.controller.engine.finalizer_error or "Multilingual finalizer could not be initialized"
                    downgraded_quality = QualityMode.BALANCED if report.quality_mode == QualityMode.BEST else report.quality_mode
                    moonshine_name = (
                        "Moonshine Small Streaming"
                        if downgraded_quality == QualityMode.EFFICIENT else "Moonshine Medium Streaming"
                    )
                    report = replace(
                        report,
                        quality_mode=downgraded_quality,
                        model_name=moonshine_name,
                        estimated_memory_mb=700 if downgraded_quality == QualityMode.EFFICIENT else 1100,
                        gpu_refinement=False,
                        downgrade_reasons=[*report.downgrade_reasons, reason],
                    )
                    self.controller.model_name = report.model_name
                if not report.verified:
                    log("Calibration: running five seconds of real speech through two lanes…")
                    report = self.capability_service.verify(report, self.controller.engine.calibrate)
                    log(f"Calibration complete: real-time factor {report.real_time_factor:g}.")
                else:
                    log("Calibration: reusing the verified hardware/model result from cache.")
                log("Model preparation complete. DaListener is ready.")
                self.bridge.model_ready.emit(report)
            except Exception as exc:
                self.bridge.model_error.emit(str(exc))
            finally:
                monitor_stop.set()

        threading.Thread(target=prepare, name="model-prepare", daemon=True).start()

    def _monitor_model_preparation(self, stop_event: threading.Event, log) -> None:
        """Report genuine cache growth and a heartbeat during long native loads."""
        roots = [self.controller.engine.model_dir]
        hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        whisper_cache = (
            "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
            if self.controller.engine.finalizer_model_name == "large-v3-turbo"
            else f"models--Systran--faster-whisper-{self.controller.engine.finalizer_model_name}"
        )
        roots.append(hf_home / "hub" / whisper_cache)
        baseline: dict[Path, int] = {}
        started = time.monotonic()

        def sizes() -> dict[Path, int]:
            result: dict[Path, int] = {}
            for root in roots:
                if not root.exists():
                    continue
                try:
                    for path in root.rglob("*"):
                        if path.is_file():
                            result[path] = path.stat().st_size
                except OSError:
                    continue
            return result

        baseline = sizes()
        while not stop_event.wait(2.0):
            current = sizes()
            active = [
                (path, size) for path, size in current.items()
                if path.suffix in (".partial", ".incomplete")
            ]
            downloaded = sum(max(0, size - baseline.get(path, 0)) for path, size in current.items())
            elapsed = int(time.monotonic() - started)
            if active:
                name, size = max(active, key=lambda item: item[1])
                log(f"Downloading {name.name}: {size / 1024**2:.1f} MB received ({elapsed}s elapsed)…")
            elif downloaded > 0:
                log(f"Model files received this run: {downloaded / 1024**2:.1f} MB ({elapsed}s elapsed)…")
            else:
                log(f"Still preparing the local model ({elapsed}s elapsed)…")

    def _show_capability(self) -> None:
        report = self.report
        self.rating_label.setText(report.rating.value.replace("-", " ").title())
        self.rating_label.setStyleSheet("color: #3fb950" if report.rating.value == "fast" else "color: #d29922")
        self.capability_label.setText(
            f"Draft text: {report.draft_delay_seconds[0]:g}–{report.draft_delay_seconds[1]:g}s behind\n"
            f"Final text: {report.final_delay_seconds[0]:g}–{report.final_delay_seconds[1]:g}s after a pause\n"
            f"Quality: {report.quality_mode.value.title()} · Local processing"
        )
        verification = "Verified by local calibration" if report.verified else "Provisional hardware estimate"
        gpu = report.gpu_name or "No discrete GPU detected"
        self.details_label.setText(
            f"{verification} · ~{report.estimated_memory_mb} MB memory\n"
            f"{report.cpu_name} · {report.physical_cores} cores · {report.total_ram_gb:g} GB RAM\n{gpu}"
            + ("\n" + " · ".join(report.downgrade_reasons) if report.downgrade_reasons else "")
        )

    def _refresh_devices(self) -> None:
        try:
            microphones = self.device_service.list_microphones()
            outputs = self.device_service.list_outputs()
            self.mic_names = {device.name: device.id for device in microphones}
            self.output_names = {device.name: device.id for device in outputs}
            self.mic_combo.clear()
            self.mic_combo.addItems(["Default microphone", *self.mic_names])
            self.output_combo.clear()
            self.output_combo.addItems(["Default output", *self.output_names])
            self.mic_combo.setCurrentText(self.settings.get("microphone_name", "Default microphone"))
            self.output_combo.setCurrentText(self.settings.get("output_name", "Default output"))
            self._update_source_states()
        except Exception as exc:
            self.status_label.setText(f"Could not enumerate audio devices: {exc}")

    def _update_source_states(self) -> None:
        mode = CaptureMode(self.mode_combo.currentText())
        self.mic_combo.setEnabled(mode in (CaptureMode.MICROPHONE, CaptureMode.BOTH))
        self.output_combo.setEnabled(mode in (CaptureMode.SYSTEM, CaptureMode.BOTH))

    def _selection(self) -> CaptureSelection:
        mic_default = self.mic_combo.currentText() == "Default microphone"
        output_default = self.output_combo.currentText() == "Default output"
        return CaptureSelection(
            mode=CaptureMode(self.mode_combo.currentText()),
            microphone_id=None if mic_default else self.mic_names.get(self.mic_combo.currentText()),
            output_id=None if output_default else self.output_names.get(self.output_combo.currentText()),
            follow_default_microphone=mic_default, follow_default_output=output_default,
            language=self.language_labels[self.language_combo.currentText()],
        )

    def toggle_session(self) -> None:
        if self.controller.session_id:
            self.stop_session()
            return
        try:
            selection = self._selection()
            self.events = {SourceKind.MICROPHONE: {}, SourceKind.SYSTEM: {}}
            self._render_all()
            self.last_session_id = self.controller.start(selection)
            self.started_at = time.monotonic()
            self.start_button.setText("Stop")
            self.pause_button.setEnabled(True)
            self.bookmark_button.setEnabled(True)
            self.live_label.setText("● Listening")
            self.live_label.setStyleSheet("color: #3fb950")
            self.settings.update({"capture_mode": selection.mode.value,
                                  "microphone_name": self.mic_combo.currentText(),
                                  "output_name": self.output_combo.currentText(),
                                  "language": selection.language.value})
            self._save_settings()
        except Exception as exc:
            QMessageBox.critical(self, "Could not start listening", str(exc))

    def test_sources(self) -> None:
        if self.controller.session_id or self.test_capture:
            return
        try:
            self.test_capture = CaptureManager()

            def frame(frame) -> None:
                import numpy as np
                rms = float(np.sqrt(np.mean(np.square(frame.samples)))) if len(frame.samples) else 0.0
                self.bridge.level.emit(frame.source_id.value, min(1.0, rms * 8.0))

            self.test_capture.start(
                self._selection(), frame,
                lambda source, text: self.bridge.status.emit(source.value, text),
            )
            self.test_button.setEnabled(False)
            self.test_button.setText("Testing…")
            QTimer.singleShot(3000, self._finish_source_test)
        except Exception as exc:
            self.test_capture = None
            QMessageBox.critical(self, "Audio test failed", str(exc))

    def _finish_source_test(self) -> None:
        if self.test_capture:
            self.test_capture.stop()
            self.test_capture = None
        self.test_button.setEnabled(True)
        self.test_button.setText("Test selected sources for 3 seconds")
        self.status_label.setText("Audio source test complete")

    def stop_session(self) -> None:
        self.last_session_id = self.controller.stop() or self.last_session_id
        self.start_button.setText("Start listening")
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Pause")
        self.bookmark_button.setEnabled(False)
        self.live_label.setText("● Ready")
        self.live_label.setStyleSheet("color: #8b949e")
        transcript_dir = self.data_dir / "Transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / f"DaListener-{datetime.now():%Y-%m-%d-%H%M%S-%f}.txt"
        if self.last_session_id:
            self.exporter.export(self.last_session_id, transcript_path)
        self._show_save_location(transcript_path, "Timestamped transcript saved")
        self.mic_level.setValue(0)
        self.system_level.setValue(0)
        self._refresh_history()

    def pause(self) -> None:
        paused = self.controller.pause()
        self.pause_button.setText("Resume" if paused else "Pause")
        self.live_label.setText("● Paused" if paused else "● Listening")

    def bookmark(self) -> None:
        self.controller.bookmark()
        self.status_label.setText("Bookmark added")

    def copy_last(self) -> None:
        candidates = [event for lane in self.events.values() for event in lane.values() if event.text]
        if candidates:
            QApplication.clipboard().setText(max(candidates, key=lambda e: (e.end_ms, e.revision)).text)
            self.status_label.setText("Last utterance copied")

    def export(self) -> None:
        session_id = self.controller.session_id or self.last_session_id
        if not session_id:
            QMessageBox.information(self, "Nothing to export", "Start or open a transcription session first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export transcript", "transcript.md",
            "Markdown (*.md);;Text (*.txt);;JSON (*.json);;SubRip (*.srt);;WebVTT (*.vtt)",
        )
        if path:
            self.exporter.export(session_id, Path(path))
            self._show_save_location(Path(path), f"Exported {Path(path).name}")

    def _show_save_location(self, path: Path, message: str) -> None:
        resolved = path.resolve()
        self.last_saved_folder = resolved if resolved.is_dir() else resolved.parent
        self.save_notice_label.setText(f"{message}: {resolved}")
        self.save_notice_label.setToolTip(str(resolved))
        self.status_label.setText(message)
        self.save_notice.show()

    def open_saved_folder(self) -> None:
        folder = self.last_saved_folder
        if not folder.exists():
            QMessageBox.warning(self, "Folder unavailable", f"The folder no longer exists:\n{folder}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
            QMessageBox.warning(self, "Could not open folder", str(folder))

    def _on_transcript(self, event: TranscriptEvent) -> None:
        visible_session = self.controller.session_id or self.last_session_id
        if event.session_id != visible_session:
            return
        if event.source_id == SourceKind.STATUS:
            self.status_label.setText(event.text)
            return
        self.events[event.source_id][event.utterance_id] = event
        if event.stability == Stability.FINAL and event.detected_language:
            name = "Tagalog" if event.detected_language == "tl" else "English"
            confidence = f" ({event.language_probability:.0%})" if event.language_probability is not None else ""
            self.status_label.setText(f"{event.source_id.value.title()} finalized as {name}{confidence}")
        self._render_lane(event.source_id)

    def _on_status(self, source: str, text: str) -> None:
        prefix = "" if source == SourceKind.STATUS.value else f"{source.title()}: "
        self.status_label.setText(prefix + text)

    def _on_level(self, source: str, value: float) -> None:
        bar = self.mic_level if source == SourceKind.MICROPHONE.value else self.system_level
        bar.setValue(int(value * 1000))

    def _on_model_ready(self, report) -> None:
        self.report = report
        self.capability_service.save(report)
        self._show_capability()
        self.start_button.setText("Start listening")
        self.start_button.setEnabled(True)
        try:
            self.start_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.start_button.clicked.connect(self.toggle_session)
        self.status_label.setText("Local model ready")

    def _on_model_error(self, message: str) -> None:
        self._append_model_log(f"ERROR: {message}")
        self.start_button.setText("Retry model")
        self.start_button.setEnabled(True)
        try:
            self.start_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.start_button.clicked.connect(self._prepare_model_async)
        self.status_label.setText("Model preparation failed")
        QMessageBox.critical(self, "Local model unavailable", message)

    def _append_model_log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.model_log.append(f"[{timestamp}] {message}")
        self.model_log.moveCursor(QTextCursor.End)

    def _render_lane(self, source: SourceKind) -> None:
        widget = self.mic_text if source == SourceKind.MICROPHONE else self.system_text
        needle = self.search.text().lower().strip()
        widget.clear()
        for event in sorted(self.events[source].values(), key=lambda e: (e.start_ms, e.utterance_id)):
            seconds = max(0, event.start_ms) // 1000
            language = f" · {event.detected_language.upper()}" if event.detected_language else ""
            text = f"[{seconds // 60:02}:{seconds % 60:02}{language}] {event.text}\n\n"
            cursor = widget.textCursor()
            cursor.movePosition(QTextCursor.End)
            fmt = QTextCharFormat()
            fmt.setForeground(QColor("#8b949e" if event.stability == Stability.DRAFT else "#e6edf3"))
            cursor.insertText(text, fmt)
        if needle:
            cursor = widget.document().find(needle)
            while not cursor.isNull():
                fmt = QTextCharFormat()
                fmt.setBackground(QColor("#5c4200"))
                cursor.mergeCharFormat(fmt)
                cursor = widget.document().find(needle, cursor)
        widget.moveCursor(QTextCursor.End)

    def _render_all(self) -> None:
        self._render_lane(SourceKind.MICROPHONE)
        self._render_lane(SourceKind.SYSTEM)

    def _refresh_history(self) -> None:
        rows = self.store.list_sessions()
        self.history.setRowCount(len(rows))
        for index, row in enumerate(rows):
            started = QTableWidgetItem(row["started_at"].replace("T", " ")[:19])
            started.setData(Qt.UserRole, row["id"])
            self.history.setItem(index, 0, started)
            self.history.setItem(index, 1, QTableWidgetItem(row["model_name"]))
            self.history.setItem(index, 2, QTableWidgetItem(str(row["event_count"])))

    def _load_history(self, row: int, _column: int) -> None:
        session_id = self.history.item(row, 0).data(Qt.UserRole)
        self.last_session_id = session_id
        self.events = {SourceKind.MICROPHONE: {}, SourceKind.SYSTEM: {}}
        for record in self.store.events(session_id):
            source = SourceKind(record["source_id"])
            if source in self.events:
                self.events[source][record["utterance_id"]] = TranscriptEvent(
                    session_id=session_id, source_id=source, utterance_id=record["utterance_id"],
                    text=record["text"], start_ms=record["start_ms"], end_ms=record["end_ms"],
                    revision=record["revision"], stability=Stability(record["stability"]),
                    detected_language=record["detected_language"],
                    language_probability=record["language_probability"],
                )
        self._render_all()
        self.status_label.setText("Historical session loaded")

    def _tick(self) -> None:
        if self.controller.session_id:
            elapsed = int(time.monotonic() - self.started_at)
            self.timer_label.setText(f"{elapsed // 3600:02}:{(elapsed % 3600) // 60:02}:{elapsed % 60:02}")

    def _load_settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.test_capture:
            self.test_capture.stop()
        self.controller.close()
        self.store.close()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("DaListener")
    window = DaListenerWindow()
    window.show()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
