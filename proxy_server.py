# -*- coding: utf-8 -*-
"""
proxy_server.py
Local HTTP tile proxy server untuk BHUMI ATR/BPN — dengan tile cache & prefetch.

Fitur:
  - Menerima request XYZ tile dari QGIS (/{z}/{x}/{y}.png)
  - Mengkonversi koordinat XYZ → BBOX EPSG:3857
  - Meneruskan request ke server WMS BHUMI dengan header browser lengkap
  - RAM tile cache dengan TTL 24 jam (via tile_cache.TileCache)
  - Overview tile dari cache saat zoom out (z < zmin) — tampil tetap!
  - Pre-fetch agresif: 3×3 grid tetangga di background setelah tile berhasil
  - Menangani GetFeatureInfo (/getfeatureinfo?...) untuk klik kanvas
"""

from __future__ import annotations

import socketserver
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Konstanta proyeksi Web Mercator (EPSG:3857)
# ─────────────────────────────────────────────────────────────────────────────
ORIGIN_SHIFT: float = 20037508.342789244
INITIAL_RES: float  = 2 * ORIGIN_SHIFT / 256.0

# Endpoint BHUMI WMS
BHUMI_WMS_URL: str = "https://bhumi.atrbpn.go.id/mprx/service"

# Timeout: (connect_timeout, read_timeout)
PROXY_TIMEOUT: tuple[int, int] = (5, 12)

# Header wajib untuk bypass WAF BPN
BROWSER_HEADERS: dict = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer"        : "https://bhumi.atrbpn.go.id/",
    "Origin"         : "https://bhumi.atrbpn.go.id",
    "Accept"         : "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection"     : "keep-alive",
    "Sec-Fetch-Dest" : "image",
    "Sec-Fetch-Mode" : "no-cors",
    "Sec-Fetch-Site" : "same-origin",
}

# Minimal transparent 1×1 PNG
TRANSPARENT_PNG: bytes = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Session HTTP global (reuse koneksi TCP/TLS)
_session: requests.Session = requests.Session()
_session.headers.update(BROWSER_HEADERS)
_session.max_redirects = 3

# ─────────────────────────────────────────────────────────────────────────────
# Threaded HTTP Server
# KRITIS: tanpa ThreadingMixIn, server handle 1 request sekaligus → timeout!
# QGIS mengirim banyak tile request paralel, semua harus diproses bersamaan.
# ─────────────────────────────────────────────────────────────────────────────

class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """
    HTTPServer yang menangani setiap request di thread terpisah.
    Tanpa ini, semua tile request QGIS akan antri satu per satu
    dan sebagian besar akan timeout.
    """
    daemon_threads    = True   # thread mati otomatis saat server berhenti
    allow_reuse_address = True  # hindari "Address already in use" saat restart


# ─────────────────────────────────────────────────────────────────────────────
# Tile cache singleton (import dari tile_cache.py)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from .tile_cache import TILE_CACHE
except ImportError:
    # Fallback jika modul tidak ditemukan (misal saat development standalone)
    from tile_cache import TILE_CACHE

# ─────────────────────────────────────────────────────────────────────────────
# Pre-fetch thread pool (terpisah dari server thread pool)
# max_workers=3 agar tidak flood BHUMI server
# ─────────────────────────────────────────────────────────────────────────────
_PREFETCH_EXECUTOR = ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="BhumiPrefetch",
)

# Rate-limit antar request prefetch (detik)
_PREFETCH_DELAY: float = 0.2


# ─────────────────────────────────────────────────────────────────────────────
# Helper: konversi XYZ tile → BBOX EPSG:3857
# ─────────────────────────────────────────────────────────────────────────────

def xyz_to_bbox(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """
    Konversi koordinat tile XYZ (Google/OSM) ke bounding box EPSG:3857.
    :returns: (minx, miny, maxx, maxy) dalam meter
    """
    res  = INITIAL_RES / (2 ** z)
    minx = -ORIGIN_SHIFT + x * 256 * res
    maxx = -ORIGIN_SHIFT + (x + 1) * 256 * res
    maxy =  ORIGIN_SHIFT - y * 256 * res
    miny =  ORIGIN_SHIFT - (y + 1) * 256 * res
    return minx, miny, maxx, maxy


# ─────────────────────────────────────────────────────────────────────────────
# Fungsi fetch tile tunggal (dipakai handler + prefetch worker)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_one_tile(layer_id: str, z: int, x: int, y: int) -> Optional[bytes]:
    """
    Fetch satu tile dari BHUMI WMS.
    Return bytes PNG jika berhasil, None jika gagal/timeout/invalid.
    Tidak pernah raise exception.
    """
    try:
        minx, miny, maxx, maxy = xyz_to_bbox(z, x, y)
        params = {
            "SERVICE"    : "WMS",
            "VERSION"    : "1.3.0",
            "REQUEST"    : "GetMap",
            "FORMAT"     : "image/png",
            "TRANSPARENT": "true",
            "LAYERS"     : layer_id,
            "STYLES"     : "",
            "SRS"        : "EPSG:3857",
            "CRS"        : "EPSG:3857",
            "TILED"      : "true",
            "WIDTH"      : "512",
            "HEIGHT"     : "512",
            "BBOX"       : f"{minx},{miny},{maxx},{maxy}",
        }
        resp = _session.get(BHUMI_WMS_URL, params=params, timeout=PROXY_TIMEOUT)
        # Server BHUMI mengembalikan status 200 dengan XML ServiceException jika request tidak valid.
        # Pastikan data yang diterima benar-benar valid PNG.
        if (
            resp.status_code == 200
            and resp.content
            and resp.content.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            return resp.content
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Pre-fetch: 3×3 grid tetangga di background
# ─────────────────────────────────────────────────────────────────────────────

def _prefetch_neighbors(layer_id: str, z: int, cx: int, cy: int) -> None:
    """
    Submit 8 tile tetangga (3×3 grid minus pusat) ke ThreadPoolExecutor.
    Setiap tile di-fetch hanya jika belum ada di cache.
    Dipanggil setelah tile utama berhasil di-fetch.
    """
    for dx in range(-1, 2):
        for dy in range(-1, 2):
            if dx == 0 and dy == 0:
                continue   # tile pusat sudah ada
            nx, ny = cx + dx, cy + dy
            if nx < 0 or ny < 0:
                continue
            if TILE_CACHE.has(layer_id, z, nx, ny):
                continue   # sudah cached, skip
            _PREFETCH_EXECUTOR.submit(_worker_prefetch, layer_id, z, nx, ny)


def _worker_prefetch(layer_id: str, z: int, x: int, y: int) -> None:
    """
    Worker thread: fetch tile dan simpan ke cache.
    Jeda kecil dulu agar tidak flood server.
    """
    time.sleep(_PREFETCH_DELAY)
    # Double-check (mungkin sudah di-fetch thread lain sejak submit)
    if TILE_CACHE.has(layer_id, z, x, y):
        return
    data = _fetch_one_tile(layer_id, z, x, y)
    if data:
        TILE_CACHE.put(layer_id, z, x, y, data)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ─────────────────────────────────────────────────────────────────────────────

class BhumiTileHandler(BaseHTTPRequestHandler):
    """
    HTTP handler untuk dua jenis request:
      1. GET /{z}/{x}/{y}.png[?layer=...&zmin=...]  → tile WMS GetMap
      2. GET /getfeatureinfo?{params}               → WMS GetFeatureInfo
      3. GET /ping                                  → health check
      4. GET /cache/stats                           → statistik cache (JSON)
      5. GET /cache/clear                           → kosongkan cache
    """

    def log_message(self, format, *args):   # noqa: A002
        pass   # Sembunyikan log per-request

    def do_GET(self):   # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path.strip("/")

        if path == "ping":
            self._send_text(200, "OK")
        elif path == "cache/stats":
            import json
            stats = TILE_CACHE.stats()
            self._send_text(200, json.dumps(stats))
        elif path == "cache/clear":
            n = TILE_CACHE.clear()
            self._send_text(200, f"Cleared {n} tiles")
        else:
            self._handle_tile(path)

    # ── Tile handler ──────────────────────────────────────────────────────────

    def _handle_tile(self, path: str) -> None:
        """
        Alur lengkap untuk request tile:
          1. Parse z/x/y dan layer_id dari path + query string
          2. Cek cache → kirim instan jika HIT
          3. Jika z < zmin → coba mosaic dari cache → kirim atau transparan
          4. Fetch dari BHUMI → simpan cache → kirim
          5. Setelah kirim: pre-fetch 3×3 grid tetangga di background
        """
        try:
            parts = path.split("/")
            if len(parts) < 3:
                self._send_png(TRANSPARENT_PNG, cacheable=False)
                return

            z = int(parts[0])
            x = int(parts[1])
            y = int(parts[2].split(".")[0])   # potong ekstensi .png

            qs       = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            layer_id = qs.get("layer", ["bhumi_persil"])[0]
            zmin     = int(qs.get("zmin", [11])[0])

            # ── 1. Cache HIT → instan ─────────────────────────────────────────
            cached = TILE_CACHE.get(layer_id, z, x, y)
            if cached is not None:
                self._send_png(cached, cacheable=True)
                if z >= zmin:
                    _PREFETCH_EXECUTOR.submit(_prefetch_neighbors, layer_id, z, x, y)
                return

            # ── 2. Zoom terlalu rendah → coba mosaic dari cache ───────────────
            if z < zmin:
                overview = TILE_CACHE.get_overview(layer_id, z, x, y, zmin)
                if overview is not None:
                    TILE_CACHE.put(layer_id, z, x, y, overview)
                    self._send_png(overview, cacheable=True)
                else:
                    self._send_png(TRANSPARENT_PNG, cacheable=False)
                return

            # ── 3. Cache MISS → fetch dari BHUMI ──────────────────────────────
            data = _fetch_one_tile(layer_id, z, x, y)

            if data:
                TILE_CACHE.put(layer_id, z, x, y, data)
                self._send_png(data, cacheable=True)
                _PREFETCH_EXECUTOR.submit(_prefetch_neighbors, layer_id, z, x, y)
            else:
                self._send_png(TRANSPARENT_PNG, cacheable=False)

        except Exception:
            self._send_png(TRANSPARENT_PNG, cacheable=False)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _send_png(self, data: bytes, cacheable: bool = False) -> None:
        """
        Kirim respons PNG ke QGIS.

        :param cacheable: True → Cache-Control max-age=86400 (tile valid 24 jam)
                          False → Cache-Control max-age=60 (tile kosong/transparan)
        """
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        if cacheable:
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "public, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────────────────────
# Proxy server manager
# ─────────────────────────────────────────────────────────────────────────────

class BhumiProxyServer:
    """
    Wrapper HTTPServer yang berjalan di daemon thread terpisah.
    Mendukung start / stop / restart dan pengecekan status.
    """

    def __init__(self, port: int = 8089):
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return (
            self._server is not None
            and self._thread is not None
            and self._thread.is_alive()
        )

    def start(self) -> tuple[bool, str]:
        """
        Mulai server proxy (multi-threaded).
        :returns: (sukses, pesan)
        """
        with self._lock:
            if self.is_running:
                return True, f"Proxy sudah berjalan di port {self.port}"
            try:
                # ⚡ _ThreadedHTTPServer (bukan HTTPServer biasa!)
                # Setiap tile request QGIS ditangani di thread terpisah.
                # HTTPServer standar hanya handle 1 request sekaligus → timeout.
                self._server = _ThreadedHTTPServer(
                    ("127.0.0.1", self.port), BhumiTileHandler
                )
                self._thread = threading.Thread(
                    target=self._server.serve_forever,
                    name="BhumiProxyThread",
                    daemon=True,
                )
                self._thread.start()
                return True, f"Proxy berhasil dimulai di port {self.port}"
            except OSError as exc:
                self._server = None
                self._thread = None
                return False, f"Gagal memulai proxy di port {self.port}: {exc}"

    def stop(self) -> str:
        """Hentikan server proxy."""
        with self._lock:
            if self._server:
                self._server.shutdown()
                self._server = None
                self._thread = None
                return f"Proxy dihentikan (port {self.port})"
            return "Proxy tidak sedang berjalan"

    def restart(self, new_port: Optional[int] = None) -> tuple[bool, str]:
        """Restart server, opsional dengan port baru."""
        self.stop()
        if new_port is not None:
            self.port = new_port
        return self.start()

    def build_tile_url(self, layer_id: str, zmin: int, zmax: int) -> str:
        """
        Bangun URI XYZ tile untuk QgsRasterLayer.

        PENTING: zmin di URI QGIS di-set ke 0 (bukan zmin layer) agar
        QGIS tetap request tile di semua zoom — kita yang handle tampilan
        overview dari cache di level zoom rendah.
        """
        tile_url = (
            f"http://127.0.0.1:{self.port}/{{z}}/{{x}}/{{y}}.png"
            f"?layer={layer_id}&zmin={zmin}"
        )
        encoded = urllib.parse.quote(tile_url, safe=":/?&={}.")
        return (
            # zmin=0 agar QGIS minta tile di semua zoom level
            # (kita handle overview di proxy, bukan QGIS)
            f"type=xyz&url={encoded}"
            f"&zmax={zmax}&zmin=0"
            f"&http-header:Referer=http://127.0.0.1:{self.port}/"
        )
