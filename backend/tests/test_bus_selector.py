"""Unit test untuk services/bus_selector.py (Algoritma 2)."""

from services.bus_selector import MAX_ETA_MENIT_DEFAULT, select_bus_per_segmen


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


def test_bus_terlalu_jauh_diabaikan_walau_lebih_sepi():
    """Trip besok/lintas hari yang sangat jauh tidak boleh menang dari bus dekat."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 1,
        "naik_di_id": "A",
    }]
    jadwal = {
        "B-DEKAT": [
            {"halte_id": "A", "koridor_id": 1, "waktu_tiba_detik": 5 * 3600 + 6 * 60},
        ],
        "B-BESOK": [
            {"halte_id": "A", "koridor_id": 1, "waktu_tiba_detik": 26 * 3600 + 10 * 60},
        ],
    }
    realtime = {"B-DEKAT": 0.60, "B-BESOK": 0.05}

    select_bus_per_segmen(segmen, sim_time=5 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)

    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "B-DEKAT"
    assert rek["eta_menit"] == 6


def test_bus_di_atas_batas_eta_default_diabaikan():
    """Default selector hanya melihat kandidat sampai 45 menit dari sekarang."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 1,
        "naik_di_id": "A",
    }]
    jadwal = {
        "B-45-MENIT": [
            {
                "halte_id": "A",
                "koridor_id": 1,
                "waktu_tiba_detik": 7 * 3600 + MAX_ETA_MENIT_DEFAULT * 60,
            },
        ],
        "B-46-MENIT-SEPI": [
            {
                "halte_id": "A",
                "koridor_id": 1,
                "waktu_tiba_detik": 7 * 3600 + (MAX_ETA_MENIT_DEFAULT + 1) * 60,
            },
        ],
    }
    realtime = {"B-45-MENIT": 0.80, "B-46-MENIT-SEPI": 0.05}

    select_bus_per_segmen(segmen, sim_time=7 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)

    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "B-45-MENIT"
    assert rek["eta_menit"] == MAX_ETA_MENIT_DEFAULT
    assert rek["candidate_count"] == 1


def test_bus_cepat_bisa_menang_dari_bus_jauh_yang_lebih_sepi():
    """Skor gabungan mencegah rekomendasi menunggu terlalu lama demi sepi."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 3,
        "naik_di_id": "PESAKIH",
    }]
    jadwal = {
        "BUS-CEPAT": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 5 * 3600 + 8 * 60},
        ],
        "BUS-SEPI-JAUH": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 5 * 3600 + 39 * 60},
        ],
    }
    realtime = {"BUS-CEPAT": 0.60, "BUS-SEPI-JAUH": 0.09}

    select_bus_per_segmen(segmen, sim_time=5 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)

    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "BUS-CEPAT"
    assert rek["eta_menit"] == 8
    assert rek["extra_wait_menit"] == 0


def test_bus_lebih_sepi_dalam_tambahan_tunggu_wajar_bisa_menang():
    """Pada jam sibuk, menunggu sedikit lebih lama layak bila bus jauh lebih sepi."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 3,
        "naik_di_id": "PESAKIH",
    }]
    jadwal = {
        "BUS-CEPAT-PADAT": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 8 * 3600 + 2 * 60},
        ],
        "BUS-LEBIH-SEPI": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 8 * 3600 + 12 * 60},
        ],
    }
    realtime = {"BUS-CEPAT-PADAT": 0.74, "BUS-LEBIH-SEPI": 0.30}

    select_bus_per_segmen(segmen, sim_time=8 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)

    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "BUS-LEBIH-SEPI"
    assert rek["eta_menit"] == 12
    assert rek["extra_wait_menit"] == 10


def test_bus_berikutnya_langsung_dipilih_jika_kepadatan_aman():
    """Kalau bus tercepat <= threshold aman 0.35, tidak perlu menunggu lagi."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 3,
        "naik_di_id": "PESAKIH",
    }]
    jadwal = {
        "BUS-CEPAT-AMAN": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 9 * 3600 + 2 * 60},
        ],
        "BUS-LEBIH-SEPI": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 9 * 3600 + 12 * 60},
        ],
    }
    realtime = {"BUS-CEPAT-AMAN": 0.35, "BUS-LEBIH-SEPI": 0.10}

    select_bus_per_segmen(segmen, sim_time=9 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)

    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "BUS-CEPAT-AMAN"
    assert rek["eta_menit"] == 2
    assert rek["selection_reason"] == "next_bus_safe_density"


def test_jam_sibuk_selector_melihat_beberapa_bus_berikutnya():
    """Bus dua puluh menit lagi bisa dipilih jika jauh lebih lega dari bus pertama."""
    segmen = [{
        "tipe": "naik",
        "koridor_id": 3,
        "naik_di_id": "PESAKIH",
    }]
    jadwal = {
        "BUS-2-MENIT-PADAT": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 9 * 3600 + 2 * 60},
        ],
        "BUS-8-MENIT-MASIH-PADAT": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 9 * 3600 + 8 * 60},
        ],
        "BUS-18-MENIT-LEGA": [
            {"halte_id": "PESAKIH", "koridor_id": 3, "waktu_tiba_detik": 9 * 3600 + 18 * 60},
        ],
    }
    realtime = {
        "BUS-2-MENIT-PADAT": 0.74,
        "BUS-8-MENIT-MASIH-PADAT": 0.68,
        "BUS-18-MENIT-LEGA": 0.35,
    }

    select_bus_per_segmen(segmen, sim_time=9 * 3600, jadwal=jadwal, realtime_kepadatan=realtime)

    rek = segmen[0]["bus_rekomendasi"]
    assert rek["bus_id"] == "BUS-18-MENIT-LEGA"
    assert rek["eta_menit"] == 18
    assert rek["extra_wait_menit"] == 16


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
