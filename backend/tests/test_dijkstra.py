"""Unit test untuk service Dijkstra TransJakarta.

Tes-tes di sini sengaja memakai graph dummy in-memory (tanpa Supabase)
agar bisa berjalan deterministik dan cepat. Topologi:

    Koridor 1 (K1):  A --> B --> C
    Koridor 2 (K2):  C --> D --> E
    Halte C dilayani oleh K1 dan K2 -> titik transit.
"""

import pytest

from services.dijkstra import (
    BOBOT_KEPADATAN,
    BOBOT_TRANSIT,
    build_graph,
    dijkstra,
    format_rute,
)

KEPADATAN_DUMMY = 0.50


def _graph_data_dummy() -> dict:
    """Bangun graph_data mirip output load_graph_data, tanpa hit Supabase.

    Skema baru: `kepadatan_bus` = per-bus per-jam (bukan per-segmen lagi).
    Dijkstra mengambil min() per koridor saat build_graph.
    """
    return {
        "segmen": [
            {"segmen_id": "K1_A_B", "koridor_id": 1, "halte_asal": "A",
             "halte_tujuan": "B", "urutan": 1, "waktu_tempuh_detik": 180},
            {"segmen_id": "K1_B_C", "koridor_id": 1, "halte_asal": "B",
             "halte_tujuan": "C", "urutan": 2, "waktu_tempuh_detik": 180},
            {"segmen_id": "K2_C_D", "koridor_id": 2, "halte_asal": "C",
             "halte_tujuan": "D", "urutan": 1, "waktu_tempuh_detik": 240},
            {"segmen_id": "K2_D_E", "koridor_id": 2, "halte_asal": "D",
             "halte_tujuan": "E", "urutan": 2, "waktu_tempuh_detik": 240},
        ],
        "kepadatan_bus": [
            # Koridor 1 punya 1 bus, koridor 2 punya 1 bus. Min == nilai itu.
            {"bus_id": "B-K1-01", "koridor_id": 1, "jam": 8,
             "hari_tipe": "weekday", "kepadatan": KEPADATAN_DUMMY},
            {"bus_id": "B-K2-01", "koridor_id": 2, "jam": 8,
             "hari_tipe": "weekday", "kepadatan": KEPADATAN_DUMMY},
        ],
        "halte": {
            h: {"halte_id": h, "nama": h, "lat": 0.0, "lng": 0.0}
            for h in ["A", "B", "C", "D", "E"]
        },
        "koridor_halte": [],
        "koridor": {
            1: {"koridor_id": 1, "nama_pendek": "K1", "nama_panjang": "Koridor 1"},
            2: {"koridor_id": 2, "nama_pendek": "K2", "nama_panjang": "Koridor 2"},
        },
        "halte_to_koridor": {
            "A": {1}, "B": {1}, "C": {1, 2}, "D": {2}, "E": {2},
        },
    }


# ----------------------------------------------------------------------
# Test case 1: rute dalam satu koridor (tanpa transit)
# ----------------------------------------------------------------------

def test_rute_dalam_satu_koridor_tanpa_transit():
    """A -> C lewat K1 saja. Skor = mean(kepadatan) * 0.40 + 0 * 0.60."""
    data = _graph_data_dummy()
    graph = build_graph(data, jam=8, hari_tipe="weekday")
    rute = dijkstra(graph, "A", "C", k=1)

    assert len(rute) == 1
    r = rute[0]
    assert r["transit_count"] == 0
    assert r["n_segmen"] == 2
    expected = KEPADATAN_DUMMY * BOBOT_KEPADATAN
    assert r["cost"] == pytest.approx(expected, abs=1e-6)

    # Pastikan semua edge yang ditempuh berupa segmen koridor 1
    tipe_path = [(e["tipe"], e["koridor_id"]) for e in r["path"]]
    assert tipe_path == [("segmen", 1), ("segmen", 1)]


# ----------------------------------------------------------------------
# Test case 2: rute dengan satu transit harus lebih mahal
# ----------------------------------------------------------------------

def test_rute_dengan_satu_transit_lebih_mahal():
    """A -> E mengharuskan transit di C antara K1 dan K2.

    Skor seharusnya = 0.5 * 0.40 + 1 * 0.60.
    Skor ini wajib lebih besar dari skor rute tanpa transit (case 1).
    """
    data = _graph_data_dummy()
    graph = build_graph(data, jam=8, hari_tipe="weekday")
    rute = dijkstra(graph, "A", "E", k=1)

    assert len(rute) == 1
    r = rute[0]
    assert r["transit_count"] == 1
    assert r["n_segmen"] == 4

    skor_tanpa_transit = KEPADATAN_DUMMY * BOBOT_KEPADATAN
    expected = KEPADATAN_DUMMY * BOBOT_KEPADATAN + 1 * BOBOT_TRANSIT
    assert r["cost"] == pytest.approx(expected, abs=1e-6)
    assert r["cost"] > skor_tanpa_transit

    # Cek bahwa path mengandung tepat satu edge transit
    n_transit_edges = sum(1 for e in r["path"] if e["tipe"] == "transit")
    assert n_transit_edges == 1

    # Format response: harus ada marker transit di C antara koridor 1 dan 2
    formatted = format_rute(r, data)
    transit_markers = [s for s in formatted["segmen"] if s.get("tipe") == "transit"]
    assert len(transit_markers) == 1
    t = transit_markers[0]
    assert t["transit_di"] == "C"
    assert t["transit_di_id"] == "C"
    assert t["dari_koridor"] == 1
    assert t["ke_koridor"] == 2

    # Pastikan field halte_id & segmen_detail tersedia pada grup "naik"
    # (dipakai frontend untuk plot polyline warna kepadatan).
    grup_naik = [s for s in formatted["segmen"] if s.get("tipe") == "naik"]
    assert len(grup_naik) == 2
    grup1, grup2 = grup_naik
    assert grup1["naik_di_id"] == "A"
    assert grup1["turun_di_id"] == "C"
    assert [d["dari_id"] for d in grup1["segmen_detail"]] == ["A", "B"]
    assert [d["ke_id"] for d in grup1["segmen_detail"]] == ["B", "C"]
    assert grup2["naik_di_id"] == "C"
    assert grup2["turun_di_id"] == "E"


# ----------------------------------------------------------------------
# Test case 3: asal = tujuan harus raise ValueError
# ----------------------------------------------------------------------

def test_asal_sama_dengan_tujuan_raises():
    data = _graph_data_dummy()
    graph = build_graph(data, jam=8, hari_tipe="weekday")
    with pytest.raises(ValueError):
        dijkstra(graph, "A", "A", k=1)


# ----------------------------------------------------------------------
# Test case 4: halte tidak terhubung -> list kosong
# ----------------------------------------------------------------------

def test_halte_tidak_terhubung_return_kosong():
    """Halte F ditambahkan tanpa edge masuk maupun keluar.

    Karena tidak ada jalur dari A ke F dalam batas transit manapun,
    dijkstra() harus mengembalikan list kosong (router akan menerjemahkannya
    jadi HTTPException 404).
    """
    data = _graph_data_dummy()
    data["halte"]["F"] = {"halte_id": "F", "nama": "F", "lat": 0.0, "lng": 0.0}
    data["halte_to_koridor"]["F"] = {99}  # koridor tunggal, tidak terhubung
    graph = build_graph(data, jam=8, hari_tipe="weekday")

    rute = dijkstra(graph, "A", "F", k=3)
    assert rute == []
