# -*- coding: utf-8 -*-
"""
plugin.py
Kelas utama plugin BHUMI Persil Connector v2.0.

Fitur:
  - Tombol toolbar 'Tampilkan Persil (Bidang Tanah)' + menu &BHUMI ATR/BPN
  - Proxy server lokal dengan port dari QSettings
  - RAM tile cache dengan TTL 24 jam + pre-fetch 3×3 grid
  - Zoom-out persistence: tampilkan mosaic dari cache saat zoom out
  - Tombol 'Hapus Cache' + tooltip statistik cache
  - Export tampilan kanvas ke PNG/JPEG
  - Pengaturan plugin via SettingsDialog
"""

from __future__ import annotations

import os
from typing import Optional

from qgis.core import QgsProject, QgsRasterLayer
from qgis.PyQt.QtCore import QSettings, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QMenu, QMessageBox, QToolBar

from .bhumi_layers import BHUMI_LAYERS
from .export_tool import start_rectangle_export
from .proxy_server import BhumiProxyServer
from .tile_cache import TILE_CACHE
from .settings_dialog import (
    SettingsDialog,
    load_enabled_layers,
    load_port,
)

PLUGIN_NAME = "BHUMI Persil Connector"
MENU_TITLE = "&BHUMI ATR/BPN"
TOOLBAR_NAME = "BHUMI ATR/BPN"
SETTINGS_GROUP = "BhumiPersilConnector"

# ─────────────────────────────────────────────────────────────────────────────
# Helper: path ikon
# ─────────────────────────────────────────────────────────────────────────────
PLUGIN_DIR = os.path.dirname(__file__)


def _icon(name: str) -> QIcon:
    path = os.path.join(PLUGIN_DIR, "resources", name)
    return QIcon(path) if os.path.exists(path) else QIcon()


# ─────────────────────────────────────────────────────────────────────────────
# Plugin utama
# ─────────────────────────────────────────────────────────────────────────────

class BhumiPersilPlugin:
    """Plugin utama BHUMI Persil Connector."""

    def __init__(self, iface):
        self.iface = iface
        self.canvas = iface.mapCanvas()

        # Proxy server
        port = load_port()
        self.proxy = BhumiProxyServer(port=port)

        # Daftar QAction untuk layer
        self._layer_actions: dict[str, QAction] = {}

        # Toolbar & menu
        self._toolbar: Optional[QToolBar] = None
        self._menu: Optional[QMenu] = None
        self._settings_action: Optional[QAction] = None
        self._export_action: Optional[QAction] = None
        self._cache_action: Optional[QAction] = None
        self._separator_actions: list = []

        # Timer untuk update tooltip cache stats tiap 10 detik
        self._cache_stats_timer = QTimer()
        self._cache_stats_timer.setInterval(10_000)
        self._cache_stats_timer.timeout.connect(self._update_cache_tooltip)

    # ── QGIS lifecycle ────────────────────────────────────────────────────────

    def initGui(self):  # noqa: N802
        """Dipanggil QGIS saat plugin diaktifkan. Buat toolbar & menu."""
        # Buat toolbar khusus BHUMI
        self._toolbar = self.iface.addToolBar(TOOLBAR_NAME)
        self._toolbar.setObjectName("BhumiPersilToolbar")

        # Bangun aksi layer
        enabled_ids = load_enabled_layers()
        self._build_layer_actions(enabled_ids)

        # ── Separator + Cache + Export + Settings ──
        sep1 = self._toolbar.addSeparator()
        self._separator_actions.append(sep1)

        self._cache_action = QAction(
            _icon("cache.png"),
            "🗑 Hapus Cache Tile",
            self.iface.mainWindow(),
        )
        self._update_cache_tooltip()   # set tooltip awal dengan stats
        self._cache_action.triggered.connect(self._on_clear_cache)
        self._toolbar.addAction(self._cache_action)
        self.iface.addPluginToMenu(MENU_TITLE, self._cache_action)

        sep2 = self._toolbar.addSeparator()
        self._separator_actions.append(sep2)

        self._export_action = QAction(
            _icon("export.png"),
            "📷 Export Area ke GeoTIFF",
            self.iface.mainWindow(),
        )
        self._export_action.setToolTip(
            "Tarik kotak (drag rectangle) di kanvas untuk mengunduh bidang tanah dengan resolusi terbaik ke GeoTIFF"
        )
        self._export_action.triggered.connect(self._on_export)
        self._toolbar.addAction(self._export_action)
        self.iface.addPluginToMenu(MENU_TITLE, self._export_action)

        sep3 = self._toolbar.addSeparator()
        self._separator_actions.append(sep3)

        self._settings_action = QAction(
            _icon("settings.png"),
            "⚙ Pengaturan BHUMI",
            self.iface.mainWindow(),
        )
        self._settings_action.setToolTip("Buka pengaturan port proxy")
        self._settings_action.triggered.connect(self._on_open_settings)
        self._toolbar.addAction(self._settings_action)
        self.iface.addPluginToMenu(MENU_TITLE, self._settings_action)

        # Mulai timer update tooltip stats cache tiap 10 detik
        self._cache_stats_timer.start()

    def unload(self):
        """Dipanggil QGIS saat plugin dinonaktifkan. Bersihkan semua resource."""
        # Hentikan timer stats cache
        self._cache_stats_timer.stop()

        # Bersihkan cache RAM
        TILE_CACHE.clear()

        # Hentikan proxy
        self.proxy.stop()

        # Hapus semua aksi dari menu & toolbar
        for action in list(self._layer_actions.values()):
            self.iface.removePluginMenu(MENU_TITLE, action)
            self.iface.removeToolBarIcon(action)

        if self._cache_action:
            self.iface.removePluginMenu(MENU_TITLE, self._cache_action)
        if self._export_action:
            self.iface.removePluginMenu(MENU_TITLE, self._export_action)
        if self._settings_action:
            self.iface.removePluginMenu(MENU_TITLE, self._settings_action)

        # Hapus toolbar
        if self._toolbar:
            self._toolbar.clear()
            del self._toolbar
            self._toolbar = None

    # ── Builder aksi ─────────────────────────────────────────────────────────

    def _build_layer_actions(self, enabled_ids: list[str]):
        """Buat QAction untuk setiap layer yang diaktifkan."""
        self._layer_actions.clear()

        for lyr in BHUMI_LAYERS:
            if lyr["id"] not in enabled_ids:
                continue

            # ── Tombol "Tampilkan Layer" ──
            show_action = QAction(
                _icon(f"{lyr['id']}.png"),
                lyr["menu_text"].replace("&", ""),
                self.iface.mainWindow(),
            )
            show_action.setToolTip(lyr["tooltip"])
            show_action.setData(lyr["id"])
            show_action.triggered.connect(
                lambda checked, lid=lyr["id"]: self._on_show_layer(lid)
            )
            self._toolbar.addAction(show_action)
            self.iface.addPluginToMenu(MENU_TITLE, show_action)
            self._layer_actions[lyr["id"]] = show_action

    def _rebuild_layer_actions(self):
        """Hapus dan bangun ulang aksi layer (dipanggil setelah settings berubah)."""
        # Hapus aksi lama
        for action in list(self._layer_actions.values()):
            self.iface.removePluginMenu(MENU_TITLE, action)
            if self._toolbar:
                self._toolbar.removeAction(action)

        # Hapus separator & export & settings dulu (akan ditambah ulang)
        for sep in self._separator_actions:
            if self._toolbar:
                self._toolbar.removeAction(sep)
        self._separator_actions.clear()

        if self._cache_action and self._toolbar:
            self._toolbar.removeAction(self._cache_action)
            self.iface.removePluginMenu(MENU_TITLE, self._cache_action)
        if self._export_action and self._toolbar:
            self._toolbar.removeAction(self._export_action)
            self.iface.removePluginMenu(MENU_TITLE, self._export_action)
        if self._settings_action and self._toolbar:
            self._toolbar.removeAction(self._settings_action)
            self.iface.removePluginMenu(MENU_TITLE, self._settings_action)

        # Bangun ulang
        enabled_ids = load_enabled_layers()
        self._build_layer_actions(enabled_ids)

        # Tambah kembali Cache + Export + Settings
        sep1 = self._toolbar.addSeparator()
        self._separator_actions.append(sep1)
        self._toolbar.addAction(self._cache_action)
        self.iface.addPluginToMenu(MENU_TITLE, self._cache_action)

        sep2 = self._toolbar.addSeparator()
        self._separator_actions.append(sep2)
        self._toolbar.addAction(self._export_action)
        self.iface.addPluginToMenu(MENU_TITLE, self._export_action)

        sep3 = self._toolbar.addSeparator()
        self._separator_actions.append(sep3)
        self._toolbar.addAction(self._settings_action)
        self.iface.addPluginToMenu(MENU_TITLE, self._settings_action)

    # ── Slot handlers ─────────────────────────────────────────────────────────

    def _on_show_layer(self, layer_id: str):
        """Tampilkan layer BHUMI ke kanvas QGIS."""
        # Pastikan proxy berjalan
        ok, msg = self.proxy.start()
        if not ok:
            self.iface.messageBar().pushCritical("BHUMI", msg)
            return

        # Cari definisi layer
        lyr_def = next((l for l in BHUMI_LAYERS if l["id"] == layer_id), None)
        if not lyr_def:
            return

        layer_name = lyr_def["label"]

        # Cek apakah layer sudah ada di proyek
        existing = QgsProject.instance().mapLayersByName(layer_name)
        if existing:
            self.iface.messageBar().pushInfo(
                "BHUMI",
                f"Layer '{layer_name}' sudah aktif di kanvas.",
            )
            return

        # Bangun URI XYZ tile menuju proxy lokal
        uri = self.proxy.build_tile_url(
            layer_id=layer_id,
            zmin=lyr_def["zmin"],
            zmax=lyr_def["zmax"],
        )

        layer = QgsRasterLayer(uri, layer_name, "wms")

        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)
            self.iface.messageBar().pushSuccess(
                "BHUMI",
                f"✔ Layer '{layer_name}' berhasil dimuat.",
            )
        else:
            self.iface.messageBar().pushCritical(
                "BHUMI",
                f"✗ Gagal memuat layer '{layer_name}'. "
                "Proxy mungkin belum siap atau port konflik.",
            )

    def _on_export(self):
        """Aktifkan alat seleksi kotak di kanvas untuk export ke GeoTIFF."""
        start_rectangle_export(self.iface, self.canvas)

    def _on_clear_cache(self):
        """Hapus semua tile yang ada di RAM cache."""
        stats = TILE_CACHE.stats()
        count = stats["count"]
        size  = stats["size_mb"]

        if count == 0:
            self.iface.messageBar().pushInfo("BHUMI Cache", "Cache sudah kosong.")
            return

        reply = QMessageBox.question(
            self.iface.mainWindow(),
            "Hapus Cache Tile BHUMI",
            f"Cache saat ini berisi {count:,} tile ({size} MB).\n"
            "Hapus semua tile dari cache?\n\n"
            "Tile akan di-fetch ulang dari server BHUMI saat dibutuhkan.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )

        if reply == QMessageBox.Yes:
            n = TILE_CACHE.clear()
            self._update_cache_tooltip()
            self.iface.messageBar().pushSuccess(
                "BHUMI Cache",
                f"✔ Cache berhasil dihapus ({n:,} tile, {size} MB dibebaskan).",
            )

    def _update_cache_tooltip(self):
        """
        Update tooltip tombol cache dengan statistik terkini.
        Dipanggil tiap 10 detik oleh QTimer.
        """
        if self._cache_action is None:
            return
        stats = TILE_CACHE.stats()
        count = stats["count"]
        size  = stats["size_mb"]

        by_zoom = stats.get("by_zoom", {})
        zoom_detail = ""
        if by_zoom:
            parts = [f"z{z}: {n}" for z, n in sorted(by_zoom.items())]
            zoom_detail = "\nPer zoom: " + ", ".join(parts)

        tooltip = (
            f"🗑 Hapus Cache Tile BHUMI\n"
            f"─────────────────────────\n"
            f"Tile tersimpan : {count:,} tile\n"
            f"Estimasi RAM   : {size} MB\n"
            f"TTL            : 24 jam{zoom_detail}\n"
            f"─────────────────────────\n"
            f"Klik untuk menghapus semua cache."
        )
        self._cache_action.setToolTip(tooltip)

    def _on_open_settings(self):
        """Buka dialog pengaturan plugin."""
        old_port = self.proxy.port

        dlg = SettingsDialog(parent=self.iface.mainWindow())
        if dlg.exec_() == SettingsDialog.Accepted:
            new_port = load_port()

            # Jika port berubah → restart proxy
            if new_port != old_port and self.proxy.is_running:
                ok, msg = self.proxy.restart(new_port=new_port)
                if ok:
                    self.iface.messageBar().pushSuccess("BHUMI", msg)
                    self._reload_all_active_layers()
                else:
                    self.iface.messageBar().pushCritical("BHUMI", msg)

            self._rebuild_layer_actions()

    # ── Helper ────────────────────────────────────────────────────────────────

    def _reload_all_active_layers(self):
        """
        Hapus dan muat ulang semua layer BHUMI yang sedang aktif di proyek
        (berguna setelah port proxy berubah).
        """
        project = QgsProject.instance()
        layer_names = {lyr["label"]: lyr for lyr in BHUMI_LAYERS}

        for layer_name, lyr_def in layer_names.items():
            existing_layers = project.mapLayersByName(layer_name)
            if not existing_layers:
                continue
            for el in existing_layers:
                project.removeMapLayer(el.id())
            self._on_show_layer(lyr_def["id"])
