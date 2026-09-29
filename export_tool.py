# -*- coding: utf-8 -*-
"""
export_tool.py
Modul Export Area Terseleksi ke Georeferenced Image (GeoTIFF) dan
Vektorisasi Otomatis ke Shapefile (.shp) & GeoJSON (.geojson) untuk BHUMI ATR/BPN.

Fitur:
  - Multi-threaded Turbo Downloader: Konkurensi paralel hingga 16 - 24 worker threads
  - High-capacity HTTP Session Connection Pooling (pool_maxsize=64)
  - Real-time Download Telemetry: Menampilkan kecepatan unduh (tiles/detik) & sisa waktu
  - Vektorisasi Poligon Otomatis (Raster -> Vector):
    * Mengekstrak petak bidang tanah menjadi Poligon vektor tertutup (Polygon)
    * Simplifikasi geometri anti-bergerigi (Douglas-Peucker 0.2m)
    * Perhitungan otomatis atribut luas (m²) dan keliling (m)
    * Ekspor simultan ke ESRI Shapefile (.shp) dan GeoJSON (.geojson)
  - RectangleAreaTool: Map tool interaktif untuk menarik kotak area (RubberBand)
  - Otomatis memuat layer GeoTIFF dan Layer Vektor Poligon ke kanvas QGIS.
"""

from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsField,
    QgsFields,
    QgsFeature,
    QgsGeometry,
    QgsMapRendererCustomPainterJob,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapCanvas, QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QPoint, QRect, QRectF, QSize, Qt, QVariant
from qgis.PyQt.QtGui import QColor, QCursor, QImage, QPainter
from qgis.PyQt.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QVBoxLayout,
)

from .proxy_server import (
    BHUMI_WMS_URL,
    BROWSER_HEADERS,
    INITIAL_RES,
    ORIGIN_SHIFT,
    PROXY_TIMEOUT,
    _fetch_one_tile,
    xyz_to_bbox,
)
from .tile_cache import TILE_CACHE

EPSG_3857_WKT = (
    'PROJCS["WGS 84 / Pseudo-Mercator",'
    'GEOGCS["WGS 84",'
    'DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,'
    'AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],'
    'PRIMEM["Greenwich",0,'
    'AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,'
    'AUTHORITY["EPSG","9122"]],'
    'AUTHORITY["EPSG","4326"]],'
    'PROJECTION["Mercator_1SP"],'
    'PARAMETER["central_meridian",0],'
    'PARAMETER["scale_factor",1],'
    'PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],'
    'UNIT["metre",1,'
    'AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],'
    'AXIS["Northing",NORTH],'
    'EXTENSION["PROJ4","+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 '
    '+x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs"],'
    'AUTHORITY["EPSG","3857"]]'
)


# ─────────────────────────────────────────────────────────────────────────────
# High-Performance HTTP Session Pool untuk Export
# ─────────────────────────────────────────────────────────────────────────────

def _create_export_session() -> requests.Session:
    """Buat session HTTP khusus export dengan connection pool besar (64 connections)."""
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=2)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.headers.update(BROWSER_HEADERS)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Helper Konversi Koordinat EPSG:3857 ke Tile Index
# ─────────────────────────────────────────────────────────────────────────────

def coord_to_tile(x: float, y: float, z: int) -> tuple[int, int]:
    """Konversi koordinat meter EPSG:3857 ke indeks tile (tx, ty)."""
    res = INITIAL_RES / (2 ** z)
    tx = int(math.floor((x + ORIGIN_SHIFT) / (256.0 * res)))
    ty = int(math.floor((ORIGIN_SHIFT - y) / (256.0 * res)))
    return tx, ty


def get_tile_bounds(rect_3857: QgsRectangle, z: int) -> tuple[int, int, int, int]:
    """
    Hitung range tile (min_tx, max_tx, min_ty, max_ty) yang mencakup rectangle.
    """
    min_tx, max_ty = coord_to_tile(rect_3857.xMinimum(), rect_3857.yMinimum(), z)
    max_tx, min_ty = coord_to_tile(rect_3857.xMaximum(), rect_3857.yMaximum(), z)
    return min_tx, max_tx, min_ty, max_ty


# ─────────────────────────────────────────────────────────────────────────────
# Dialog Konfigurasi Export
# ─────────────────────────────────────────────────────────────────────────────

class ExportAreaDialog(QDialog):
    """
    Dialog konfigurasi resolusi zoom level, kecepatan thread paralel,
    opsi vektorisasi poligon (.shp & .geojson), opsi basemap, dan estimasi tile.
    """

    def __init__(self, rect_3857: QgsRectangle, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Area Bidang Tanah ke GeoTIFF & Vektor")
        self.setMinimumWidth(500)
        self.rect = rect_3857

        self.width_m = rect_3857.width()
        self.height_m = rect_3857.height()

        self._build_ui()
        self._on_zoom_changed()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # ── Group Info Area ──
        area_group = QGroupBox("Dimensi Area Terpilih")
        area_form = QFormLayout(area_group)

        width_str = f"{self.width_m:,.1f} meter" if self.width_m < 1000 else f"{self.width_m/1000:.2f} km"
        height_str = f"{self.height_m:,.1f} meter" if self.height_m < 1000 else f"{self.height_m/1000:.2f} km"
        area_form.addRow("Lebar Area:", QLabel(f"<b>{width_str}</b>"))
        area_form.addRow("Tinggi Area:", QLabel(f"<b>{height_str}</b>"))
        area_form.addRow("Koordinat BBOX:", QLabel(
            f"<span style='font-size:8pt; color:gray;'>"
            f"X: {self.rect.xMinimum():.0f} s/d {self.rect.xMaximum():.0f}<br>"
            f"Y: {self.rect.yMinimum():.0f} s/d {self.rect.yMaximum():.0f}"
            f"</span>"
        ))
        layout.addWidget(area_group)

        # ── Group Resolusi / Zoom & Thread ──
        res_group = QGroupBox("Pengaturan Kualitas & Kecepatan Unduh")
        res_form = QFormLayout(res_group)

        self.zoom_combo = QComboBox()
        for z in range(14, 20):
            res_m = INITIAL_RES / (2 ** z)
            desc = ""
            if z == 19:
                desc = " (Sangat Detail / ~0.3 m/px)"
            elif z == 18:
                desc = " (Rekomendasi / ~0.6 m/px)"
            elif z == 17:
                desc = " (Detail / ~1.2 m/px)"
            elif z == 16:
                desc = " (Menengah / ~2.4 m/px)"
            else:
                desc = f" (~{res_m:.1f} m/px)"
            self.zoom_combo.addItem(f"Zoom {z}{desc}", z)

        default_index = 4  # Zoom 18
        if self.width_m > 5000 or self.height_m > 5000:
            default_index = 2  # Zoom 16 jika area > 5km
        elif self.width_m > 2500 or self.height_m > 2500:
            default_index = 3  # Zoom 17 jika area > 2.5km
        self.zoom_combo.setCurrentIndex(default_index)
        self.zoom_combo.currentIndexChanged.connect(self._on_zoom_changed)

        # Pilihan Kecepatan / Concurrency Thread
        self.thread_combo = QComboBox()
        self.thread_combo.addItem("🚀 Turbo — 16 Thread Paralel (Sangat Cepat)", 16)
        self.thread_combo.addItem("⚡ Maksimal — 24 Thread Paralel (Koneksi Cepat)", 24)
        self.thread_combo.addItem("⚖ Cepat — 12 Thread Paralel", 12)
        self.thread_combo.addItem("🛡 Standar — 8 Thread Paralel (Ringan)", 8)
        self.thread_combo.setCurrentIndex(0)

        self.lbl_tile_count = QLabel("<b>-</b>")
        self.lbl_image_dim = QLabel("<b>-</b>")
        self.lbl_warning = QLabel("")
        self.lbl_warning.setWordWrap(True)
        self.lbl_warning.setStyleSheet("color: #d9534f; font-weight: bold;")

        res_form.addRow("Target Resolusi:", self.zoom_combo)
        res_form.addRow("Kecepatan Download:", self.thread_combo)
        res_form.addRow("Jumlah Tiles:", self.lbl_tile_count)
        res_form.addRow("Dimensi Gambar:", self.lbl_image_dim)
        res_form.addRow("", self.lbl_warning)
        layout.addWidget(res_group)

        # ── Group Opsi Output & Vektorisasi ──
        opt_group = QGroupBox("Opsi Format & Vektorisasi")
        opt_layout = QVBoxLayout(opt_group)

        # Opsi Vektorisasi Poligon
        self.cb_vectorize = QCheckBox("📐 Sekaligus Buat Vektor Poligon Bidang (.shp & .geojson)")
        self.cb_vectorize.setChecked(True)
        self.cb_vectorize.setStyleSheet("font-weight: bold; color: #1e7e34;")
        self.cb_vectorize.setToolTip(
            "Ekstrak otomatis garis batas menjadi poligon bidang tanah tertutup lengkap dengan atribut luas (m²) dan keliling (m)."
        )

        self.cb_basemap = QCheckBox("Sertakan Basemap Kanvas pada GeoTIFF (Google Maps / Citra Satelit)")
        self.cb_basemap.setChecked(False)

        self.cb_add_layer = QCheckBox("Otomatis muat layer GeoTIFF & Vektor ke kanvas QGIS setelah selesai")
        self.cb_add_layer.setChecked(True)

        opt_layout.addWidget(self.cb_vectorize)
        opt_layout.addWidget(self.cb_basemap)
        opt_layout.addWidget(self.cb_add_layer)
        layout.addWidget(opt_group)

        # ── Buttons ──
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Mulai Export...")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_zoom_changed(self):
        z = self.selected_zoom
        min_tx, max_tx, min_ty, max_ty = get_tile_bounds(self.rect, z)
        tiles_x = (max_tx - min_tx + 1)
        tiles_y = (max_ty - min_ty + 1)
        total_tiles = tiles_x * tiles_y

        res = INITIAL_RES / (2 ** z)
        px_w = int(self.width_m / res)
        px_h = int(self.height_m / res)

        self.lbl_tile_count.setText(f"<b>{total_tiles:,} tiles</b> ({tiles_x} × {tiles_y})")
        self.lbl_image_dim.setText(f"<b>{px_w:,} × {px_h:,} piksel</b> (resolusi {res:.2f} m/px)")

        if total_tiles > 400:
            self.lbl_warning.setText(
                f"⚠ Peringatan: {total_tiles} tiles membutuhkan waktu unduh lebih lama. "
                "Disarankan menurunkan zoom level jika area sangat luas."
            )
        else:
            self.lbl_warning.setText("")

    def _on_accept(self):
        z = self.selected_zoom
        min_tx, max_tx, min_ty, max_ty = get_tile_bounds(self.rect, z)
        total_tiles = (max_tx - min_tx + 1) * (max_ty - min_ty + 1)

        if total_tiles > 400:
            reply = QMessageBox.question(
                self,
                "Konfirmasi Jumlah Tiles",
                f"Area ini memerlukan {total_tiles:,} tiles untuk diunduh.\n"
                f"Dengan mode paralel ({self.selected_threads} threads), estimasi waktu: ~{max(1, total_tiles // 8)} detik.\n\n"
                "Apakah Anda ingin melanjutkan?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if reply != QMessageBox.Yes:
                return

        self.accept()

    @property
    def selected_zoom(self) -> int:
        return self.zoom_combo.currentData()

    @property
    def selected_threads(self) -> int:
        return self.thread_combo.currentData()

    @property
    def should_vectorize(self) -> bool:
        return self.cb_vectorize.isChecked()

    @property
    def include_basemap(self) -> bool:
        return self.cb_basemap.isChecked()

    @property
    def auto_add_layer(self) -> bool:
        return self.cb_add_layer.isChecked()


# ─────────────────────────────────────────────────────────────────────────────
# Helper Konversi QImage ke RGBA NumPy Array (Aman Tanpa sip.voidptr)
# ─────────────────────────────────────────────────────────────────────────────

def qimage_to_rgba_array(qimage: QImage):
    """
    Konversi QImage ke numpy array berdimensi (height, width, 4) uint8 RGBA.
    Menggunakan in-memory QBuffer dan GDAL Virtual Filesystem (/vsimem/)
    yang 100% aman dan kompatibel di semua versi PyQt5, PyQt6, dan Python 3.
    """
    import uuid
    import numpy as np
    from osgeo import gdal

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    qimage.save(buf, "PNG")
    png_bytes = bytes(ba.data())
    buf.close()

    mem_vsi = f"/vsimem/qimg_{uuid.uuid4().hex}.png"
    gdal.FileFromMemBuffer(mem_vsi, png_bytes)
    try:
        ds = gdal.Open(mem_vsi)
        if ds is not None:
            raw = ds.ReadAsArray()
            ds = None
            if raw is not None:
                if raw.ndim == 3:
                    # (bands, height, width) -> (height, width, bands)
                    arr = np.transpose(raw, (1, 2, 0))
                    if arr.shape[2] == 3:
                        alpha = np.full((arr.shape[0], arr.shape[1], 1), 255, dtype=np.uint8)
                        arr = np.concatenate([arr, alpha], axis=2)
                    return arr
                elif raw.ndim == 2:
                    arr = np.stack([raw, raw, raw, np.full_like(raw, 255)], axis=2)
                    return arr
    except Exception as e:
        QgsMessageLog.logMessage(f"Gagal konversi QImage ke Array: {e}", "BHUMI ATR/BPN", Qgis.Warning)
    finally:
        gdal.Unlink(mem_vsi)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Writer GeoTIFF & World File
# ─────────────────────────────────────────────────────────────────────────────

def save_as_geotiff(
    filepath: str,
    qimage: QImage,
    rect_3857: QgsRectangle,
) -> bool:
    """
    Simpan QImage ke format GeoTIFF (.tif) lengkap dengan geotransform EPSG:3857
    dan world file (.tfw + .prj) sebagai pendamping.
    """
    width = qimage.width()
    height = qimage.height()

    res_x = rect_3857.width() / float(width)
    res_y = rect_3857.height() / float(height)

    # 1. Coba menggunakan GDAL dengan compression DEFLATE & GeoTransform
    try:
        from osgeo import gdal, osr

        arr = qimage_to_rgba_array(qimage)
        if arr is not None:
            driver = gdal.GetDriverByName("GTiff")
            if driver is not None:
                ds = driver.Create(
                    filepath,
                    width,
                    height,
                    4,
                    gdal.GDT_Byte,
                    ["COMPRESS=DEFLATE", "TILED=YES"],
                )
                if ds is not None:
                    geotransform = [
                        rect_3857.xMinimum(),
                        res_x,
                        0.0,
                        rect_3857.yMaximum(),
                        0.0,
                        -res_y,
                    ]
                    ds.SetGeoTransform(geotransform)

                    srs = osr.SpatialReference()
                    srs.ImportFromEPSG(3857)
                    ds.SetProjection(srs.ExportToWkt())

                    for band_idx in range(4):
                        band = ds.GetRasterBand(band_idx + 1)
                        band.WriteArray(arr[:, :, band_idx])

                    ds.FlushCache()
                    ds = None

                    _write_world_files(filepath, rect_3857, res_x, res_y)
                    return True
    except Exception as e:
        QgsMessageLog.logMessage(f"GeoTIFF GDAL export warning: {e}", "BHUMI ATR/BPN", Qgis.Warning)

    # 2. Fallback jika GDAL direct creation bermasalah
    success = qimage.save(filepath, "TIFF")
    if not success:
        if not filepath.lower().endswith(".png"):
            filepath_png = os.path.splitext(filepath)[0] + ".png"
            qimage.save(filepath_png, "PNG")
            filepath = filepath_png

    _write_world_files(filepath, rect_3857, res_x, res_y)
    return True


def _write_world_files(filepath: str, rect_3857: QgsRectangle, res_x: float, res_y: float):
    """Tulis berkas pendamping world file (.tfw / .pgw) dan .prj."""
    base = os.path.splitext(filepath)[0]
    ext = os.path.splitext(filepath)[1].lower()

    world_ext = ".tfw" if "tif" in ext else ".pgw"
    world_path = base + world_ext
    prj_path = base + ".prj"

    try:
        center_x = rect_3857.xMinimum() + (res_x / 2.0)
        center_y = rect_3857.yMaximum() - (res_y / 2.0)

        with open(world_path, "w", encoding="utf-8") as f:
            f.write(f"{res_x:.10f}\n")
            f.write("0.0000000000\n")
            f.write("0.0000000000\n")
            f.write(f"{-res_y:.10f}\n")
            f.write(f"{center_x:.10f}\n")
            f.write(f"{center_y:.10f}\n")

        with open(prj_path, "w", encoding="utf-8") as f:
            f.write(EPSG_3857_WKT)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Mesin Klasifikasi Warna & Vektorisasi Berbasis Legenda BHUMI
# ─────────────────────────────────────────────────────────────────────────────

def _classify_polygon_by_color(
    geom,
    rgba_arr,
    rect_3857: QgsRectangle,
    res_x: float,
    res_y: float,
    img_w: int,
    img_h: int,
) -> tuple[str, str, str]:
    """
    Klasifikasikan poligon berdasarkan sampel warna piksel boundary garis batas & interior.
    :returns: (status, kode_warna, is_overlap)
    """
    import numpy as np

    try:
        # 1. Sampel warna pada garis batas keliling (Boundary stroke)
        boundary = geom.Boundary()
        boundary_points = []
        if boundary:
            pt_count = boundary.GetPointCount()
            step = max(1, pt_count // 60)
            for i in range(0, pt_count, step):
                pt = boundary.GetPoint(i)
                px = int((pt[0] - rect_3857.xMinimum()) / res_x)
                py = int((rect_3857.yMaximum() - pt[1]) / res_y)
                px = max(0, min(px, img_w - 1))
                py = max(0, min(py, img_h - 1))
                boundary_points.append(rgba_arr[py, px])

        sr, sg, sb, sa = 0, 0, 0, 0
        if boundary_points:
            b_arr = np.array(boundary_points)
            stroke_hits = b_arr[b_arr[:, 3] > 30]
            if len(stroke_hits) > 0:
                median_stroke = np.median(stroke_hits, axis=0)
                sr, sg, sb, sa = median_stroke[0], median_stroke[1], median_stroke[2], median_stroke[3]

        # 2. Sampel warna interior di sekitar centroid
        centroid = geom.Centroid()
        ir, ig, ib, ia = 0, 0, 0, 0
        if centroid is not None:
            cx = centroid.GetX()
            cy = centroid.GetY()
            cpx = max(0, min(int((cx - rect_3857.xMinimum()) / res_x), img_w - 1))
            cpy = max(0, min(int((rect_3857.yMaximum() - cy) / res_y), img_h - 1))

            samples = []
            for dx in range(-2, 3):
                for dy in range(-2, 3):
                    sx = max(0, min(cpx + dx, img_w - 1))
                    sy = max(0, min(cpy + dy, img_h - 1))
                    samples.append(rgba_arr[sy, sx])
            samples_arr = np.array(samples)
            median_rgb = np.median(samples_arr[:, :3], axis=0)
            ir, ig, ib = median_rgb[0], median_rgb[1], median_rgb[2]

        # ── Evaluasi Kategori Legenda BHUMI ATR/BPN ──
        # A. Hak Pengelolaan (Garis / Area Merah Pekat)
        if (sr > 190 and sg < 70 and sb < 70) or (ir > 190 and ig < 70 and ib < 70):
            return "Hak Pengelolaan", "#FF0000", "Tidak"

        # B. Belum Terdaftar (Garis / Area Hijau)
        if (sg > sr + 15 and sg > sb + 15) or (sg > 140 and sr < 160 and sb < 160) or (ig > ir + 15 and ig > ib + 15):
            return "Belum Terdaftar", "#81C784", "Tidak"

        # C. Unsur Geografis (Garis / Area Biru)
        if (sb > sr + 15 and sb > sg + 10) or (sb > 150 and sr < 140) or (ib > ir + 15 and ib > ig + 10):
            return "Unsur Geografis", "#6C8EBF", "Tidak"

        # D. Bidang Terdaftar (Kuning Standar ATR/BPN: R > 190, G > 160)
        if (ir > 190 and ig > 160) or (sr > 190 and sg > 160):
            return "Bidang Terdaftar", "#F5C258", "Tidak"

        # E. Kawasan Terdaftar / Tumpang Tindih (Kuning Emas Pekat / Oranye Coklat)
        if (sr > 200 and 110 <= sg <= 185 and sb < 80) or (ir > 190 and 110 <= ig <= 185 and ib < 80):
            return "Kawasan Terdaftar", "#E5A024", "Ya"

    except Exception:
        pass

    return "Bidang Terdaftar", "#F5C258", "Tidak"


def apply_bhumi_symbology(layer: QgsVectorLayer):
    """
    Pasang simbologi otomatis (Categorized Renderer) pada layer vektor
    sesuai warna resmi legenda BHUMI ATR/BPN.
    """
    try:
        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsFillSymbol,
            QgsRendererCategory,
        )

        categories_def = [
            ("Bidang Terdaftar", QColor(245, 194, 88, 140), QColor(212, 136, 6, 255), 0.5),
            ("Belum Terdaftar", QColor(129, 199, 132, 150), QColor(56, 142, 60, 255), 0.5),
            ("Hak Pengelolaan", QColor(255, 0, 0, 40), QColor(255, 0, 0, 255), 1.2),
            ("Kawasan Terdaftar", QColor(229, 160, 36, 160), QColor(212, 107, 8, 255), 0.7),
            ("Unsur Geografis", QColor(108, 142, 191, 140), QColor(29, 57, 196, 255), 0.5),
            ("Tumpang Tindih", QColor(255, 140, 0, 180), QColor(178, 34, 34, 255), 0.9),
        ]

        categories = []
        for val, fill_col, stroke_col, stroke_w in categories_def:
            symbol = QgsFillSymbol.createSimple({
                "color": f"{fill_col.red()},{fill_col.green()},{fill_col.blue()},{fill_col.alpha()}",
                "outline_color": f"{stroke_col.red()},{stroke_col.green()},{stroke_col.blue()},{stroke_col.alpha()}",
                "outline_width": str(stroke_w),
            })
            cat = QgsRendererCategory(val, symbol, val)
            categories.append(cat)

        renderer = QgsCategorizedSymbolRenderer("status", categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
    except Exception as e:
        QgsMessageLog.logMessage(f"Gagal memasang simbologi: {e}", "BHUMI ATR/BPN", Qgis.Warning)


def vectorize_persil_to_vector_files(
    qimage: QImage,
    rect_3857: QgsRectangle,
    base_filepath: str,
    min_area_m2: float = 8.0,
    simplify_tolerance: float = 0.2,
) -> tuple[Optional[str], Optional[str], int]:
    """
    Ekstrak bidang tanah dari raster qimage menjadi layer Poligon vektor tertutup.
    Menyimpan sekaligus ke format ESRI Shapefile (.shp) dan GeoJSON (.geojson).
    """
    width = qimage.width()
    height = qimage.height()
    if width <= 0 or height <= 0:
        return None, None, 0

    res_x = rect_3857.width() / float(width)
    res_y = rect_3857.height() / float(height)

    try:
        import numpy as np
        from osgeo import gdal, ogr, osr

        arr = qimage_to_rgba_array(qimage)
        if arr is None:
            QgsMessageLog.logMessage("Array piksel QImage tidak dapat diekstrak.", "BHUMI ATR/BPN", Qgis.Critical)
            return None, None, 0

        alpha = arr[:, :, 3]
        mask_binary = (alpha > 30).astype(np.uint8)

        # Buat in-memory raster untuk Polygonize
        mem_driver = gdal.GetDriverByName("MEM")
        src_ds = mem_driver.Create("", width, height, 1, gdal.GDT_Byte)
        src_ds.SetGeoTransform([
            rect_3857.xMinimum(), res_x, 0.0,
            rect_3857.yMaximum(), 0.0, -res_y
        ])
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(3857)
        src_ds.SetProjection(srs.ExportToWkt())

        band = src_ds.GetRasterBand(1)
        band.WriteArray(mask_binary)

        # Buat in-memory vector layer penampung hasil Polygonize
        ogr_driver = ogr.GetDriverByName("MEM")
        if ogr_driver is None:
            ogr_driver = ogr.GetDriverByName("Memory")
        dst_ds = ogr_driver.CreateDataSource("mem_polygons")
        dst_layer = dst_ds.CreateLayer("polygons", srs=srs, geom_type=ogr.wkbPolygon)

        field_dn = ogr.FieldDefn("DN", ogr.OFTInteger)
        dst_layer.CreateField(field_dn)

        gdal.Polygonize(band, None, dst_layer, 0, [], callback=None)

        valid_polygons = []
        outer_bbox_area = rect_3857.width() * rect_3857.height()

        for feat in dst_layer:
            dn_val = feat.GetField("DN")
            # Ambil DN == 1 (area bidang tanah yang memiliki piksel warna / non-transparan)
            if dn_val != 1:
                continue

            geom = feat.GetGeometryRef()
            if geom is None:
                continue

            area = geom.GetArea()
            if area < min_area_m2 or area >= (outer_bbox_area * 0.999):
                continue

            # Klasifikasikan status & warna berdasarkan legenda resmi BHUMI
            status, kode_warna, is_overlap = _classify_polygon_by_color(
                geom, arr, rect_3857, res_x, res_y, width, height
            )

            if simplify_tolerance > 0:
                simplified_geom = geom.Simplify(simplify_tolerance)
                if simplified_geom and not simplified_geom.IsEmpty():
                    geom = simplified_geom

            perimeter = geom.Boundary().Length() if geom.Boundary() else 0.0
            wkt_str = geom.ExportToWkt()

            valid_polygons.append({
                "wkt": wkt_str,
                "status": status,
                "is_overlap": is_overlap,
                "kode_warna": kode_warna,
                "area_m2": round(area, 2),
                "perimeter_m": round(perimeter, 2),
            })

        src_ds = None
        dst_ds = None

        if not valid_polygons:
            QgsMessageLog.logMessage("Tidak ditemukan poligon bidang tanah yang valid pada area ini.", "BHUMI ATR/BPN", Qgis.Info)
            return None, None, 0

        base_no_ext = os.path.splitext(base_filepath)[0]
        shp_path = f"{base_no_ext}_poligon.shp"
        geojson_path = f"{base_no_ext}_poligon.geojson"

        # Buat temporary QgsVectorLayer untuk diekspor
        vl = QgsVectorLayer("Polygon?crs=EPSG:3857", "persil_temp", "memory")
        pr = vl.dataProvider()

        pr.addAttributes([
            QgsField("id", QVariant.Int),
            QgsField("status", QVariant.String),
            QgsField("is_overlap", QVariant.String),
            QgsField("luas_m2", QVariant.Double),
            QgsField("keliling_m", QVariant.Double),
            QgsField("kode_warna", QVariant.String),
            QgsField("sumber", QVariant.String),
        ])
        vl.updateFields()

        qgs_features = []
        for idx, p in enumerate(valid_polygons, start=1):
            f = QgsFeature(vl.fields())
            geom_qgs = QgsGeometry.fromWkt(p["wkt"])
            f.setGeometry(geom_qgs)
            f.setAttribute("id", idx)
            f.setAttribute("status", p["status"])
            f.setAttribute("is_overlap", p["is_overlap"])
            f.setAttribute("luas_m2", p["area_m2"])
            f.setAttribute("keliling_m", p["perimeter_m"])
            f.setAttribute("kode_warna", p["kode_warna"])
            f.setAttribute("sumber", "BHUMI ATR/BPN")
            qgs_features.append(f)

        pr.addFeatures(qgs_features)
        vl.updateExtents()

        # 1. Simpan ke ESRI Shapefile (.shp)
        options_shp = QgsVectorFileWriter.SaveVectorOptions()
        options_shp.driverName = "ESRI Shapefile"
        options_shp.fileEncoding = "UTF-8"
        QgsVectorFileWriter.writeAsVectorFormatV3(
            vl, shp_path, QgsProject.instance().transformContext(), options_shp
        )

        # 2. Simpan ke GeoJSON (.geojson)
        options_geojson = QgsVectorFileWriter.SaveVectorOptions()
        options_geojson.driverName = "GeoJSON"
        options_geojson.fileEncoding = "UTF-8"
        QgsVectorFileWriter.writeAsVectorFormatV3(
            vl, geojson_path, QgsProject.instance().transformContext(), options_geojson
        )

        QgsMessageLog.logMessage(
            f"Sukses vektorisasi {len(valid_polygons)} bidang tanah: {shp_path} & {geojson_path}",
            "BHUMI ATR/BPN",
            Qgis.Success,
        )

        return shp_path, geojson_path, len(valid_polygons)

    except Exception as e:
        QgsMessageLog.logMessage(f"Kesalahan proses vektorisasi: {e}", "BHUMI ATR/BPN", Qgis.Critical)
        return None, None, 0


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Threaded Parallel Download & Stitching
# ─────────────────────────────────────────────────────────────────────────────

def stitch_persil_tiles(
    rect_3857: QgsRectangle,
    zoom: int,
    thread_count: int = 16,
    progress_dlg: Optional[QProgressDialog] = None,
) -> Optional[QImage]:
    """
    Unduh semua tiles pada zoom terpilih secara PARALEL (16-24 threads)
    dan jahit ke satu QImage besar, lalu crop sesuai exact BBOX rect_3857.
    """
    min_tx, max_tx, min_ty, max_ty = get_tile_bounds(rect_3857, zoom)
    tiles_x = max_tx - min_tx + 1
    tiles_y = max_ty - min_ty + 1
    total_tiles = tiles_x * tiles_y

    res = INITIAL_RES / (2 ** zoom)

    # Ukuran mosaik utuh
    mosaic_w = tiles_x * 256
    mosaic_h = tiles_y * 256

    mosaic_img = QImage(mosaic_w, mosaic_h, QImage.Format_ARGB32_Premultiplied)
    mosaic_img.fill(0)  # Transparan

    painter = QPainter(mosaic_img)
    painter.setRenderHint(QPainter.Antialiasing, False)

    # Kumpulkan daftar koordinat tile
    tasks: list[tuple[int, int]] = []
    for ty in range(min_ty, max_ty + 1):
        for tx in range(min_tx, max_tx + 1):
            tasks.append((tx, ty))

    if progress_dlg:
        progress_dlg.setMaximum(total_tiles)
        progress_dlg.setValue(0)
        progress_dlg.setLabelText(f"🚀 Memulai download paralel ({thread_count} threads)...")
        QApplication.processEvents()

    # Siapkan HTTP session dengan connection pooling besar
    session = _create_export_session()

    def _fetch_single_tile(coords: tuple[int, int]) -> tuple[int, int, Optional[bytes]]:
        tx, ty = coords
        cached = TILE_CACHE.get("bhumi_persil", zoom, tx, ty)
        if cached:
            return tx, ty, cached

        try:
            minx, miny, maxx, maxy = xyz_to_bbox(zoom, tx, ty)
            params = {
                "SERVICE": "WMS",
                "VERSION": "1.3.0",
                "REQUEST": "GetMap",
                "FORMAT": "image/png",
                "TRANSPARENT": "true",
                "LAYERS": "bhumi_persil",
                "STYLES": "",
                "SRS": "EPSG:3857",
                "CRS": "EPSG:3857",
                "TILED": "true",
                "WIDTH": "512",
                "HEIGHT": "512",
                "BBOX": f"{minx},{miny},{maxx},{maxy}",
            }
            resp = session.get(BHUMI_WMS_URL, params=params, timeout=PROXY_TIMEOUT)
            if (
                resp.status_code == 200
                and resp.content
                and resp.content.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                TILE_CACHE.put("bhumi_persil", zoom, tx, ty, resp.content)
                return tx, ty, resp.content
        except Exception:
            pass

        return tx, ty, None

    completed_count = 0
    t_start = time.time()

    workers = min(thread_count, total_tiles)
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="TurboExport") as executor:
        futures = {executor.submit(_fetch_single_tile, t): t for t in tasks}

        for future in as_completed(futures):
            if progress_dlg and progress_dlg.wasCanceled():
                painter.end()
                return None

            try:
                tx, ty, data = future.result()
                if data:
                    tile_qimg = QImage()
                    if tile_qimg.loadFromData(data) and not tile_qimg.isNull():
                        if tile_qimg.width() != 256 or tile_qimg.height() != 256:
                            tile_qimg = tile_qimg.scaled(
                                256, 256, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
                            )
                        px = (tx - min_tx) * 256
                        py = (ty - min_ty) * 256
                        painter.drawImage(px, py, tile_qimg)
            except Exception:
                pass

            completed_count += 1
            if progress_dlg:
                elapsed = max(0.1, time.time() - t_start)
                speed = completed_count / elapsed
                remaining = max(0, int((total_tiles - completed_count) / max(0.5, speed)))
                progress_dlg.setValue(completed_count)
                progress_dlg.setLabelText(
                    f"⚡ Mengunduh {completed_count}/{total_tiles} tiles ({speed:.1f} tiles/dtk)\n"
                    f"Estimasi sisa waktu: ~{remaining} detik..."
                )
                QApplication.processEvents()

    painter.end()

    mosaic_min_x = -ORIGIN_SHIFT + min_tx * 256.0 * res
    mosaic_max_y = ORIGIN_SHIFT - min_ty * 256.0 * res

    crop_x = int(math.floor((rect_3857.xMinimum() - mosaic_min_x) / res))
    crop_y = int(math.floor((mosaic_max_y - rect_3857.yMaximum()) / res))
    crop_w = int(math.ceil(rect_3857.width() / res))
    crop_h = int(math.ceil(rect_3857.height() / res))

    crop_x = max(0, min(crop_x, mosaic_w - 1))
    crop_y = max(0, min(crop_y, mosaic_h - 1))
    crop_w = max(1, min(crop_w, mosaic_w - crop_x))
    crop_h = max(1, min(crop_h, mosaic_h - crop_y))

    cropped_img = mosaic_img.copy(crop_x, crop_y, crop_w, crop_h)
    return cropped_img


def render_canvas_basemap(
    canvas: QgsMapCanvas,
    rect_3857: QgsRectangle,
    output_width: int,
    output_height: int,
) -> QImage:
    """
    Render layer non-BHUMI (seperti basemap citra satelit Google / OSM)
    pada area rect_3857 dengan ukuran piksel yang sama.
    """
    settings = canvas.mapSettings()
    settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    settings.setExtent(rect_3857)
    settings.setOutputSize(QSize(output_width, output_height))

    all_layers = canvas.layers()
    basemap_layers = [l for l in all_layers if "bhumi" not in l.name().lower() and "persil" not in l.name().lower()]
    settings.setLayers(basemap_layers)

    img = QImage(output_width, output_height, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)

    job = QgsMapRendererCustomPainterJob(settings, painter)
    job.start()
    job.waitForFinished()
    painter.end()

    return img


# ─────────────────────────────────────────────────────────────────────────────
# Map Tool: Rectangle Area Selection Tool
# ─────────────────────────────────────────────────────────────────────────────

class RectangleAreaTool(QgsMapTool):
    """
    Map Tool interaktif untuk memilih area kotak persegi (Rectangle) di kanvas.
    Menampilkan RubberBand transparan biru saat user klik & geser kursor.
    """

    def __init__(self, iface, canvas: QgsMapCanvas):
        super().__init__(canvas)
        self.iface = iface
        self.canvas = canvas
        self.start_point: Optional[QgsPointXY] = None
        self.end_point: Optional[QgsPointXY] = None
        self.is_dragging = False

        self.setCursor(QCursor(Qt.CrossCursor))

        self.rubber_band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rubber_band.setColor(QColor(0, 150, 255, 60))
        self.rubber_band.setStrokeColor(QColor(0, 120, 230, 220))
        self.rubber_band.setWidth(2)

        self._epsg3857 = QgsCoordinateReferenceSystem("EPSG:3857")

    def canvasPressEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.start_point = self.toMapCoordinates(event.pos())
            self.end_point = self.start_point
            self.is_dragging = True
            self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
            self._update_rubber_band()
        elif event.button() == Qt.RightButton:
            self._deactivate_tool()

    def canvasMoveEvent(self, event):  # noqa: N802
        if self.is_dragging and self.start_point:
            self.end_point = self.toMapCoordinates(event.pos())
            self._update_rubber_band()

    def canvasReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton and self.is_dragging:
            self.is_dragging = False
            self.end_point = self.toMapCoordinates(event.pos())
            self._update_rubber_band()

            if not self.start_point or not self.end_point:
                self._deactivate_tool()
                return

            canvas_crs = self.canvas.mapSettings().destinationCrs()
            pt1 = self.start_point
            pt2 = self.end_point

            if canvas_crs != self._epsg3857:
                xform = QgsCoordinateTransform(canvas_crs, self._epsg3857, QgsProject.instance())
                pt1 = xform.transform(pt1)
                pt2 = xform.transform(pt2)

            rect_3857 = QgsRectangle(
                min(pt1.x(), pt2.x()),
                min(pt1.y(), pt2.y()),
                max(pt1.x(), pt2.x()),
                max(pt1.y(), pt2.y()),
            )

            if rect_3857.width() < 20 or rect_3857.height() < 20:
                self.iface.messageBar().pushWarning(
                    "BHUMI Export",
                    "Area yang dipilih terlalu kecil. Silakan klik dan tarik kotak yang lebih luas.",
                )
                self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
                return

            self._process_export(rect_3857)
            self._deactivate_tool()

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._deactivate_tool()

    def _update_rubber_band(self):
        if not self.start_point or not self.end_point:
            return
        rect = QgsRectangle(self.start_point, self.end_point)
        geom = QgsGeometry.fromRect(rect)
        self.rubber_band.setToGeometry(geom, None)
        self.rubber_band.show()

    def _deactivate_tool(self):
        self.rubber_band.reset(QgsWkbTypes.PolygonGeometry)
        self.is_dragging = False
        self.start_point = None
        self.end_point = None
        self.canvas.unsetMapTool(self)

    def _process_export(self, rect_3857: QgsRectangle):
        """Buka dialog konfigurasi export dan jalankan stitching & vectorization."""
        dlg = ExportAreaDialog(rect_3857, parent=self.iface.mainWindow())
        if dlg.exec_() != QDialog.Accepted:
            return

        zoom = dlg.selected_zoom
        threads = dlg.selected_threads
        should_vec = dlg.should_vectorize
        include_basemap = dlg.include_basemap
        auto_add = dlg.auto_add_layer

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_filename = f"bhumi_persil_z{zoom}_{timestamp}.tif"
        default_path = os.path.join(os.path.expanduser("~"), "Desktop", default_filename)

        filepath, selected_filter = QFileDialog.getSaveFileName(
            parent=self.iface.mainWindow(),
            caption="Tentukan Lokasi & Nama Berkas Export",
            directory=default_path,
            filter="GeoTIFF (*.tif *.tiff);;PNG Image + World File (*.png);;All Files (*)",
        )

        if not filepath:
            return

        if not filepath.lower().endswith((".tif", ".tiff", ".png")):
            filepath += ".tif"

        # Dialog Progress Bar
        progress = QProgressDialog(
            "Mempersiapkan pengunduhan tiles paralel...",
            "Batal",
            0,
            100,
            self.iface.mainWindow(),
        )
        progress.setWindowTitle("Memproses Export Area BHUMI")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()

        # 1. Unduh paralel dan jahit garis persil
        persil_img = stitch_persil_tiles(rect_3857, zoom, thread_count=threads, progress_dlg=progress)
        if persil_img is None:
            if not progress.wasCanceled():
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Export Gagal",
                    "Gagal mengunduh tiles bidang tanah dari server BHUMI.",
                )
            return

        # 2. Vektorisasi Poligon Otomatis (.shp & .geojson)
        shp_file = None
        geojson_file = None
        poly_count = 0

        if should_vec:
            progress.setLabelText("📐 Mengekstrak Vektor Poligon Bidang Tanah (.shp & .geojson)...")
            QApplication.processEvents()
            shp_file, geojson_file, poly_count = vectorize_persil_to_vector_files(
                persil_img, rect_3857, filepath, min_area_m2=8.0, simplify_tolerance=0.2
            )

        # 3. Jika opsi basemap dipilih, gabungkan dengan basemap kanvas untuk raster
        final_raster_img = persil_img
        if include_basemap:
            progress.setLabelText("Menggabungkan dengan Basemap Kanvas...")
            QApplication.processEvents()
            basemap_img = render_canvas_basemap(
                self.canvas, rect_3857, persil_img.width(), persil_img.height()
            )

            comp = QImage(persil_img.size(), QImage.Format_ARGB32_Premultiplied)
            comp.fill(0)
            painter = QPainter(comp)
            painter.drawImage(0, 0, basemap_img)
            painter.drawImage(0, 0, persil_img)
            painter.end()
            final_raster_img = comp

        progress.setLabelText("Menyimpan metadata Georeference GeoTIFF...")
        QApplication.processEvents()

        # 4. Simpan ke GeoTIFF
        success_raster = save_as_geotiff(filepath, final_raster_img, rect_3857)
        progress.close()

        if success_raster:
            vector_info = ""
            if should_vec and shp_file and poly_count > 0:
                vector_info = (
                    f"\n\n📐 Hasil Vektor Poligon ({poly_count:,} bidang tanah):\n"
                    f" • Shapefile: {shp_file}\n"
                    f" • GeoJSON  : {geojson_file}"
                )

            msg = (
                f"✔ Export Berhasil Selesai!\n\n"
                f"📷 GeoTIFF Raster:\n"
                f" • Lokasi: {filepath}\n"
                f" • Dimensi: {final_raster_img.width():,} × {final_raster_img.height():,} px (Zoom {zoom})\n"
                f" • Proyeksi: EPSG:3857 (WGS 84 / Pseudo-Mercator)"
                f"{vector_info}"
            )
            self.iface.messageBar().pushSuccess("BHUMI Export", "GeoTIFF & Vektor berhasil dibuat!")

            # 5. Otomatis tambahkan layer ke QGIS jika dipilih
            if auto_add:
                # Tambahkan layer GeoTIFF
                layer_name_r = os.path.splitext(os.path.basename(filepath))[0]
                raster_layer = QgsRasterLayer(filepath, layer_name_r, "gdal")
                if raster_layer.isValid():
                    QgsProject.instance().addMapLayer(raster_layer)

                # Tambahkan layer Vektor Poligon Shapefile
                if should_vec and shp_file and os.path.isfile(shp_file):
                    layer_name_v = os.path.splitext(os.path.basename(shp_file))[0]
                    vector_layer = QgsVectorLayer(shp_file, layer_name_v, "ogr")
                    if vector_layer.isValid():
                        apply_bhumi_symbology(vector_layer)
                        QgsProject.instance().addMapLayer(vector_layer)

            QMessageBox.information(
                self.iface.mainWindow(),
                "Export Selesai",
                msg,
            )
        else:
            QMessageBox.critical(
                self.iface.mainWindow(),
                "Export Gagal",
                f"Gagal menulis berkas GeoTIFF ke:\n{filepath}",
            )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point pemanggil dari toolbar plugin
# ─────────────────────────────────────────────────────────────────────────────

def start_rectangle_export(iface, canvas: QgsMapCanvas) -> RectangleAreaTool:
    """
    Aktifkan Map Tool Rectangle Selection untuk memulai proses export GeoTIFF.
    """
    tool = RectangleAreaTool(iface, canvas)
    canvas.setMapTool(tool)
    iface.messageBar().pushInfo(
        "BHUMI Export",
        "Klik dan tarik kotak (drag rectangle) di atas kanvas peta pada area yang ingin diexport.",
    )
    return tool
