# -*- coding: utf-8 -*-
r"""
install.py — Skrip Installer Plugin BHUMI Persil Connector v2.0
═══════════════════════════════════════════════════════════════════════════════

CARA PAKAI:
  Jalankan seluruh isi skrip ini di QGIS Python Console (Ctrl+Alt+P):

      exec(open(r"D:\Web_app\qgis\bhumi_wms\install.py").read())

  Atau copy-paste seluruh konten skrip ini langsung ke konsol QGIS.

YANG DILAKUKAN SKRIP INI:
  1. Membuat folder plugin 'bhumi_persil' di direktori plugin QGIS pengguna
  2. Menyalin semua file plugin dari workspace ke folder tujuan
  3. Mendaftarkan dan mengaktifkan plugin di QGIS
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import shutil
import sys

# ─────────────────────────────────────────────────────────────────────────────
# Konfigurasi
# ─────────────────────────────────────────────────────────────────────────────

# Folder sumber (lokasi file plugin di workspace Anda)
SOURCE_DIR = r"D:\Web_app\qgis\bhumi_wms"

# Nama folder plugin di QGIS (harus unik & tanpa spasi)
PLUGIN_FOLDER_NAME = "bhumi_persil"


# ─────────────────────────────────────────────────────────────────────────────
# Proses instalasi
# ─────────────────────────────────────────────────────────────────────────────

def install_plugin():
    try:
        from qgis.core import QgsApplication
        from qgis.utils import loadPlugin, startPlugin, updateAvailablePlugins
    except ImportError:
        print("❌ ERROR: Skrip ini harus dijalankan di dalam QGIS Python Console!")
        return False

    # Tentukan direktori tujuan plugin QGIS
    qgis_plugins_dir = os.path.join(
        QgsApplication.qgisSettingsDirPath(), "python", "plugins"
    )
    target_dir = os.path.join(qgis_plugins_dir, PLUGIN_FOLDER_NAME)

    print(f"📂 Sumber     : {SOURCE_DIR}")
    print(f"📂 Tujuan     : {target_dir}")
    print("")

    # ── Validasi folder sumber ────────────────────────────────────────────────
    if not os.path.isdir(SOURCE_DIR):
        print(
            f"❌ ERROR: Folder sumber tidak ditemukan: {SOURCE_DIR}\n"
            "   Pastikan path SOURCE_DIR di atas sudah benar."
        )
        return False

    required_files = ["__init__.py", "plugin.py", "metadata.txt"]
    for fname in required_files:
        fpath = os.path.join(SOURCE_DIR, fname)
        if not os.path.isfile(fpath):
            print(f"❌ ERROR: File wajib tidak ditemukan: {fpath}")
            return False

    # ── Jika plugin sudah ada, unload dulu ───────────────────────────────────
    if PLUGIN_FOLDER_NAME in sys.modules:
        print(f"🔄 Plugin '{PLUGIN_FOLDER_NAME}' sudah terpasang, melakukan unload...")
        try:
            from qgis.utils import unloadPlugin
            unloadPlugin(PLUGIN_FOLDER_NAME)
            # Hapus modul dari sys.modules agar reload bersih
            mods_to_remove = [
                k for k in sys.modules if k.startswith(PLUGIN_FOLDER_NAME)
            ]
            for mod in mods_to_remove:
                del sys.modules[mod]
            print(f"   ✔ Unload berhasil ({len(mods_to_remove)} modul dihapus dari cache)")
        except Exception as e:
            print(f"   ⚠ Gagal unload plugin lama: {e} (lanjut instalasi...)")

    # ── Salin file ke folder plugin QGIS ─────────────────────────────────────
    print(f"📋 Menyalin file plugin ke: {target_dir}")

    # File dan folder yang disalin
    items_to_copy = [
        "__init__.py",
        "plugin.py",
        "metadata.txt",
        "proxy_server.py",
        "bhumi_layers.py",
        "settings_dialog.py",
        "export_tool.py",
        "tile_cache.py",
    ]

    os.makedirs(target_dir, exist_ok=True)

    copied = 0
    for item in items_to_copy:
        src = os.path.join(SOURCE_DIR, item)
        dst = os.path.join(target_dir, item)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            print(f"   ✔ {item}")
            copied += 1
        else:
            print(f"   ⚠ Tidak ditemukan (dilewati): {item}")

    # Salin folder resources jika ada
    src_resources = os.path.join(SOURCE_DIR, "resources")
    dst_resources = os.path.join(target_dir, "resources")
    if os.path.isdir(src_resources):
        shutil.copytree(src_resources, dst_resources, dirs_exist_ok=True)
        print(f"   ✔ resources/ (folder ikon)")
    else:
        os.makedirs(dst_resources, exist_ok=True)
        print(f"   ℹ resources/ tidak ada, folder kosong dibuat")

    print(f"\n📊 Total {copied} file berhasil disalin.")

    # ── Aktivasi plugin ───────────────────────────────────────────────────────
    print("\n🔌 Mendaftarkan dan mengaktifkan plugin...")

    updateAvailablePlugins()

    load_ok = loadPlugin(PLUGIN_FOLDER_NAME)
    if not load_ok:
        print(
            f"❌ Gagal memuat plugin '{PLUGIN_FOLDER_NAME}'.\n"
            "   Cek error di atas atau buka Plugin Manager untuk detail."
        )
        return False

    start_ok = startPlugin(PLUGIN_FOLDER_NAME)
    if not start_ok:
        print(
            f"❌ Gagal mengaktifkan plugin '{PLUGIN_FOLDER_NAME}'.\n"
            "   Plugin dimuat tapi gagal start. Cek error di atas."
        )
        return False

    print("\n" + "═" * 60)
    print("✅ PLUGIN BHUMI PERSIL CONNECTOR BERHASIL TERPASANG!")
    print("═" * 60)
    print("")
    print("📌 Langkah selanjutnya:")
    print("   1. Klik tombol 'Tampilkan Persil (Bidang Tanah)' di toolbar BHUMI ATR/BPN")
    print("   2. Pastikan peta di zoom ke skala <= 1:5.000")
    print("   3. Jika layer persil lama masih ada di Layers Panel, hapus dulu lalu klik tombol toolbar lagi")
    print("")
    return True


# ── Jalankan saat skrip di-exec() ────────────────────────────────────────────
if __name__ == "__main__":
    install_plugin()
else:
    # Dipanggil via exec() dari QGIS Console
    install_plugin()
