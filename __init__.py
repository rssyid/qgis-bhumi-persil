# -*- coding: utf-8 -*-
"""
BHUMI Persil Connector — QGIS Plugin
Entry point: classFactory dipanggil oleh QGIS saat plugin diaktifkan.
"""


def classFactory(iface):
    """
    Dipanggil QGIS untuk menginisialisasi plugin.

    :param iface: QgisInterface — antarmuka utama QGIS.
    :returns: Instance BhumiPersilPlugin.
    """
    from .plugin import BhumiPersilPlugin
    return BhumiPersilPlugin(iface)
