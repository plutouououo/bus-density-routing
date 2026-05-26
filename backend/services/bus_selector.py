"""Service pemilih bus per-segmen 'naik' (Algoritma 2).

Dipanggil SETELAH Dijkstra (services/dijkstra.py) menentukan rute terbaik.
Algoritma 1 hanya peduli min kepadatan per koridor; Algoritma 2 memilih bus
SPESIFIK yang direkomendasikan untuk tiap segmen 'naik':

    primary  : kepadatan ASC (dibulatkan 2 desimal untuk dedupe noise)
    tiebreak : eta_menit ASC

Dua algoritma sengaja dipisah: Dijkstra tidak perlu tahu identitas bus, dan
bus_selector tidak perlu tahu topologi graf. State tidak dibagi.
"""

from collections import defaultdict
from typing import Any

KEPADATAN_DECIMAL = 2  # presisi bulatan untuk grouping kepadatan setara


def _label_kepadatan(k: float) -> str:
    """Mapping numerik -> label UI. Threshold sesuai spec produk."""
    if k <= 0.33:
        return "Sepi"
    if k <= 0.66:
        return "Sedang"
    return "Padat"


def _eta_menit_ke_halte(
    stops: list[dict],
    halte_id: str,
    koridor_id: int,
    sim_time: int,
) -> int | None:
    """ETA dalam menit dari sim_time sampai bus tiba di halte_id.

    Pakai waktu_tiba_detik dari jadwal — primitif yang sama dipakai oleh
    services/interpolation.py:get_bus_position (untuk eta_minutes ke next_stop)
    dan routers/rute.py:bus_berikutnya. Tidak ada perhitungan paralel di sini.

    Return None bila bus sudah lewat halte_naik di sim_time, atau halte tidak
    ada di jadwal bus tsb.
    """
    for stop in stops:
        if stop.get("halte_id") != halte_id:
            continue
        if stop.get("koridor_id") != koridor_id:
            continue
        if stop["waktu_tiba_detik"] < sim_time:
            continue  # kunjungan sebelumnya sudah lewat — terus cari yang berikut
        return round((stop["waktu_tiba_detik"] - sim_time) / 60)
    return None


def select_bus_per_segmen(
    segmen_list: list[dict],
    sim_time: int,
    jadwal: dict[str, list[dict]],
    realtime_kepadatan: dict[str, float],
) -> list[dict]:
    """Inject `bus_rekomendasi` ke tiap segmen tipe='naik' (mutasi in-place).

    Args:
        segmen_list: list segmen hasil format_rute (campuran 'naik' & 'transit').
        sim_time: detik sim saat ini (dipakai filter bus yang belum lewat &
            untuk hitung ETA).
        jadwal: dict bus_id -> list stop (urut per urutan), sama dengan
            app.state.jadwal.
        realtime_kepadatan: dict bus_id -> kepadatan (0..1) pada jam request.
            Bus tanpa entri di sini akan diabaikan (tidak masuk kandidat).

    Bus tanpa kandidat valid -> `bus_rekomendasi: None` (frontend graceful).
    """
    # Index bus per koridor sekali, dipakai ulang untuk setiap segmen 'naik'.
    bus_per_koridor: dict[int, list[str]] = defaultdict(list)
    for bus_id, stops in jadwal.items():
        if not stops:
            continue
        bus_per_koridor[stops[0]["koridor_id"]].append(bus_id)

    for segmen in segmen_list:
        if segmen.get("tipe") != "naik":
            continue

        koridor_id = segmen["koridor_id"]
        halte_naik = segmen["naik_di_id"]

        kandidat: list[dict[str, Any]] = []
        for bus_id in bus_per_koridor.get(koridor_id, []):
            kepadatan = realtime_kepadatan.get(bus_id)
            if kepadatan is None:
                continue
            eta = _eta_menit_ke_halte(jadwal[bus_id], halte_naik, koridor_id, sim_time)
            if eta is None:
                continue
            kandidat.append({
                "bus_id": bus_id,
                "kepadatan": float(kepadatan),
                "eta_menit": eta,
            })

        if not kandidat:
            segmen["bus_rekomendasi"] = None
            continue

        # Dedupe noise kepadatan via pembulatan; tie -> eta_menit lebih kecil
        kandidat.sort(key=lambda b: (round(b["kepadatan"], KEPADATAN_DECIMAL), b["eta_menit"]))
        terbaik = kandidat[0]
        segmen["bus_rekomendasi"] = {
            "bus_id": terbaik["bus_id"],
            "kepadatan": round(terbaik["kepadatan"], 3),
            "label_kepadatan": _label_kepadatan(terbaik["kepadatan"]),
            "eta_menit": terbaik["eta_menit"],
        }

    return segmen_list
