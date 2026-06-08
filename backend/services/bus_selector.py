"""Service pemilih bus per-segmen 'naik' (Algoritma 2).

Dipanggil SETELAH Dijkstra (services/dijkstra.py) menentukan rute terbaik.
Algoritma 1 mencari kandidat rute; Algoritma 2 memilih bus SPESIFIK yang
direkomendasikan untuk tiap segmen 'naik'. Pemilihan bus memakai skor gabungan
kepadatan dan tambahan waktu tunggu dari bus tercepat agar bus yang sedikit
lebih lama tetapi jauh lebih sepi bisa dipilih, tanpa menyarankan tunggu
terlalu lama.

Dua algoritma sengaja dipisah: Dijkstra tidak perlu tahu identitas bus, dan
bus_selector tidak perlu tahu topologi graf. State tidak dibagi.
"""

from collections import defaultdict
from typing import Any

KEPADATAN_DECIMAL = 2  # presisi bulatan untuk grouping kepadatan setara
BUS_CAPACITY = 80
MAX_ETA_MENIT_DEFAULT = 45
MAX_ETA_DETIK_DEFAULT = MAX_ETA_MENIT_DEFAULT * 60
MAX_EXTRA_WAIT_MENIT_DEFAULT = 20
SAFE_NEXT_BUS_DENSITY_THRESHOLD = 0.35
BUS_SCORE_DENSITY_WEIGHT = 0.85
BUS_SCORE_WAIT_WEIGHT = 0.15


def _label_kepadatan(k: float) -> str:
    """Mapping numerik -> label lama UI. Sangat padat tetap dipetakan Padat."""
    if k < 0.50:
        return "Sepi"
    if k < 0.80:
        return "Sedang"
    return "Padat"


def _kategori_kepadatan(k: float) -> str:
    if k < 0.50:
        return "sepi"
    if k < 0.80:
        return "sedang"
    if k < 1.00:
        return "padat"
    return "sangat_padat"


def _bus_selection_score(
    kepadatan: float,
    eta_menit: int,
    earliest_eta_menit: int,
    max_extra_wait_menit: int,
) -> dict[str, float]:
    kepadatan_norm = min(max(float(kepadatan), 0.0), 1.0)
    extra_wait_menit = max(0, eta_menit - earliest_eta_menit)
    extra_wait_norm = min(extra_wait_menit / max(1, max_extra_wait_menit), 1.0)
    score = (
        BUS_SCORE_DENSITY_WEIGHT * kepadatan_norm
        + BUS_SCORE_WAIT_WEIGHT * extra_wait_norm
    )
    return {
        "score": score,
        "kepadatan_norm": kepadatan_norm,
        "earliest_eta_menit": earliest_eta_menit,
        "extra_wait_menit": extra_wait_menit,
        "extra_wait_norm": extra_wait_norm,
    }


def _eta_menit_ke_halte(
    stops: list[dict],
    halte_id: str,
    koridor_id: int,
    sim_time: int,
    max_eta_detik: int = MAX_ETA_DETIK_DEFAULT,
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
        eta_detik = stop["waktu_tiba_detik"] - sim_time
        if eta_detik < 0:
            continue  # kunjungan sebelumnya sudah lewat; cari yang berikut
        if eta_detik > max_eta_detik:
            continue  # hindari trip besok / kandidat yang terlalu jauh
        return round(eta_detik / 60)
    return None


def select_bus_per_segmen(
    segmen_list: list[dict],
    sim_time: int,
    jadwal: dict[str, list[dict]],
    realtime_kepadatan: dict[str, float],
    max_eta_menit: int = MAX_ETA_MENIT_DEFAULT,
    max_extra_wait_menit: int = MAX_EXTRA_WAIT_MENIT_DEFAULT,
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
            eta = _eta_menit_ke_halte(
                jadwal[bus_id],
                halte_naik,
                koridor_id,
                sim_time,
                max_eta_detik=max_eta_menit * 60,
            )
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

        earliest_eta = min(b["eta_menit"] for b in kandidat)
        earliest_candidates = [b for b in kandidat if b["eta_menit"] == earliest_eta]
        earliest_best = min(
            earliest_candidates,
            key=lambda b: (round(b["kepadatan"], KEPADATAN_DECIMAL), b["bus_id"]),
        )
        kandidat_layak = [
            b for b in kandidat
            if b["eta_menit"] <= earliest_eta + max_extra_wait_menit
        ]
        for bus in kandidat_layak:
            bus.update(_bus_selection_score(
                bus["kepadatan"],
                bus["eta_menit"],
                earliest_eta,
                max_extra_wait_menit,
            ))

        if earliest_best["kepadatan"] <= SAFE_NEXT_BUS_DENSITY_THRESHOLD:
            earliest_best.update(_bus_selection_score(
                earliest_best["kepadatan"],
                earliest_best["eta_menit"],
                earliest_eta,
                max_extra_wait_menit,
            ))
            terbaik = earliest_best
            selection_reason = "next_bus_safe_density"
        else:
            kandidat_layak.sort(key=lambda b: (
                b["score"],
                b["eta_menit"],
                round(b["kepadatan"], KEPADATAN_DECIMAL),
            ))
            terbaik = kandidat_layak[0]
            selection_reason = "bus_score"

        segmen["bus_rekomendasi"] = {
            "bus_id": terbaik["bus_id"],
            "kepadatan": round(terbaik["kepadatan"], 3),
            "label_kepadatan": _label_kepadatan(terbaik["kepadatan"]),
            "kategori_kepadatan": _kategori_kepadatan(terbaik["kepadatan"]),
            "eta_menit": terbaik["eta_menit"],
            "selection_score": round(terbaik["score"], 4),
            "selection_reason": selection_reason,
            "safe_density_threshold": SAFE_NEXT_BUS_DENSITY_THRESHOLD,
            "earliest_eta_menit": terbaik["earliest_eta_menit"],
            "extra_wait_menit": terbaik["extra_wait_menit"],
            "extra_wait_norm": round(terbaik["extra_wait_norm"], 3),
            "estimated_passengers": round(terbaik["kepadatan"] * BUS_CAPACITY, 2),
            "capacity": BUS_CAPACITY,
            "candidate_count": len(kandidat),
            "considered_candidate_count": len(kandidat_layak),
            "candidate_debug": [
                {
                    "bus_id": bus["bus_id"],
                    "eta_menit": bus["eta_menit"],
                    "kepadatan": round(bus["kepadatan"], 3),
                    "score": round(bus.get("score", 0.0), 4),
                }
                for bus in sorted(
                    kandidat_layak,
                    key=lambda b: (b["eta_menit"], round(b["kepadatan"], KEPADATAN_DECIMAL)),
                )[:5]
            ],
        }

    return segmen_list
