"""Main application entry point for Turbo Whisper."""

import enum
import os
import subprocess
import sys
import tempfile
import threading
import time

# Platform-specific imports for single-instance locking
if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .api import WhisperAPIError, WhisperClient
from .config import Config
from .hotkey import create_hotkey_manager
from .icons import (
    get_check_icon,
    get_chevron_down_icon,
    get_chevron_up_icon,
    get_close_icon,
    get_copy_icon,
    get_eye_icon,
    get_eye_off_icon,
    get_play_icon,
    get_stop_icon,
    get_tray_icon,
)
from .recorder import AudioRecorder
from .typer import Typer
from .waveform import WaveformWidget


class AppState(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    RESULT = "result"
    TYPING = "typing"


class SignalBridge(QObject):
    """Bridge for thread-safe Qt signals."""

    toggle_recording = pyqtSignal()
    update_waveform = pyqtSignal(float, list)
    transcription_complete = pyqtSignal(str)
    transcription_error = pyqtSignal(str)
    transcription_chunk = pyqtSignal(str)
    show_status = pyqtSignal(str)
    state_changed = pyqtSignal(str)   # AppState.value string — updates UI only
    set_app_state = pyqtSignal(str)   # AppState.value string — full _set_state()
    show_window = pyqtSignal()        # safe show() from background thread
    hide_window = pyqtSignal()        # safe hide() from background thread
    tray_message = pyqtSignal(str, str, int)  # title, message, duration_ms


class TickMarksWidget(QWidget):
    """Widget that draws tick mark notches for a slider."""

    def __init__(self, num_ticks: int = 11, parent=None):
        super().__init__(parent)
        self.num_ticks = num_ticks  # 0%, 20%, 40%... 200% = 11 ticks
        self.setFixedHeight(6)

    def paintEvent(self, event):
        from PyQt6.QtGui import QColor, QPainter, QPen

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pen = QPen(QColor("#666"))
        pen.setWidth(1)
        painter.setPen(pen)

        width = self.width()
        # Account for slider handle padding (roughly 8px on each side)
        padding = 8
        usable_width = width - 2 * padding

        for i in range(self.num_ticks):
            x = padding + int(i * usable_width / (self.num_ticks - 1))
            # Draw shorter tick for non-100% marks, taller for 100% (middle)
            if i == 5:  # 100% mark (middle)
                painter.drawLine(x, 0, x, 5)
            else:
                painter.drawLine(x, 2, x, 5)

        painter.end()


class RecordingWindow(QWidget):
    """Floating window showing waveform during recording."""

    # Signal emitted when ESC is pressed to cancel
    cancel_requested = pyqtSignal()

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self._drag_pos = None  # For dragging support
        self._setup_ui()

        # Timer to refresh Claude status while settings panel is open
        self._claude_status_timer = QTimer()
        self._claude_status_timer.timeout.connect(self._update_claude_status)
        self._claude_status_timer.setInterval(1000)  # Update every second

    def _setup_ui(self) -> None:
        """Set up the recording window UI."""
        # Set window icon for taskbar (orange = idle)
        self.setWindowIcon(get_tray_icon(128, recording=False))

        # Frameless, always on top, floating window that doesn't steal focus.
        # On macOS: Tool type lowers window priority below active apps, so we skip it.
        # WindowStaysOnTopHint works correctly without Tool on macOS.
        if sys.platform == "darwin":
            self._base_window_flags = (
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
            )
        else:
            self._base_window_flags = (
                Qt.WindowType.FramelessWindowHint
                | Qt.WindowType.WindowStaysOnTopHint
                | Qt.WindowType.Tool
            )
        self.setWindowFlags(self._base_window_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Allow resize via mouse
        self._resize_edge = None

        # Main container with rounded corners and purple gradient
        container = QWidget(self)
        container.setObjectName("container")
        container.setStyleSheet(
            """
            #container {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #2d1b4e,
                    stop:0.5 #1a1033,
                    stop:1 #0f0a1a
                );
                border-radius: 12px;
                border: 1px solid #4a3070;
            }
        """
        )

        # Use a stacked layout - waveform behind, controls on top
        from PyQt6.QtWidgets import QFrame

        # Container layout
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Create a frame for the main content
        content_frame = QFrame()
        content_frame.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content_frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        container_layout.addWidget(content_frame)

        # Waveform - use the bright KnowAll lime green (#84cc16)
        self.waveform = WaveformWidget(
            color="#84cc16",  # Same bright green as buttons
            bg_color=self.config.background_color,
        )
        self.waveform.setMinimumHeight(160)  # Bigger orb
        layout.addWidget(self.waveform, stretch=2)  # Give it more priority

        # Status row - transparent background so orb shows through
        status_widget = QWidget()
        status_widget.setStyleSheet("background: transparent;")
        status_layout = QHBoxLayout(status_widget)
        status_layout.setContentsMargins(4, 0, 4, 0)

        self.status_label = QLabel("Listening...")
        self.status_label.setStyleSheet(
            """
            color: #888;
            font-size: 11px;
        """
        )
        status_layout.addWidget(self.status_label)

        status_layout.addStretch()

        # Hint label - show configured hotkey
        self._hotkey_str = "+".join(k.title() for k in self.config.hotkey)
        self.hints_label = QLabel(f"Start: {self._hotkey_str}")
        self.hints_label.setStyleSheet(
            """
            color: #666;
            font-size: 10px;
        """
        )
        status_layout.addWidget(self.hints_label)

        # Animated status timer
        self._status_dots = 0
        self._status_timer = QTimer()
        self._status_timer.timeout.connect(self._animate_status)
        self._status_timer.setInterval(400)

        layout.addWidget(status_widget)

        # Streaming transcript preview — shown during processing and result
        self.transcript_preview = QLabel("")
        self.transcript_preview.setWordWrap(True)
        self.transcript_preview.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.transcript_preview.setStyleSheet(
            """
            color: #e2e8f0;
            font-size: 12px;
            background: rgba(132, 204, 22, 0.07);
            border: 1px solid rgba(132, 204, 22, 0.2);
            border-radius: 6px;
            padding: 6px 8px;
            """
        )
        self.transcript_preview.setMinimumHeight(40)
        self.transcript_preview.setMaximumHeight(120)
        self.transcript_preview.hide()
        layout.addWidget(self.transcript_preview)

        # Control buttons panel — always visible, changes per state
        _btn_style_primary = """
            QPushButton {
                background: rgba(132, 204, 22, 0.15);
                border: 1px solid rgba(132, 204, 22, 0.5);
                border-radius: 6px;
                color: #84cc16;
                font-size: 12px;
                font-weight: bold;
                padding: 4px 12px;
            }
            QPushButton:hover { background: rgba(132, 204, 22, 0.3); }
            QPushButton:disabled { opacity: 0.3; color: #555; border-color: #555; background: transparent; }
        """
        _btn_style_secondary = """
            QPushButton {
                background: rgba(255,255,255,0.06);
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 6px;
                color: #888;
                font-size: 12px;
                padding: 4px 12px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.12); color: #ccc; }
            QPushButton:disabled { opacity: 0.3; }
        """
        _btn_style_danger = """
            QPushButton {
                background: rgba(239,68,68,0.1);
                border: 1px solid rgba(239,68,68,0.35);
                border-radius: 6px;
                color: #f87171;
                font-size: 12px;
                padding: 4px 12px;
            }
            QPushButton:hover { background: rgba(239,68,68,0.22); }
            QPushButton:disabled { opacity: 0.3; }
        """

        self.controls_widget = QWidget()
        self.controls_widget.setStyleSheet("background: transparent;")
        controls_layout = QHBoxLayout(self.controls_widget)
        controls_layout.setContentsMargins(0, 2, 0, 2)
        controls_layout.setSpacing(6)

        self.btn_primary = QPushButton("▶ Start")
        self.btn_primary.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_primary.setStyleSheet(_btn_style_primary)
        self.btn_primary.setFixedHeight(28)
        controls_layout.addWidget(self.btn_primary)

        self.btn_secondary = QPushButton("✕ Cancel")
        self.btn_secondary.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_secondary.setStyleSheet(_btn_style_secondary)
        self.btn_secondary.setFixedHeight(28)
        self.btn_secondary.hide()
        controls_layout.addWidget(self.btn_secondary)

        self.btn_repeat = QPushButton("↩ Repeat")
        self.btn_repeat.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_repeat.setStyleSheet(_btn_style_secondary)
        self.btn_repeat.setFixedHeight(28)
        self.btn_repeat.setToolTip("Re-paste last transcription into focused window")
        self.btn_repeat.hide()
        controls_layout.addWidget(self.btn_repeat)

        layout.addWidget(self.controls_widget)

        # More toggle button - chevron icon
        self.settings_btn = QPushButton()
        self.settings_btn.setIcon(get_chevron_down_icon(20, "#84cc16"))
        self.settings_btn.setFixedSize(40, 28)
        self.settings_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Prevent SPACE triggering
        self.settings_btn.setStyleSheet(
            """
            QPushButton {
                background: rgba(132, 204, 22, 0.1);
                border: 1px solid rgba(132, 204, 22, 0.3);
                border-radius: 6px;
            }
            QPushButton:hover {
                background: rgba(132, 204, 22, 0.2);
            }
        """
        )
        self.settings_btn.clicked.connect(self._toggle_settings)
        layout.addWidget(self.settings_btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # Collapsible settings panel
        self.settings_panel = QWidget()
        self.settings_panel.setStyleSheet(
            """
            QWidget {
                background-color: rgba(0, 0, 0, 0.3);
                border-radius: 8px;
            }
            QLabel {
                color: #888;
                font-size: 10px;
            }
            QLineEdit {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid #4a3070;
                border-radius: 4px;
                color: #fff;
                padding: 6px;
                font-size: 11px;
            }
            QSlider::groove:horizontal {
                background: #333;
                height: 6px;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #84cc16;
                width: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
        """
        )
        settings_layout = QVBoxLayout(self.settings_panel)
        settings_layout.setContentsMargins(12, 8, 12, 8)
        settings_layout.setSpacing(8)

        # API URL
        url_label = QLabel("API URL")
        url_row = QHBoxLayout()
        self.api_url_input = QLineEdit(self.config.api_url)
        self.api_url_input.setPlaceholderText("https://api.openai.com/v1/audio/transcriptions")
        self.url_copy_btn = QPushButton()
        self.url_copy_btn.setIcon(get_copy_icon(16, "#888888"))
        self.url_copy_btn.setFixedSize(28, 28)
        self.url_copy_btn.setToolTip("Copy to clipboard")
        self.url_copy_btn.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(132, 204, 22, 0.2);
                border-color: rgba(132, 204, 22, 0.3);
            }
        """
        )
        self.url_copy_btn.clicked.connect(
            lambda: self._copy_to_clipboard(self.api_url_input.text(), self.url_copy_btn)
        )
        url_row.addWidget(self.api_url_input)
        url_row.addWidget(self.url_copy_btn)
        settings_layout.addWidget(url_label)
        settings_layout.addLayout(url_row)

        # API Key - store actual value separately and display asterisks
        key_label = QLabel("API Key")
        key_row = QHBoxLayout()
        self._actual_api_key = self.config.api_key
        self.api_key_input = QLineEdit()
        self._key_visible = False
        self._update_api_key_display()
        self.api_key_input.setPlaceholderText("sk-...")
        self.api_key_input.textChanged.connect(self._on_api_key_changed)
        # Style to ensure asterisks show clearly
        self.api_key_input.setStyleSheet(
            """
            QLineEdit {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid #4a3070;
                border-radius: 4px;
                color: #fff;
                padding: 6px;
                font-size: 12px;
                font-family: monospace;
            }
        """
        )
        # Eye icon button for show/hide
        self.key_visible_btn = QPushButton()
        self.key_visible_btn.setIcon(get_eye_icon(16, "#888888"))
        self.key_visible_btn.setFixedSize(28, 28)
        self.key_visible_btn.setToolTip("Show/hide API key")
        self.key_visible_btn.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(132, 204, 22, 0.2);
                border-color: rgba(132, 204, 22, 0.3);
            }
        """
        )
        self.key_visible_btn.clicked.connect(self._toggle_key_visibility)
        # Copy icon button
        self.key_copy_btn = QPushButton()
        self.key_copy_btn.setIcon(get_copy_icon(16, "#888888"))
        self.key_copy_btn.setFixedSize(28, 28)
        self.key_copy_btn.setToolTip("Copy to clipboard")
        self.key_copy_btn.setStyleSheet(
            """
            QPushButton {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(132, 204, 22, 0.2);
                border-color: rgba(132, 204, 22, 0.3);
            }
        """
        )
        self.key_copy_btn.clicked.connect(
            lambda: self._copy_to_clipboard(self._actual_api_key, self.key_copy_btn)
        )
        key_row.addWidget(self.api_key_input)
        key_row.addWidget(self.key_visible_btn)
        key_row.addWidget(self.key_copy_btn)
        settings_layout.addWidget(key_label)
        settings_layout.addLayout(key_row)

        # Microphone selection
        mic_label = QLabel("Microphone")
        self.mic_combo = QComboBox()
        self.mic_combo.setStyleSheet(
            """
            QComboBox {
                background-color: rgba(255, 255, 255, 0.1);
                border: 1px solid #4a3070;
                border-radius: 4px;
                color: #fff;
                padding: 6px;
                font-size: 11px;
            }
            QComboBox::drop-down {
                border: none;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 5px solid #888;
                margin-right: 8px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1033;
                border: 1px solid #4a3070;
                color: #fff;
                selection-background-color: rgba(132, 204, 22, 0.3);
            }
        """
        )
        self._populate_mic_dropdown()
        settings_layout.addWidget(mic_label)
        settings_layout.addWidget(self.mic_combo)

        # Gain slider with dynamic level display in groove
        # 0-200% range, with 100% (1.0x) in the middle
        gain_row = QHBoxLayout()
        self.gain_label = QLabel("Mic Gain:")
        self.gain_value_label = QLabel("100%")
        self.gain_value_label.setStyleSheet("color: #84cc16; font-weight: bold;")
        gain_row.addWidget(self.gain_label)
        gain_row.addStretch()
        gain_row.addWidget(self.gain_value_label)
        self.sensitivity_slider = QSlider(Qt.Orientation.Horizontal)
        self.sensitivity_slider.setRange(0, 200)
        self.sensitivity_slider.setValue(100)  # 100% = no gain adjustment
        self.sensitivity_slider.setSingleStep(20)  # Arrow keys move by 20%
        self.sensitivity_slider.setPageStep(20)  # Page up/down move by 20%
        self.sensitivity_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.sensitivity_slider.setTickInterval(20)  # Tick every 20% (20 units = 20%)
        self.sensitivity_slider.valueChanged.connect(self._on_sensitivity_changed)
        self._current_mic_level = 0  # Track current level for styling
        self._update_sensitivity_style()
        settings_layout.addLayout(gain_row)
        settings_layout.addWidget(self.sensitivity_slider)

        # Tick marks below slider (visual notches at 20% intervals)
        tick_marks = TickMarksWidget(num_ticks=11)  # 0%, 20%, 40%... 200%
        settings_layout.addWidget(tick_marks)

        # History section
        history_label = QLabel("Recent Clips")
        settings_layout.addWidget(history_label)

        self.history_list = QListWidget()
        self.history_list.setMinimumHeight(200)
        self.history_list.setMaximumHeight(320)  # ~10 items at 32px each
        self.history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.history_list.setStyleSheet(
            """
            QListWidget {
                background-color: rgba(255, 255, 255, 0.05);
                border: 1px solid #4a3070;
                border-radius: 4px;
                color: #ccc;
                font-size: 11px;
            }
            QListWidget::item {
                padding: 2px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            }
            QListWidget::item:hover {
                background-color: rgba(132, 204, 22, 0.1);
            }
        """
        )
        self._refresh_history()
        settings_layout.addWidget(self.history_list)

        # Claude integration status
        claude_row = QHBoxLayout()
        claude_row.setSpacing(8)
        claude_label = QLabel("Claude Code")
        claude_label.setStyleSheet("color: #888; font-size: 11px;")
        self.claude_status = QLabel()
        self.claude_status.setStyleSheet("font-size: 11px;")
        self._update_claude_status()
        claude_row.addWidget(claude_label)
        claude_row.addWidget(self.claude_status)
        claude_row.addStretch()
        settings_layout.addLayout(claude_row)

        # Save button - at the bottom, vibrant green
        self.save_btn = QPushButton("Save Settings")
        self.save_btn.setStyleSheet(
            """
            QPushButton {
                background-color: #84cc16;
                color: #000;
                border: none;
                border-radius: 4px;
                font-size: 11px;
                font-weight: bold;
                padding: 8px 16px;
            }
            QPushButton:hover {
                background-color: #9ae62a;
            }
        """
        )
        self.save_btn.clicked.connect(self._save_settings)
        settings_layout.addWidget(self.save_btn)

        self.settings_panel.hide()  # Hidden by default
        layout.addWidget(self.settings_panel)

        # Main layout
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(container)

        # Close button - overlaid in top-right corner (not in layout)
        self.close_btn = QPushButton(container)
        self.close_btn.setIcon(get_close_icon(14, "#666666"))
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setToolTip("Close")
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # Prevent SPACE triggering
        self.close_btn.setStyleSheet(
            """
            QPushButton {
                background: transparent;
                border: none;
            }
        """
        )
        self.close_btn.clicked.connect(self._close_window)
        # Hover behavior - change icon to green instead of background
        self.close_btn.enterEvent = lambda e: self.close_btn.setIcon(get_close_icon(14, "#84cc16"))
        self.close_btn.leaveEvent = lambda e: self.close_btn.setIcon(get_close_icon(14, "#666666"))
        self.close_btn.move(self.config.window_width - 28, 8)  # Top-right corner
        self.close_btn.raise_()  # Bring to front

        # Version label - overlaid in top-left corner (not in layout)
        self.version_label = QLabel("v1.0.0", container)
        self.version_label.setStyleSheet(
            """
            color: #666;
            font-size: 10px;
        """
        )
        self.version_label.move(12, 8)

        # Size
        self.setFixedSize(self.config.window_width, self.config.window_height)

    def update_icon(self, recording: bool) -> None:
        """Update window icon based on recording state."""
        self.setWindowIcon(get_tray_icon(128, recording=recording))

    def keyPressEvent(self, event) -> None:
        """Handle key presses - ESC cancels recording."""
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_requested.emit()
        else:
            super().keyPressEvent(event)

    def set_status(self, text: str, animate: bool = False) -> None:
        """Update status label."""
        self._base_status = text
        self._status_dots = 0
        self.status_label.setText(text)
        if animate:
            self._status_timer.start()
        else:
            self._status_timer.stop()

    def set_recording_hint(self, recording: bool) -> None:
        """Update hint text based on recording state."""
        action = "Stop" if recording else "Start"
        self.hints_label.setText(f"{action}: {self._hotkey_str}")

    def update_mic_level(self, level: float) -> None:
        """Update the mic level display in sensitivity slider (0.0 to 1.0 scale)."""
        # Only update if level changed significantly (reduces stylesheet updates)
        if abs(level - self._current_mic_level) > 0.01 or level == 0:
            self._current_mic_level = level
            self._update_sensitivity_style()


    def _animate_status(self) -> None:
        """Animate the status text with dots."""
        self._status_dots = (self._status_dots + 1) % 4
        dots = "." * self._status_dots
        self.status_label.setText(f"{self._base_status}{dots}")

    def _toggle_settings(self) -> None:
        """Toggle settings panel visibility."""
        if self.settings_panel.isVisible():
            self.settings_panel.hide()
            self.settings_btn.setIcon(get_chevron_down_icon(20, "#84cc16"))
            self.setFixedSize(self.config.window_width, self.config.window_height)
            self._claude_status_timer.stop()
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        else:
            self.settings_panel.show()
            self.settings_btn.setIcon(get_chevron_up_icon(20, "#84cc16"))
            self.setFixedSize(self.config.window_width, self.config.window_height + 520)
            self._update_claude_status()
            self._claude_status_timer.start()
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.activateWindow()

    def _update_api_key_display(self) -> None:
        """Update the API key display based on visibility."""
        # Block signals to prevent textChanged from firing
        self.api_key_input.blockSignals(True)
        if self._key_visible:
            self.api_key_input.setText(self._actual_api_key)
            self.api_key_input.setReadOnly(False)
        else:
            # Show asterisks for each character (use bullet character for better display)
            mask = "●" * len(self._actual_api_key) if self._actual_api_key else ""
            self.api_key_input.setText(mask)
            self.api_key_input.setReadOnly(True)  # Can't edit while hidden
        self.api_key_input.blockSignals(False)

    def _on_api_key_changed(self, text: str) -> None:
        """Handle API key text changes."""
        if self._key_visible:
            # If visible, update the actual key
            self._actual_api_key = text

    def _toggle_key_visibility(self) -> None:
        """Toggle API key visibility."""
        self._key_visible = not self._key_visible
        self._update_api_key_display()
        if self._key_visible:
            self.key_visible_btn.setIcon(get_eye_off_icon(16, "#888888"))
        else:
            self.key_visible_btn.setIcon(get_eye_icon(16, "#888888"))

    def _copy_to_clipboard(self, text: str, button: QPushButton = None) -> None:
        """Copy text to clipboard and show feedback on button."""
        clipboard = QApplication.clipboard()
        clipboard.setText(text)

        # Show "Copied" feedback on button if provided
        if button:
            original_icon = button.icon()
            button.setIcon(get_check_icon(16, "#84cc16"))
            QTimer.singleShot(1500, lambda: button.setIcon(original_icon))

    def _on_sensitivity_changed(self, value: int) -> None:
        """Handle gain slider change - update in real-time with 20% snapping."""
        # Snap to nearest 20% increment
        snapped = round(value / 20) * 20
        if snapped != value:
            self.sensitivity_slider.blockSignals(True)
            self.sensitivity_slider.setValue(snapped)
            self.sensitivity_slider.blockSignals(False)
            value = snapped

        self.waveform.sensitivity = value
        self.gain_value_label.setText(f"{value}%")
        self._update_sensitivity_style()

    def _update_sensitivity_style(self) -> None:
        """Update the gain slider groove to show current mic level after gain."""
        # Apply gain to the raw level for visualization
        gain = self.sensitivity_slider.value() / 100.0  # 0-2.0
        gained_level = min(1.0, self._current_mic_level * gain * 5)  # Scale for visibility
        level_pct = int(gained_level * 100)

        self.sensitivity_slider.setStyleSheet(
            f"""
            QSlider::groove:horizontal {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #84cc16,
                    stop:{level_pct / 100:.2f} #84cc16,
                    stop:{min(1.0, level_pct / 100 + 0.01):.2f} #333,
                    stop:1 #333
                );
                height: 8px;
                border-radius: 4px;
            }}
            QSlider::handle:horizontal {{
                background: #fff;
                width: 16px;
                height: 16px;
                margin: -5px 0;
                border-radius: 8px;
                border: 2px solid #84cc16;
            }}
            QSlider::sub-page:horizontal {{
                background: transparent;
            }}
            QSlider::add-page:horizontal {{
                background: transparent;
            }}
            QSlider {{
                height: 24px;
            }}
        """
        )

    def _populate_mic_dropdown(self) -> None:
        """Populate the microphone dropdown with available devices."""
        import sys

        from .recorder import get_pipewire_sources

        self.mic_combo.clear()
        self.mic_combo.addItem("System Default", None)

        # Get input devices - use PipeWire on Linux, PyAudio elsewhere
        if sys.platform.startswith("linux"):
            pw_sources = get_pipewire_sources()
            if pw_sources:
                for src in pw_sources:
                    idx = src["id"]  # PipeWire source ID
                    name = src["description"]
                    display = f"{name} (48000Hz)"
                    self.mic_combo.addItem(display, idx)
                return

        # Fallback to PyAudio device enumeration
        import pyaudio

        try:
            audio = pyaudio.PyAudio()
            for i in range(audio.get_device_count()):
                try:
                    info = audio.get_device_info_by_index(i)
                    if info["maxInputChannels"] > 0 and info["maxOutputChannels"] == 0:
                        name = info["name"]
                        rate = int(info["defaultSampleRate"])
                        self.mic_combo.addItem(f"{name} ({rate}Hz)", i)
                except Exception:
                    pass
            audio.terminate()
        except Exception as e:
            print(f"Could not enumerate audio devices: {e}")

        # Select the saved device
        if self.config.input_device_index is not None:
            for i in range(self.mic_combo.count()):
                if self.mic_combo.itemData(i) == self.config.input_device_index:
                    self.mic_combo.setCurrentIndex(i)
                    break

    def _save_settings(self) -> None:
        """Save settings to config."""
        self.config.api_url = self.api_url_input.text()
        self.config.api_key = self._actual_api_key  # Use the actual stored key
        # Save selected microphone
        self.config.input_device_index = self.mic_combo.currentData()
        self.config.input_device_name = self.mic_combo.currentText()
        self.config.save()
        # Brief confirmation
        self.save_btn.setText("✓ Saved!")
        QTimer.singleShot(1500, lambda: self.save_btn.setText("Save Settings"))

    def _update_claude_status(self) -> None:
        """Update the Claude integration status indicator."""
        if not self.config.claude_integration:
            self.claude_status.setText("Disabled")
            self.claude_status.setStyleSheet("color: #666; font-size: 11px;")
            return

        # Check integration server status - simple Ready/Busy display
        try:
            import json
            import urllib.request

            req = urllib.request.Request(
                f"http://127.0.0.1:{self.config.claude_integration_port}/status",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                data = json.loads(resp.read().decode())
                age = data.get("last_signal_age", 999)
                # Ready if signal within last 30 seconds (matches typing logic)
                if age < 30:
                    self.claude_status.setText("Ready")
                    self.claude_status.setStyleSheet("color: #84cc16; font-size: 11px;")
                else:
                    self.claude_status.setText("Busy")
                    self.claude_status.setStyleSheet("color: #f59e0b; font-size: 11px;")
        except Exception:
            self.claude_status.setText("Server error")
            self.claude_status.setStyleSheet("color: #f59e0b; font-size: 11px;")

    def _make_history_widget(self, idx: int, entry) -> QWidget:
        """Build a single history list widget for the given entry."""
        _btn_icon_style = """
            QPushButton {
                background: transparent; border: none; border-radius: 4px;
            }
            QPushButton:hover { background: rgba(132, 204, 22, 0.2); }
        """

        if isinstance(entry, dict):
            text = entry.get("text", "")
            timestamp = entry.get("timestamp", "")
            audio_file = entry.get("audio_file") or ""
            status = entry.get("status", "ok")
        else:
            text = str(entry)
            timestamp = ""
            audio_file = ""
            status = "ok"

        # Format timestamp
        time_str = ""
        if timestamp:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(timestamp)
                time_str = dt.strftime("%b %d %H:%M") + " "
            except ValueError:
                pass

        widget = QWidget()
        widget.setStyleSheet("background: transparent;")
        row = QHBoxLayout(widget)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(4)

        # Label — colour and text depend on status
        if status == "recording":
            label = QLabel(f"🎙️ {time_str}Recording...")
            label.setStyleSheet("color: #84cc16; font-size: 11px;")
            label.setToolTip("Recording in progress")
        elif status == "transcribing":
            label = QLabel(f"⏳ {time_str}Transcribing...")
            label.setStyleSheet("color: #f59e0b; font-size: 11px;")
            label.setToolTip("Transcription in progress")
        elif status in ("failed", "empty"):
            icon = "❌" if status == "failed" else "🔇"
            hint = "API error" if status == "failed" else "No speech"
            label = QLabel(f"🎙️{icon} {time_str}{hint}")
            label.setStyleSheet("color: #f87171; font-size: 11px;")
            label.setToolTip("Click 🔄 to retry transcription")
        else:
            display = (text[:40] + "…") if len(text) > 40 else text
            label = QLabel(f"{time_str}{display}")
            label.setStyleSheet("color: #ccc; font-size: 11px;")
            label.setToolTip(text)

        row.addWidget(label, stretch=1)

        # Copy button — only for successful entries with text
        if status == "ok" and text:
            copy_btn = QPushButton()
            copy_btn.setIcon(get_copy_icon(14, "#888"))
            copy_btn.setFixedSize(24, 24)
            copy_btn.setToolTip("Copy to clipboard")
            copy_btn.setStyleSheet(_btn_icon_style)
            copy_btn.clicked.connect(lambda checked, t=text: self._copy_history_item(t))
            row.addWidget(copy_btn)

        # Play button — whenever there is an audio file
        if audio_file:
            play_btn = QPushButton()
            play_btn.setIcon(get_play_icon(14, "#888"))
            play_btn.setFixedSize(24, 24)
            play_btn.setToolTip("Play recording")
            play_btn.setStyleSheet(_btn_icon_style)
            play_btn.clicked.connect(
                lambda checked, f=audio_file, b=play_btn: self._play_audio(f, b)
            )
            row.addWidget(play_btn)

        # Retry button — any entry with audio (not just failed)
        if audio_file and status != "recording":
            retry_btn = QPushButton("🔄")
            retry_btn.setFixedSize(28, 24)
            retry_btn.setToolTip("Re-transcribe this recording")
            retry_btn.setStyleSheet(
                """
                QPushButton {
                    background: rgba(251,146,60,0.12);
                    border: 1px solid rgba(251,146,60,0.35);
                    border-radius: 4px;
                    color: #fb923c;
                    font-size: 12px;
                }
                QPushButton:hover { background: rgba(251,146,60,0.28); }
                """
            )
            retry_btn.clicked.connect(
                lambda checked, f=audio_file, i=idx: self._on_retry_clicked(f, i)
            )
            row.addWidget(retry_btn)

        return widget

    def _refresh_history(self) -> None:
        """Rebuild the full history list."""
        self.history_list.clear()
        for idx, entry in enumerate(self.config.history):
            widget = self._make_history_widget(idx, entry)
            item = QListWidgetItem()
            item.setSizeHint(widget.sizeHint())
            self.history_list.addItem(item)
            self.history_list.setItemWidget(item, widget)

    def _update_history_item(self, idx: int) -> None:
        """Update a single history row in-place (no full rebuild)."""
        if idx < 0 or idx >= self.history_list.count():
            return
        entry = self.config.history[idx] if idx < len(self.config.history) else None
        if entry is None:
            return
        widget = self._make_history_widget(idx, entry)
        item = self.history_list.item(idx)
        if item:
            item.setSizeHint(widget.sizeHint())
            self.history_list.setItemWidget(item, widget)


    def update_controls(self, state: str) -> None:
        """Update control buttons and status label based on app state."""
        hotkey = self._hotkey_str

        if state == "idle":
            self.btn_primary.setText("▶ Start")
            self.btn_primary.setEnabled(True)
            self.btn_secondary.hide()
            self.btn_repeat.hide()
            self.set_status("Ready", animate=False)
            self.hints_label.setText(f"Start: {hotkey}")

        elif state == "recording":
            self.btn_primary.setText("⏹ Stop")
            self.btn_primary.setEnabled(True)
            self.btn_secondary.setText("✕ Cancel")
            self.btn_secondary.show()
            self.btn_repeat.hide()
            self.set_status("Listening", animate=True)
            self.hints_label.setText(f"Stop: {hotkey}")

        elif state == "transcribing":
            self.btn_primary.setText("⏳ Processing...")
            self.btn_primary.setEnabled(False)
            self.btn_secondary.hide()
            self.btn_repeat.hide()
            self.set_status("Transcribing", animate=True)
            self.hints_label.setText("")

        elif state == "result":
            self.btn_primary.setText("▶ New")
            self.btn_primary.setEnabled(True)
            self.btn_secondary.hide()
            self.btn_repeat.show()
            self.set_status("Done", animate=False)
            self.hints_label.setText(f"New: {hotkey}")

        elif state == "typing":
            self.btn_primary.setText("✍ Typing...")
            self.btn_primary.setEnabled(False)
            self.btn_secondary.hide()
            self.btn_repeat.hide()
            self.set_status("Typing", animate=True)
            self.hints_label.setText("")

    def show_transcript_chunk(self, text: str) -> None:
        """Show streaming transcript text in the preview widget."""
        if not text:
            return
        # Truncate for display — keep last 200 chars so it fits in the widget
        display = text if len(text) <= 200 else "…" + text[-200:]
        self.transcript_preview.setText(display)
        self.transcript_preview.setStyleSheet(
            """
            color: #e2e8f0;
            font-size: 12px;
            background: rgba(132, 204, 22, 0.07);
            border: 1px solid rgba(132, 204, 22, 0.2);
            border-radius: 6px;
            padding: 6px 8px;
            """
        )
        if not self.transcript_preview.isVisible():
            self.transcript_preview.show()
            new_h = self.config.window_height + 140
            self.setFixedSize(self.config.window_width, new_h)

    def show_result(self, text: str) -> None:
        """Show final transcription result — keep window visible, style as done."""
        if not text:
            return
        display = text if len(text) <= 300 else text[:300] + "…"
        self.transcript_preview.setText(display)
        # Green border — done state
        self.transcript_preview.setStyleSheet(
            """
            color: #f0fdf4;
            font-size: 12px;
            background: rgba(132, 204, 22, 0.12);
            border: 1px solid rgba(132, 204, 22, 0.5);
            border-radius: 6px;
            padding: 6px 8px;
            """
        )
        if not self.transcript_preview.isVisible():
            self.transcript_preview.show()
            new_h = self.config.window_height + 140
            self.setFixedSize(self.config.window_width, new_h)

    def show_error_result(self, label: str) -> None:
        """Show failed transcription result — keep window visible, style as error."""
        self.transcript_preview.setText(label)
        self.transcript_preview.setStyleSheet(
            """
            color: #fca5a5;
            font-size: 12px;
            background: rgba(239, 68, 68, 0.08);
            border: 1px solid rgba(239, 68, 68, 0.35);
            border-radius: 6px;
            padding: 6px 8px;
            """
        )
        if not self.transcript_preview.isVisible():
            self.transcript_preview.show()
            new_h = self.config.window_height + 140
            self.setFixedSize(self.config.window_width, new_h)

    def clear_transcript_preview(self) -> None:
        """Hide and reset the transcript preview widget."""
        self.transcript_preview.hide()
        self.transcript_preview.setText("")
        self.setFixedSize(self.config.window_width, self.config.window_height)

    def set_retranscribe_callback(self, callback) -> None:
        """Set the callback for retranscribe button clicks."""
        self._retranscribe_callback = callback

    def _on_retry_clicked(self, audio_file: str, entry_index: int) -> None:
        """Handle retry button click — delegate to main app."""
        cb = getattr(self, "_retranscribe_callback", None)
        if cb:
            cb(audio_file, entry_index)

    def _copy_history_item(self, text: str) -> None:
        """Copy a history item to clipboard."""
        self._copy_to_clipboard(text)
        # Show brief status update
        self.set_status("Copied!")

    def _play_audio(self, filename: str, button: QPushButton) -> None:
        """Play or stop an audio recording."""
        from PyQt6.QtCore import QUrl
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

        # If already playing this file, stop it
        if hasattr(self, "_playing_button") and self._playing_button == button:
            self._media_player.stop()
            button.setIcon(get_play_icon(14, "#888"))
            button.setToolTip("Play recording")
            self._playing_button = None
            return

        audio_path = self.config.get_recordings_dir() / filename
        if not audio_path.exists():
            self.set_status("Audio file not found")
            return

        # Create or reuse media player
        if not hasattr(self, "_media_player"):
            self._media_player = QMediaPlayer()
            self._audio_output = QAudioOutput()
            self._media_player.setAudioOutput(self._audio_output)
            # Connect to playback state changes
            self._media_player.playbackStateChanged.connect(self._on_playback_state_changed)

        # Stop any current playback and reset previous button
        if hasattr(self, "_playing_button") and self._playing_button:
            self._playing_button.setIcon(get_play_icon(14, "#888"))
            self._playing_button.setToolTip("Play recording")
        self._media_player.stop()

        # Update button to stop icon
        button.setIcon(get_stop_icon(14, "#888"))
        button.setToolTip("Stop playback")
        self._playing_button = button

        # Play the file
        self._media_player.setSource(QUrl.fromLocalFile(str(audio_path)))
        self._audio_output.setVolume(1.0)
        self._media_player.play()

    def _on_playback_state_changed(self, state) -> None:
        """Handle media player state changes."""
        from PyQt6.QtMultimedia import QMediaPlayer

        if state == QMediaPlayer.PlaybackState.StoppedState:
            # Reset button icon when playback stops
            if hasattr(self, "_playing_button") and self._playing_button:
                self._playing_button.setIcon(get_play_icon(14, "#888"))
                self._playing_button.setToolTip("Play recording")
                self._playing_button = None

    def _close_window(self) -> None:
        """Close the window (emits cancel if recording)."""
        self.cancel_requested.emit()
        self.hide()

    def center_on_screen(self) -> None:
        """Center window on the screen."""
        screen = QApplication.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = int(screen.height() * 0.3)  # Upper third of screen
        self.move(x, y)

    def mousePressEvent(self, event) -> None:
        """Handle mouse press for dragging."""
        if event.button() == Qt.MouseButton.LeftButton:
            # Use startSystemMove for Wayland compatibility
            if hasattr(self.windowHandle(), "startSystemMove"):
                self.windowHandle().startSystemMove()
            else:
                # Fallback for X11
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        """Handle mouse move for dragging (X11 fallback)."""
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        """Handle mouse release."""
        self._drag_pos = None

    def showEvent(self, event) -> None:
        """Set NSWindow collection behavior on every show to prevent focus stealing."""
        super().showEvent(event)
        if sys.platform == "darwin":
            QTimer.singleShot(0, self._set_macos_no_focus_behavior)

    def _set_macos_no_focus_behavior(self) -> None:
        """Configure NSWindow so it never steals focus and stays above other windows.

        Uses PyObjC objc_object to get the real NSWindow from Qt's NSView pointer.
        - NSWindowCollectionBehaviorStationary (1<<4): shown without stealing focus
        - NSWindowCollectionBehaviorIgnoresCycle (1<<6): excluded from Cmd+Tab
        - NSWindowCollectionBehaviorCanJoinAllSpaces (1<<0): visible on all spaces
        - NSFloatingWindowLevel: always on top
        """
        try:
            import objc as pyobjc
            from AppKit import NSFloatingWindowLevel

            ns_view = pyobjc.objc_object(c_void_p=int(self.winId()))
            ns_window = ns_view.window()
            if ns_window is None:
                return

            NO_FOCUS = (1 << 4) | (1 << 6) | (1 << 0)
            ns_window.setCollectionBehavior_(NO_FOCUS)
            ns_window.setLevel_(NSFloatingWindowLevel)
        except Exception as e:
            print(f"macOS no-focus behavior: {e}")


class TurboWhisper:
    """Main application class."""

    def __init__(self):
        self.config = Config.load()
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setWindowIcon(get_tray_icon(128, recording=False))

        # Components
        self.recorder = AudioRecorder(self.config)
        self.client = WhisperClient(self.config)
        self.typer = Typer(typing_delay_ms=self.config.typing_delay_ms)
        self.signals = SignalBridge()

        # UI
        self.window = RecordingWindow(self.config)
        self.window.set_retranscribe_callback(self._retranscribe)
        self._setup_tray()

        # State machine — single source of truth
        self._state = AppState.IDLE
        self._transcribing = False          # guard against double transcription
        self._pending_audio_filename: str | None = None
        self._retranscribe_entry_index: int | None = None
        self._last_toggle_time: float = 0.0
        self._last_text: str = ""           # for Repeat button
        self._current_history_idx: int | None = None  # live history entry
        self._pending_waveform_data = None

        # Wire up buttons
        self.window.btn_primary.clicked.connect(self._on_primary_btn)
        self.window.btn_secondary.clicked.connect(self._cancel_recording)
        self.window.btn_repeat.clicked.connect(self._repeat_last)

        # Connect signals
        self.signals.toggle_recording.connect(self._toggle_recording)
        self.signals.transcription_complete.connect(self._on_transcription_complete)
        self.signals.transcription_error.connect(self._on_transcription_error)
        self.signals.transcription_chunk.connect(self._on_transcription_chunk)
        self.signals.show_status.connect(self.window.set_status)
        self.signals.state_changed.connect(self.window.update_controls)
        self.signals.set_app_state.connect(
            lambda s: self._set_state(AppState(s))
        )
        self.signals.show_window.connect(self.window.show)
        self.signals.hide_window.connect(self.window.hide)
        self.signals.tray_message.connect(self._show_tray_message)
        self.window.cancel_requested.connect(self._cancel_recording)

        # Timer to poll waveform data from recorder thread (avoids cross-thread signal issues)
        self._waveform_timer = QTimer()
        self._waveform_timer.timeout.connect(self._poll_waveform_data)
        self._waveform_timer.setInterval(30)  # Poll at ~33 FPS

        # Hotkey - use appropriate backend for platform
        self.hotkey_manager = create_hotkey_manager(
            self.config.hotkey,
            lambda: self.signals.toggle_recording.emit(),
        )
        if self.hotkey_manager is None:
            print("Warning: Global hotkeys not available on this platform")

        # Integration server for Claude Code
        self.integration_server = None
        if self.config.claude_integration:
            from .integration_server import IntegrationServer

            self.integration_server = IntegrationServer(self.config.claude_integration_port)
            if not self.integration_server.start():
                self.integration_server = None

    def _setup_tray(self) -> None:
        """Set up system tray icon."""
        self.tray = QSystemTrayIcon(self.app)

        # Create simple icon (will use default if no icon available)
        self.tray.setIcon(get_tray_icon(64, recording=False))  # Orange when idle
        hotkey_str = "+".join(k.capitalize() for k in self.config.hotkey)
        self.tray.setToolTip(f"Turbo Whisper - Press {hotkey_str} to dictate")

        # Context menu
        menu = QMenu()

        show_action = QAction("Show Window", menu)
        show_action.triggered.connect(self._show_window)
        menu.addAction(show_action)

        self.toggle_action = QAction("Start Recording", menu)
        self.toggle_action.triggered.connect(self._toggle_recording)
        menu.addAction(self.toggle_action)

        menu.addSeparator()

        settings_action = QAction("Settings...", menu)
        settings_action.triggered.connect(self._show_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """Handle tray icon clicks."""
        # Trigger = left click, DoubleClick = double click
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def _update_icons(self, recording: bool) -> None:
        """Update all icons based on recording state."""
        self.tray.setIcon(get_tray_icon(64, recording=recording))
        self.window.update_icon(recording=recording)

    def _show_tray_message(self, title: str, message: str, duration_ms: int) -> None:
        """Show tray notification — must be called from main thread."""
        self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, duration_ms)

    def _set_state(self, state: AppState) -> None:
        """Centralized state transition — updates all UI in one place."""
        self._state = state
        recording = state == AppState.RECORDING
        self._update_icons(recording=recording)
        self.signals.state_changed.emit(state.value)
        # Tray menu label
        if state == AppState.RECORDING:
            self.toggle_action.setText("Stop Recording")
        elif state in (AppState.TRANSCRIBING, AppState.TYPING):
            self.toggle_action.setText("Processing...")
        else:
            self.toggle_action.setText("Start Recording")

    def _on_primary_btn(self) -> None:
        """Handle primary button click — behaviour depends on current state."""
        if self._state == AppState.IDLE:
            self._start_recording()
        elif self._state == AppState.RECORDING:
            self._stop_recording()
        elif self._state == AppState.RESULT:
            self._start_recording()

    def _repeat_last(self) -> None:
        """Re-paste the last transcription into the focused window."""
        if not self._last_text:
            return
        text = self._last_text
        self._set_state(AppState.TYPING)

        def _do_paste(t=text):
            time.sleep(0.05)
            if self._wait_for_claude_ready():
                self.typer.type_text(t)
            else:
                self.typer.copy_to_clipboard(t)
            self.signals.set_app_state.emit(AppState.IDLE.value)
            self.signals.hide_window.emit()

        threading.Thread(target=_do_paste, daemon=True).start()

    def _save_wav(self, path, audio_data: bytes) -> None:
        """Save audio data as a WAV file."""
        import wave

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(self.config.channels)
            wf.setsampwidth(2)  # 16-bit audio = 2 bytes
            wf.setframerate(self.config.sample_rate)
            wf.writeframes(audio_data)

    def _show_window(self) -> None:
        """Show the window without stealing focus."""
        self.window.waveform.set_recording(False)
        self.window.set_recording_hint(recording=False)
        self.window.center_on_screen()
        self.window.show()

    def _show_settings(self) -> None:
        """Show the window with settings panel expanded (takes focus for editing)."""
        self._show_window()
        if not self.window.settings_panel.isVisible():
            self.window._toggle_settings()
        self.window.activateWindow()

    def _toggle_recording(self) -> None:
        """Hotkey handler — debounced, delegates to state machine."""
        now = time.monotonic()
        if now - self._last_toggle_time < 0.3:
            return
        self._last_toggle_time = now

        if self._state == AppState.RECORDING:
            self._stop_recording()
        elif self._state in (AppState.IDLE, AppState.RESULT):
            self._start_recording()
        # TRANSCRIBING / TYPING — ignore hotkey, not safe to interrupt

    def _start_recording(self) -> None:
        """Start recording audio."""
        if self._state == AppState.RECORDING:
            return

        self._set_state(AppState.RECORDING)
        self.window.clear_transcript_preview()
        self.window.waveform.set_recording(True)
        self.window.set_recording_hint(recording=True)

        # Add live entry to history immediately so user can see it
        from datetime import datetime
        live_entry = {
            "text": "",
            "timestamp": datetime.now().isoformat(),
            "audio_file": None,
            "status": "recording",
        }
        self.config.history.insert(0, live_entry)
        self._current_history_idx = 0
        self.window._refresh_history()

        if self.window.settings_panel.isVisible():
            self.window._toggle_settings()

        self.window.center_on_screen()
        self.window.show()

        self._pending_waveform_data = None
        self._waveform_timer.start()
        self.recorder.start(level_callback=self._on_audio_level)

    def _cancel_recording(self) -> None:
        """Cancel recording (ESC / Cancel button). Also closes window in RESULT state."""
        if self._state == AppState.RESULT:
            self.window.clear_transcript_preview()
            self.window.hide()
            self._set_state(AppState.IDLE)
            return

        if self._state != AppState.RECORDING:
            return

        self._waveform_timer.stop()
        self.recorder.stop()

        # Remove the live "recording" history entry we added
        if self._current_history_idx is not None:
            try:
                entry = self.config.history[self._current_history_idx]
                if isinstance(entry, dict) and entry.get("status") == "recording":
                    self.config.history.pop(self._current_history_idx)
                    self.window._refresh_history()
            except IndexError:
                pass
        self._current_history_idx = None

        self.window.waveform.set_recording(False)
        self.window.clear_transcript_preview()
        self.window.hide()
        self._set_state(AppState.IDLE)

        self.tray.showMessage(
            "Turbo Whisper",
            "Recording cancelled",
            QSystemTrayIcon.MessageIcon.Information,
            1500,
        )

    def _stop_recording(self) -> None:
        """Stop recording and start transcription."""
        if self._state != AppState.RECORDING:
            return
        if self._transcribing:
            return

        self._waveform_timer.stop()
        self.window.waveform.set_recording(False)
        self.window.set_recording_hint(recording=False)
        self._set_state(AppState.TRANSCRIBING)

        audio_data = self.recorder.stop()

        # Save audio immediately so retry is always possible
        audio_filename = None
        if audio_data:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            audio_filename = f"{ts}.wav"
            audio_path = self.config.get_recordings_dir() / audio_filename
            try:
                self._save_wav(audio_path, audio_data)
                # Update live history entry with audio filename
                if self._current_history_idx is not None:
                    try:
                        self.config.history[self._current_history_idx]["audio_file"] = audio_filename
                    except (IndexError, TypeError):
                        pass
            except Exception as e:
                print(f"Warning: Could not save audio: {e}")
                audio_filename = None

        self._pending_audio_filename = audio_filename
        self._transcribing = True

        def transcribe():
            try:
                text = self.client.transcribe_stream(
                    audio_data,
                    chunk_callback=lambda chunk: self.signals.transcription_chunk.emit(chunk),
                )
                self.signals.transcription_complete.emit(text)
            except WhisperAPIError as e:
                self.signals.transcription_error.emit(str(e))

        threading.Thread(target=transcribe, daemon=True).start()

    def _on_audio_level(self, level: float, waveform_buffer: list[float]) -> None:
        """Handle audio level update from recorder (called from recorder thread)."""
        # Store data for main thread to poll (thread-safe assignment)
        self._pending_waveform_data = (level, list(waveform_buffer))

    def _poll_waveform_data(self) -> None:
        """Poll waveform data from recorder thread (called from main thread timer)."""
        if self._pending_waveform_data is not None:
            level, waveform_buffer = self._pending_waveform_data
            self.window.waveform.update_waveform(level, waveform_buffer)
            # Update mic level meter (scale 0-1 to 0-100, cap at 100)
            self.window.update_mic_level(level)

    def _is_claude_running(self) -> bool:
        """Check if Claude Code process is running."""
        try:
            result = subprocess.run(
                ["pgrep", "-x", "claude"],
                capture_output=True,
                timeout=1,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _wait_for_claude_ready(self) -> bool:
        """Wait for Claude to signal ready, with timeout.

        Returns True if ready signal received or Claude not running.
        Returns False if timed out waiting.
        """
        if not self.config.claude_integration or not self.integration_server:
            return True

        # Only wait if Claude is actually running
        if not self._is_claude_running():
            return True

        from .integration_server import IntegrationServer

        # Accept signal from last 30 seconds (covers recording + transcription time)
        if IntegrationServer.is_ready(max_age=30.0):
            IntegrationServer.reset_ready()
            return True

        # Otherwise wait for a new signal
        timeout = self.config.claude_wait_timeout
        start = time.time()
        while (time.time() - start) < timeout:
            if IntegrationServer.is_ready(max_age=1.0):
                IntegrationServer.reset_ready()
                return True
            time.sleep(0.1)
        return False

    def _on_transcription_chunk(self, text: str) -> None:
        """Streaming chunk — update preview and live history entry."""
        self.window.show_transcript_chunk(text)
        # Update live history item text without full rebuild
        if self._current_history_idx is not None:
            try:
                self.config.history[self._current_history_idx]["text"] = text
                self.window._update_history_item(self._current_history_idx)
            except (IndexError, TypeError):
                pass

    def _on_transcription_complete(self, text: str) -> None:
        """Handle completed transcription."""
        self._transcribing = False
        audio_filename = self._pending_audio_filename
        self._pending_audio_filename = None

        # For retranscribe: remove old entry first (it will be replaced)
        retranscribe_index = self._retranscribe_entry_index
        self._retranscribe_entry_index = None
        if retranscribe_index is not None and 0 <= retranscribe_index < len(self.config.history):
            self.config.history.pop(retranscribe_index)
            # live idx shifted by removal
            if self._current_history_idx is not None and retranscribe_index < self._current_history_idx:
                self._current_history_idx -= 1

        if text:
            self._last_text = text

            if not self.config.store_recordings and audio_filename:
                try:
                    (self.config.get_recordings_dir() / audio_filename).unlink(missing_ok=True)
                except OSError:
                    pass
                audio_filename = None

            # Update live history entry in-place
            if self._current_history_idx is not None:
                try:
                    entry = self.config.history[self._current_history_idx]
                    entry["text"] = text
                    entry["status"] = "ok"
                    if audio_filename:
                        entry["audio_file"] = audio_filename
                    self.config.save()
                    self.window._refresh_history()
                except (IndexError, TypeError):
                    self.config.add_to_history(text, audio_file=audio_filename)
                    self.window._refresh_history()
            else:
                self.config.add_to_history(text, audio_file=audio_filename)
                self.window._refresh_history()

            self._current_history_idx = None
            self.window.show_result(text)
            self._set_state(AppState.RESULT)

            if self.config.copy_to_clipboard:
                self.typer.copy_to_clipboard(text)

            # Paste in background thread — never blocks Qt main thread
            if self.config.auto_paste:
                self._set_state(AppState.TYPING)

                def _paste_async(t=text):
                    time.sleep(0.05)
                    if self._wait_for_claude_ready():
                        self.typer.type_text(t)
                    else:
                        self.signals.tray_message.emit("Turbo Whisper", "Copied (Claude busy)", 2000)
                    # Hide window after paste — user can bring it back with ESC/Cancel
                    self.signals.set_app_state.emit(AppState.IDLE.value)
                    self.signals.hide_window.emit()

                threading.Thread(target=_paste_async, daemon=True).start()
        else:
            # No speech detected
            if self._current_history_idx is not None:
                try:
                    entry = self.config.history[self._current_history_idx]
                    entry["status"] = "empty"
                    self.config.save()
                    self.window._refresh_history()
                except (IndexError, TypeError):
                    if audio_filename:
                        self.config.add_failed_to_history(audio_filename, status="empty")
                        self.window._refresh_history()
            self._current_history_idx = None
            self.window.show_error_result("🔇 No speech detected")
            self._set_state(AppState.RESULT)
            self.tray.showMessage(
                "Turbo Whisper",
                "No speech detected — saved for retry",
                QSystemTrayIcon.MessageIcon.Warning,
                2000,
            )

    def _on_transcription_error(self, error: str) -> None:
        """Handle transcription error."""
        self._transcribing = False
        audio_filename = self._pending_audio_filename
        self._pending_audio_filename = None

        if self._current_history_idx is not None:
            try:
                entry = self.config.history[self._current_history_idx]
                entry["status"] = "failed"
                self.config.save()
                self.window._refresh_history()
            except (IndexError, TypeError):
                if audio_filename:
                    self.config.add_failed_to_history(audio_filename, status="failed")
                    self.window._refresh_history()
        self._current_history_idx = None

        short_error = error[:60] + "…" if len(error) > 60 else error
        self.window.show_error_result(f"❌ {short_error}")
        self._set_state(AppState.RESULT)

        self.tray.showMessage(
            "Turbo Whisper - Error",
            error,
            QSystemTrayIcon.MessageIcon.Critical,
            3000,
        )

    def _retranscribe(self, audio_filename: str, entry_index: int) -> None:
        """Re-send a saved audio file to Whisper for transcription."""
        if self._state == AppState.RECORDING or self._transcribing:
            return

        audio_path = self.config.get_recordings_dir() / audio_filename
        if not audio_path.exists():
            self.tray.showMessage(
                "Turbo Whisper", "Audio file not found",
                QSystemTrayIcon.MessageIcon.Warning, 2000,
            )
            return

        import wave
        try:
            with wave.open(str(audio_path), "rb") as wf:
                audio_data = wf.readframes(wf.getnframes())
        except Exception as e:
            self.tray.showMessage(
                "Turbo Whisper", f"Cannot read audio: {e}",
                QSystemTrayIcon.MessageIcon.Critical, 2000,
            )
            return

        self._pending_audio_filename = audio_filename
        self._retranscribe_entry_index = entry_index
        self._current_history_idx = entry_index
        self._transcribing = True
        self._set_state(AppState.TRANSCRIBING)
        self.window.clear_transcript_preview()
        self.window.show()

        def transcribe():
            try:
                text = self.client.transcribe_stream(
                    audio_data,
                    chunk_callback=lambda chunk: self.signals.transcription_chunk.emit(chunk),
                )
                self.signals.transcription_complete.emit(text)
            except WhisperAPIError as e:
                self.signals.transcription_error.emit(str(e))

        threading.Thread(target=transcribe, daemon=True).start()

    def _quit(self) -> None:
        """Clean up and quit application."""
        if self.hotkey_manager:
            self.hotkey_manager.stop()
        if self.integration_server:
            self.integration_server.stop()
        self.recorder.cleanup()
        self.app.quit()

    def run(self) -> int:
        """Run the application."""
        if self.hotkey_manager:
            self.hotkey_manager.start()

        hotkey_str = "+".join(k.title() for k in self.config.hotkey)
        self.tray.showMessage(
            "Turbo Whisper",
            f"Press {hotkey_str} to start dictating",
            QSystemTrayIcon.MessageIcon.Information,
            3000,
        )

        return self.app.exec()


_lock_fd = None  # Global to keep lock file descriptor open


def ensure_single_instance():
    """Ensure only one instance of the app is running."""
    global _lock_fd

    if sys.platform == "win32":
        # Windows: use msvcrt.locking
        lock_path = os.path.join(tempfile.gettempdir(), "turbo-whisper.lock")
        try:
            # Open/create lock file (kept open to hold the lock)
            _lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            # Try to acquire exclusive lock (non-blocking)
            msvcrt.locking(_lock_fd, msvcrt.LK_NBLCK, 1)
            # Write PID
            os.lseek(_lock_fd, 0, os.SEEK_SET)
            os.ftruncate(_lock_fd, 0)
            os.write(_lock_fd, str(os.getpid()).encode())
        except OSError:
            print("Turbo Whisper is already running.")
            sys.exit(0)
    else:
        # Unix: use fcntl.flock
        lock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "turbo-whisper.lock")
        try:
            # Open with O_CREAT to create if doesn't exist (kept open to hold the lock)
            _lock_fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID
            os.ftruncate(_lock_fd, 0)
            os.write(_lock_fd, str(os.getpid()).encode())
        except OSError:
            print("Turbo Whisper is already running.")
            sys.exit(0)


def main():
    """Application entry point."""
    ensure_single_instance()
    app = TurboWhisper()
    sys.exit(app.run())


if __name__ == "__main__":
    main()
