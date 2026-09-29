# -*- coding: utf-8 -*-
"""
tile_cache.py
In-memory tile cache dengan TTL, overview generator, dan prefetch scheduler.

Fitur:
  - Thread-safe RAM cache (dict) untuk semua layer BHUMI
  - TTL 24 jam per tile
  - get_overview(): susun mosaic dari tile zoom tinggi yang di-cache
    agar tetap tampil saat zoom out
  - Statistik (jumlah tile, estimasi RAM usage)
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Entry cache
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CacheEntry:
    data: bytes          # bytes PNG tile
    layer_id: str
    z: int
    x: int
    y: int
    timestamp: float = field(default_factory=time.time)


# ─────────────────────────────────────────────────────────────────────────────
# Tile Cache utama
# ─────────────────────────────────────────────────────────────────────────────

class TileCache:
    """
    RAM tile cache global untuk semua layer BHUMI.

    Key: (layer_id, z, x, y)
    Value: CacheEntry dengan bytes PNG + timestamp

    Thread-safe via RLock. Tidak ada batas ukuran (unlimited RAM).
    """

    DEFAULT_TTL: int = 24 * 3600   # 24 jam dalam detik

    def __init__(self, ttl: int = DEFAULT_TTL):
        self.ttl = ttl
        self._cache: dict[tuple, CacheEntry] = {}
        self._lock = threading.RLock()

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def get(self, layer_id: str, z: int, x: int, y: int) -> Optional[bytes]:
        """
        Ambil tile dari cache.
        Return None jika tidak ada atau sudah expired (TTL habis).
        """
        key = (layer_id, z, x, y)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if time.time() - entry.timestamp > self.ttl:
                del self._cache[key]
                return None
            return entry.data

    def put(self, layer_id: str, z: int, x: int, y: int, data: bytes) -> None:
        """Simpan tile ke cache (overwrite jika sudah ada)."""
        with self._lock:
            self._cache[(layer_id, z, x, y)] = CacheEntry(
                data=data, layer_id=layer_id, z=z, x=x, y=y,
            )

    def has(self, layer_id: str, z: int, x: int, y: int) -> bool:
        """True jika tile ada di cache dan belum expired."""
        return self.get(layer_id, z, x, y) is not None

    def clear(self) -> int:
        """Hapus seluruh cache. Return jumlah tile yang dihapus."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            return count

    def evict_expired(self) -> int:
        """Hapus semua entry yang sudah melewati TTL. Return jumlah entry dihapus."""
        now = time.time()
        with self._lock:
            expired = [k for k, v in self._cache.items()
                       if now - v.timestamp > self.ttl]
            for k in expired:
                del self._cache[k]
            return len(expired)

    # ── Statistik ─────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """
        Kembalikan dict statistik cache:
          count    : jumlah tile
          size_mb  : estimasi RAM usage dalam MB
          by_zoom  : {z: jumlah tile} per zoom level
        """
        now = time.time()
        with self._lock:
            total = 0
            size_bytes = 0
            by_zoom: dict[int, int] = {}
            for entry in self._cache.values():
                if now - entry.timestamp <= self.ttl:   # hanya yang masih valid
                    total += 1
                    size_bytes += len(entry.data)
                    by_zoom[entry.z] = by_zoom.get(entry.z, 0) + 1
            return {
                "count": total,
                "size_mb": round(size_bytes / 1024 / 1024, 2),
                "by_zoom": dict(sorted(by_zoom.items())),
            }

    # ── Overview tile (zoom-out persistence) ─────────────────────────────────

    def get_overview(
        self,
        layer_id: str,
        z: int,
        x: int,
        y: int,
        zmin: int,
    ) -> Optional[bytes]:
        """
        Buat tile overview 256×256 dari tile-tile cached di zoom lebih tinggi.

        Dipanggil ketika z < zmin (zoom out dari area yang sudah di-browse).
        Mencari tile cache di zoom (zmin, zmin+1, ..., zmin+3) yang secara
        geografis cover area tile (z, x, y), lalu menyusunnya menjadi mosaic.

        Strategi pencarian zoom sumber:
          - Mulai dari zmin (paling detail → mosaic paling tajam)
          - Jika dz > 4 (lebih dari 16×16 sub-tile), terlalu kecil → skip
          - Jika tidak ada tile cached di zoom tsb → coba zoom lebih tinggi

        :returns: PNG bytes mosaic 256×256, atau None jika tidak ada cache sama sekali
        """
        # Batasi pencarian: coba dari zmin s/d zmin+3
        for source_z in range(zmin, min(zmin + 4, 22)):
            dz = source_z - z
            if dz <= 0:
                continue

            tile_count = 2 ** dz    # jumlah sub-tile per sisi

            # Terlalu banyak sub-tile → gambar terlalu kecil, skip
            if tile_count > 16:
                continue

            x_start = x * tile_count
            x_end   = (x + 1) * tile_count
            y_start = y * tile_count
            y_end   = (y + 1) * tile_count

            # Cek ada setidaknya 1 tile valid di range ini
            if not self._has_any_in_range(layer_id, source_z, x_start, x_end, y_start, y_end):
                continue

            # Susun mosaic
            mosaic = self._build_mosaic(
                layer_id, source_z,
                x_start, x_end,
                y_start, y_end,
                tile_count,
            )
            if mosaic is not None:
                return mosaic

        return None

    def _has_any_in_range(
        self,
        layer_id: str,
        z: int,
        x_start: int, x_end: int,
        y_start: int, y_end: int,
    ) -> bool:
        """Return True jika ada minimal 1 tile valid dalam range yang diberikan."""
        now = time.time()
        with self._lock:
            for tx in range(x_start, x_end):
                for ty in range(y_start, y_end):
                    entry = self._cache.get((layer_id, z, tx, ty))
                    if entry and (now - entry.timestamp <= self.ttl):
                        return True
        return False

    def _build_mosaic(
        self,
        layer_id: str,
        source_z: int,
        x_start: int, x_end: int,
        y_start: int, y_end: int,
        tile_count: int,
    ) -> Optional[bytes]:
        """
        Susun tile-tile source_z menjadi satu gambar 256×256.

        Menggunakan Qt QImage + QPainter (selalu tersedia di QGIS).
        Setiap sub-tile di-scale ke (256 // tile_count) piksel lalu digambar
        pada posisi yang sesuai dalam output image.
        """
        try:
            # Import lazy — Qt tersedia di environment QGIS
            from qgis.PyQt.QtCore import QBuffer, QByteArray, Qt
            from qgis.PyQt.QtGui import QImage, QPainter

            out_size  = 256
            cell_size = max(1, out_size // tile_count)

            # Buat canvas output transparan
            output = QImage(out_size, out_size, QImage.Format_ARGB32_Premultiplied)
            output.fill(0)

            painter = QPainter(output)

            drawn = 0
            now   = time.time()

            with self._lock:
                for i, tx in enumerate(range(x_start, x_end)):
                    for j, ty in enumerate(range(y_start, y_end)):
                        entry = self._cache.get((layer_id, source_z, tx, ty))
                        if entry is None:
                            continue
                        if now - entry.timestamp > self.ttl:
                            continue

                        tile_img = QImage()
                        if not tile_img.loadFromData(entry.data) or tile_img.isNull():
                            continue

                        # Scale tile ke ukuran cell (smooth, anti-aliased)
                        scaled = tile_img.scaled(
                            cell_size, cell_size,
                            Qt.IgnoreAspectRatio,
                            Qt.SmoothTransformation,
                        )

                        painter.drawImage(i * cell_size, j * cell_size, scaled)
                        drawn += 1

            painter.end()

            if drawn == 0:
                return None

            # Encode QImage → PNG bytes
            ba  = QByteArray()
            buf = QBuffer(ba)
            buf.open(QBuffer.WriteOnly)
            output.save(buf, "PNG")
            buf.close()

            result = bytes(ba)
            return result if result else None

        except Exception:
            return None


# ─────────────────────────────────────────────────────────────────────────────
# Singleton global — dipakai oleh proxy_server dan plugin
# ─────────────────────────────────────────────────────────────────────────────

TILE_CACHE: TileCache = TileCache()
