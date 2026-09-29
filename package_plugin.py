# -*- coding: utf-8 -*-
"""
package_plugin.py
Skrip untuk mengemas (package) plugin BHUMI Persil Connector ke dalam file ZIP
yang siap didistribusikan dan di-install via:
QGIS Menu -> Plugins -> Manage and Install Plugins... -> Install from ZIP.
"""

import os
import zipfile

# Direktori sumber
SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_ZIP = os.path.join(SOURCE_DIR, "bhumi_persil.zip")
PLUGIN_ROOT_NAME = "bhumi_persil"

# Berkas-berkas plugin yang disertakan
PLUGIN_FILES = [
    "__init__.py",
    "metadata.txt",
    "plugin.py",
    "proxy_server.py",
    "tile_cache.py",
    "bhumi_layers.py",
    "settings_dialog.py",
    "export_tool.py",
]


def build_zip():
    print("[*] Membuat paket ZIP distribusi QGIS...")
    print(f"    Tujuan: {OUTPUT_ZIP}")

    with zipfile.ZipFile(OUTPUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Salin berkas python & metadata
        for filename in PLUGIN_FILES:
            file_path = os.path.join(SOURCE_DIR, filename)
            if os.path.isfile(file_path):
                arcname = os.path.join(PLUGIN_ROOT_NAME, filename)
                zf.write(file_path, arcname)
                print(f"    + {arcname}")
            else:
                print(f"    - Berkas tidak ditemukan (dilewati): {filename}")

        # 2. Salin folder resources
        res_dir = os.path.join(SOURCE_DIR, "resources")
        if os.path.isdir(res_dir):
            for item in os.listdir(res_dir):
                item_path = os.path.join(res_dir, item)
                if os.path.isfile(item_path):
                    arcname = os.path.join(PLUGIN_ROOT_NAME, "resources", item)
                    zf.write(item_path, arcname)
                    print(f"    + {arcname}")

    size_kb = os.path.getsize(OUTPUT_ZIP) / 1024.0
    print("\n" + "=" * 60)
    print(f"[OK] PAKET ZIP BERHASIL DIBUAT!")
    print(f"Lokasi : {OUTPUT_ZIP} ({size_kb:.1f} KB)")
    print("=" * 60)
    print("\nCara Install di QGIS komputer lain:")
    print("  1. Buka software QGIS")
    print("  2. Menu bar: Plugins -> Manage and Install Plugins... (Kelola & Pasang Plugin)")
    print("  3. Pilih tab: 'Install from ZIP' (Pasang dari ZIP)")
    print("  4. Klik tombol [...] lalu pilih file: bhumi_persil.zip")
    print("  5. Klik 'Install Plugin' -> Selesai!")


if __name__ == "__main__":
    build_zip()
