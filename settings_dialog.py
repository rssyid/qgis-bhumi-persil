# -*- coding: utf-8 -*-
"""
settings_dialog.py
Dialog konfigurasi plugin BHUMI Persil Connector.

Fitur:
  - Input port proxy (disimpan via QSettings)
  - Daftar layer yang bisa di-toggle aktif/nonaktif
  - Tombol OK / Batal
"""

from qgis.PyQt.QtCore import QSettings, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .bhumi_layers import BHUMI_LAYERS

SETTINGS_GROUP = "BhumiPersilConnector"


class SettingsDialog(QDialog):
    """Dialog pengaturan plugin BHUMI Persil Connector."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pengaturan BHUMI Persil Connector")
        self.setMinimumWidth(420)
        self.setModal(True)

        self._settings = QSettings()
        self._layer_checkboxes: dict[str, QCheckBox] = {}

        self._build_ui()
        self._load_settings()

    # ── UI Builder ────────────────────────────────────────────────────────────

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)

        # ── Bagian Port ──────────────────────────────────────────────────────
        port_group = QGroupBox("Konfigurasi Proxy Server")
        port_form = QFormLayout()
        port_form.setLabelAlignment(Qt.AlignRight)

        self._port_spin = QSpinBox()
        self._port_spin.setRange(1024, 65535)
        self._port_spin.setValue(8089)
        self._port_spin.setToolTip(
            "Port lokal yang digunakan proxy tile server.\n"
            "Default: 8089. Ubah jika ada konflik port."
        )

        port_note = QLabel(
            '<span style="color: gray; font-size: 9pt;">'
            "⚠ Perubahan port akan me-restart proxy server dan me-reload semua layer aktif."
            "</span>"
        )
        port_note.setWordWrap(True)

        port_form.addRow("Port Proxy:", self._port_spin)
        port_form.addRow("", port_note)
        port_group.setLayout(port_form)
        main_layout.addWidget(port_group)

        # ── Bagian Layer ─────────────────────────────────────────────────────
        layer_group = QGroupBox("Layer yang Ditampilkan di Toolbar")
        layer_layout = QVBoxLayout()

        layer_info = QLabel(
            '<span style="color: gray; font-size: 9pt;">'
            "Centang layer yang ingin tersedia sebagai tombol di toolbar QGIS."
            "</span>"
        )
        layer_info.setWordWrap(True)
        layer_layout.addWidget(layer_info)

        # Scroll area agar tidak terlalu panjang jika banyak layer
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(220)

        scroll_content = QWidget()
        scroll_vlayout = QVBoxLayout(scroll_content)
        scroll_vlayout.setSpacing(4)

        for lyr in BHUMI_LAYERS:
            cb = QCheckBox(lyr["label"])
            cb.setToolTip(lyr["tooltip"])
            self._layer_checkboxes[lyr["id"]] = cb
            scroll_vlayout.addWidget(cb)

        scroll_vlayout.addStretch()
        scroll.setWidget(scroll_content)
        layer_layout.addWidget(scroll)
        layer_group.setLayout(layer_layout)
        main_layout.addWidget(layer_group)

        # ── Tombol OK / Batal ─────────────────────────────────────────────────
        btn_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal,
        )
        btn_box.accepted.connect(self._save_and_accept)
        btn_box.rejected.connect(self.reject)
        main_layout.addWidget(btn_box)

    # ── Settings load / save ──────────────────────────────────────────────────

    def _load_settings(self):
        s = self._settings
        s.beginGroup(SETTINGS_GROUP)

        port = int(s.value("proxy_port", 8089))
        self._port_spin.setValue(port)

        for layer_id, cb in self._layer_checkboxes.items():
            # Default: semua layer aktif
            enabled = s.value(f"layer_enabled/{layer_id}", True, type=bool)
            cb.setChecked(enabled)

        s.endGroup()

    def _save_and_accept(self):
        s = self._settings
        s.beginGroup(SETTINGS_GROUP)
        s.setValue("proxy_port", self._port_spin.value())

        for layer_id, cb in self._layer_checkboxes.items():
            s.setValue(f"layer_enabled/{layer_id}", cb.isChecked())

        s.endGroup()
        s.sync()
        self.accept()

    # ── Akses nilai dari luar ─────────────────────────────────────────────────

    @property
    def selected_port(self) -> int:
        return self._port_spin.value()

    @property
    def enabled_layers(self) -> list[str]:
        """Kembalikan list layer_id yang dicentang."""
        return [lid for lid, cb in self._layer_checkboxes.items() if cb.isChecked()]


# ─────────────────────────────────────────────────────────────────────────────
# Helper: baca settings tanpa membuka dialog
# ─────────────────────────────────────────────────────────────────────────────

def load_port() -> int:
    """Baca port proxy dari QSettings."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    port = int(s.value("proxy_port", 8089))
    s.endGroup()
    return port


def load_enabled_layers() -> list[str]:
    """Baca daftar layer_id yang diaktifkan dari QSettings."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    result = []
    for lyr in BHUMI_LAYERS:
        enabled = s.value(f"layer_enabled/{lyr['id']}", True, type=bool)
        if enabled:
            result.append(lyr["id"])
    s.endGroup()
    return result


def save_port(port: int):
    """Simpan port ke QSettings."""
    s = QSettings()
    s.beginGroup(SETTINGS_GROUP)
    s.setValue("proxy_port", port)
    s.endGroup()
    s.sync()
