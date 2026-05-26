"""Unit test untuk services/bus_selector.py (Algoritma 2)."""

from services.bus_selector import select_bus_per_segmen


def _jadwal_dummy() -> dict[str, list[dict]]:
    """Dua bus di koridor 1, satu bus di koridor 2.

    Semua tiba di halte 'A' tapi pada waktu berbeda.
    """
    return {
        "B-K1-01": [
            {"halte_id": "A", "koridor_id": 1, "waktu_tiba_detik": 8 * 3600 + 60},
            {"halte_id": "B", "koridor_id": 1, "waktu_tiba_detik": 8 * 3600 + 300},
        ],
        "B-K1-02": [
            {"halte_id": "A", "koridor_id": 1, "waktu_tiba_detik": 8 * 3600 + 600},
            {"halte_id": "B", "koridor_id": 1, "waktu_tiba_detik": 8 * 3600 + 900},
        ],
        "B-K2-01": [
            {"halte_id": "A", "koridor_id": 2, "waktu_tiba_detik": 8 * 3600 + 120},
        ],
    }


def test_pilih_bus_berdasarkan_kepadatan_terendah():
    """Dua bus di koridor sama: yang kepadatan lebih rendah harus terpilih,
    walau ETA-nya lebih lama."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 1,
        "naik_di_id": "A",
    }]
    jadwal = _jadwal_dummy()
    realtime = {"B-K1-01": 0.80, "B-K1-02": 0.20}  # bus-02 lebih sepi

    select_bus_per_segmen(segmen, sim_time=8 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)
    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "B-K1-02"
    assert rek["label_kepadatan"] == "Sepi"
    assert rek["eta_menit"] == 10  # 600 detik / 60


def test_tiebreak_eta_saat_kepadatan_setara():
    """Kepadatan setara (perbedaan < epsilon 0.01) -> pilih ETA terkecil."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 1,
        "naik_di_id": "A",
    }]
    jadwal = _jadwal_dummy()
    # 0.30 vs 0.304 -> dibulatkan 2-decimal == 0.30 -> tie -> eta menang
    realtime = {"B-K1-01": 0.30, "B-K1-02": 0.304}

    select_bus_per_segmen(segmen, sim_time=8 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)
    assert segmen[0]["bus_rekomendasi"]["bus_id"] == "B-K1-01"  # ETA 1 min < 10 min


def test_bus_yang_sudah_lewat_diabaikan():
    """Bus yang waktu_tiba_detik-nya < sim_time tidak boleh jadi kandidat."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 1,
        "naik_di_id": "A",
    }]
    jadwal = _jadwal_dummy()
    realtime = {"B-K1-01": 0.10, "B-K1-02": 0.50}
    # sim_time melewati bus-01 (tiba 8:00:60) tapi belum lewat bus-02 (8:10:00)
    select_bus_per_segmen(segmen, sim_time=8 * 3600 + 120, jadwal=jadwal, realtime_kepadatan=realtime)
    assert segmen[0]["bus_rekomendasi"]["bus_id"] == "B-K1-02"


def test_tidak_ada_kandidat_return_none():
    """Tidak ada bus tersisa -> bus_rekomendasi=None (frontend graceful)."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 1,
        "naik_di_id": "A",
    }]
    jadwal = _jadwal_dummy()
    realtime = {"B-K1-01": 0.10, "B-K1-02": 0.50}
    # sim_time di luar jadwal semua bus
    select_bus_per_segmen(segmen, sim_time=20 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)
    assert segmen[0]["bus_rekomendasi"] is None


def test_segmen_transit_tidak_disentuh():
    """Segmen tipe transit tidak boleh mendapat bus_rekomendasi."""
    segmen = [
        {"tipe": "transit", "transit_di": "C", "dari_koridor": 1, "ke_koridor": 2},
    ]
    select_bus_per_segmen(segmen, sim_time=8 * 3600, jadwal=_jadwal_dummy(), realtime_kepadatan={})
    assert "bus_rekomendasi" not in segmen[0]


def test_label_kepadatan_threshold():
    """Mapping label: <=0.33 Sepi, <=0.66 Sedang, >0.66 Padat."""
    base_segmen = {"tipe": "naik", "koridor_id": 2, "naik_di_id": "A"}
    jadwal = _jadwal_dummy()

    for kepadatan, label in [(0.20, "Sepi"), (0.50, "Sedang"), (0.90, "Padat")]:
        segmen = [dict(base_segmen)]
        select_bus_per_segmen(
            segmen,
            sim_time=8 * 3600,
            jadwal=jadwal,
            realtime_kepadatan={"B-K2-01": kepadatan},
        )
        assert segmen[0]["bus_rekomendasi"]["label_kepadatan"] == label
