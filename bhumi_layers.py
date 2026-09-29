# -*- coding: utf-8 -*-
"""
bhumi_layers.py
Definisi semua layer BHUMI ATR/BPN yang tersedia beserta metadata-nya.
Tambahkan layer baru cukup di daftar BHUMI_LAYERS di bawah ini.
"""

# ─────────────────────────────────────────────────────────────────────────────
# Daftar layer BHUMI ATR/BPN
# Setiap entry adalah dict dengan key:
#   id        : nama unik layer (dipakai sbg WMS LAYERS param & key QSettings)
#   label     : nama tampilan di QGIS & menu toolbar
#   menu_text : teks menu di &BHUMI ATR/BPN
#   tooltip   : tooltip tombol toolbar
#   zmin      : zoom minimum agar tile tidak kosong
#   zmax      : zoom maximum
#   info_format: format GetFeatureInfo yang didukung server
# ─────────────────────────────────────────────────────────────────────────────

BHUMI_LAYERS: list[dict] = [
    {
        "id": "bhumi_persil",
        "label": "Persil BHUMI (Bidang Tanah)",
        "menu_text": "Tampilkan &Persil (Bidang Tanah)",
        "tooltip": "Tampilkan layer bidang tanah / persil BHUMI ATR/BPN",
        "zmin": 11,
        "zmax": 21,
        "info_format": "text/html",
    },
]

# Lookup cepat berdasarkan id
LAYERS_BY_ID: dict[str, dict] = {lyr["id"]: lyr for lyr in BHUMI_LAYERS}


def get_layer_by_id(layer_id: str) -> dict | None:
    """Kembalikan definisi layer berdasarkan id-nya, atau None jika tidak ada."""
    return LAYERS_BY_ID.get(layer_id)
