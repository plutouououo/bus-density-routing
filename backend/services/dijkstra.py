"""Service rekomendasi rute Dijkstra untuk TransJakarta (Algoritma 1).

Graf dimodelkan sebagai *directed graph*:
- Node = halte_id unik.
- Edge "segmen" = pasangan (halte_asal, halte_tujuan) per koridor. Edge paralel
  diizinkan saat sepasang halte dilayani oleh lebih dari satu koridor.
- Edge "transit" = self-loop di halte yang muncul di >= 2 koridor. Edge ini
  hanya valid bila state Dijkstra sudah memiliki koridor_aktif yang berbeda
  dengan koridor target.

Sumber bobot kepadatan:
    skor_segmen = min(kepadatan_bus) untuk semua bus aktif di koridor tsb
                  pada (jam, hari_tipe) yang diminta
    skor_jalur  = mean(skor_segmen) * BOBOT_KEPADATAN
                + jumlah_transit     * BOBOT_TRANSIT

Dijkstra hanya peduli "berapa kepadatan TERENDAH yang tersedia di koridor ini
pada jam ini" — pemilihan bus spesifik diserahkan ke services/bus_selector.py
(Algoritma 2) yang dijalankan setelah Dijkstra selesai.

Bobot transit > kepadatan mengacu pada Garcia-Martinez et al. (2018), "Transfer
penalties in multimodal public transport networks", yang menyatakan pure
transfer penalty setara 15.2-17.7 Equivalent In-Vehicle Minutes.

Catatan teknis: karena skor menggunakan mean (bukan sum), cost tidak strict
monoton naik saat edge ditambah. Dijkstra di sini melakukan eksplorasi penuh
sambil melacak best-cost per state (node, koridor_aktif) dan memilih kedatangan
tujuan dengan cost terendah. State dengan cost >= target terbaik dipangkas.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from typing import Any

BOBOT_KEPADATAN: float = 0.40
BOBOT_TRANSIT: float = 0.60
MAKS_TRANSIT_DEFAULT: int = 4  # maksimum 5 koridor (1 boarding + 4 transit)
KEPADATAN_FALLBACK: float = 0.5  # default jika data kepadatan tidak ada


# ----------------------------------------------------------------------
# 1. Data loader
# ----------------------------------------------------------------------

def _fetch_semua(supabase, nama_tabel: str, kolom: str = "*") -> list[dict]:
    """Ambil semua baris dari tabel/view dengan pagination 1000 baris."""
    hasil: list[dict] = []
    offset = 0
    while True:
        chunk = (
            supabase.table(nama_tabel)
            .select(kolom)
            .range(offset, offset + 999)
            .execute()
            .data
        )
        if not chunk:
            break
        hasil.extend(chunk)
        if len(chunk) < 1000:
            break
        offset += 1000
    return hasil


async def load_graph_data(supabase) -> dict[str, Any]:
    """Muat semua data graf dari Supabase. Dijalankan sekali saat startup.

    Return struktur siap pakai untuk build_graph() pada tiap request.

    `kepadatan_bus` adalah tabel realtime (bus_id, koridor_id, jam, hari_tipe,
    kepadatan). Algoritma 1 (Dijkstra) memakai min() per koridor; Algoritma 2
    (bus_selector) memakai nilai per bus. Tabel di-load tolerant — bila belum
    ada, fallback ke KEPADATAN_FALLBACK akan dipakai oleh build_graph.
    """
    segmen = _fetch_semua(supabase, "segmen")
    halte_rows = _fetch_semua(supabase, "halte", "halte_id, nama, lat, lng")
    koridor_halte = _fetch_semua(supabase, "koridor_halte")
    koridor_rows = _fetch_semua(
        supabase, "koridor", "koridor_id, nama_pendek, nama_panjang"
    )

    try:
        kepadatan_bus = _fetch_semua(supabase, "kepadatan_bus")
    except Exception as e:
        print(
            f"[dijkstra] WARNING: gagal muat 'kepadatan_bus' ({e!r}); "
            f"semua bobot kepadatan akan jatuh ke fallback {KEPADATAN_FALLBACK}"
        )
        kepadatan_bus = []

    halte_to_koridor: dict[str, set[int]] = defaultdict(set)
    for row in koridor_halte:
        halte_to_koridor[row["halte_id"]].add(row["koridor_id"])

    return {
        "segmen": segmen,
        "kepadatan_bus": kepadatan_bus,
        "halte": {h["halte_id"]: h for h in halte_rows},
        "koridor_halte": koridor_halte,
        "koridor": {k["koridor_id"]: k for k in koridor_rows},
        "halte_to_koridor": dict(halte_to_koridor),
    }


def get_realtime_kepadatan(
    graph_data: dict[str, Any],
    jam: int,
    hari_tipe: str,
) -> dict[str, float]:
    """Snapshot kepadatan per bus pada (jam, hari_tipe) -> dipakai bus_selector."""
    hasil: dict[str, float] = {}
    for row in graph_data.get("kepadatan_bus", []):
        if row["jam"] != jam or row["hari_tipe"] != hari_tipe:
            continue
        nilai = row.get("kepadatan")
        if nilai is None:
            continue
        hasil[row["bus_id"]] = float(nilai)
    return hasil


# ----------------------------------------------------------------------
# 2. Graph builder
# ----------------------------------------------------------------------

def build_graph(
    graph_data: dict[str, Any],
    jam: int,
    hari_tipe: str,
    segment_crowding: dict[str, float] | None = None,
    daily_mean_by_koridor: dict[Any, float] | None = None,
) -> dict[str, list[dict]]:
    """Bangun adjacency list dengan bobot kepadatan untuk (jam, hari_tipe).

    Operasi murni in-memory; tidak menyentuh Supabase. Bobot tiap edge segmen
    = min(kepadatan) di antara semua bus pada koridor edge tsb (sumber:
    `graph_data["kepadatan_bus"]`). Semua segmen di koridor yang sama berbagi
    bobot — granularitas per-bus diserahkan ke bus_selector setelah Dijkstra.
    """
    # Index: koridor_id -> kepadatan terendah di antara bus aktif pada
    # (jam, hari_tipe). Dijkstra optimis: asumsikan penumpang bisa memilih
    # bus paling sepi yang tersedia di koridor itu.
    kepadatan_per_koridor: dict[int, float] = {}
    for row in graph_data.get("kepadatan_bus", []):
        if row["jam"] != jam or row["hari_tipe"] != hari_tipe:
            continue
        nilai = row.get("kepadatan")
        if nilai is None:
            continue
        kid = row["koridor_id"]
        nilai_f = float(nilai)
        if kid not in kepadatan_per_koridor or nilai_f < kepadatan_per_koridor[kid]:
            kepadatan_per_koridor[kid] = nilai_f

    adjacency: dict[str, list[dict]] = defaultdict(list)

    # Edge "segmen": satu edge per baris segmen.
    # Edge paralel antar koridor terbentuk otomatis karena segmen_id berbeda.
    for seg in graph_data["segmen"]:
        segmen_id = str(seg["segmen_id"])
        koridor_id = seg["koridor_id"]
        if segment_crowding is not None and segmen_id in segment_crowding:
            bobot_kepadatan = segment_crowding[segmen_id]
        elif daily_mean_by_koridor is not None and koridor_id in daily_mean_by_koridor:
            bobot_kepadatan = daily_mean_by_koridor[koridor_id]
        elif daily_mean_by_koridor is not None and str(koridor_id) in daily_mean_by_koridor:
            bobot_kepadatan = daily_mean_by_koridor[str(koridor_id)]
        else:
            bobot_kepadatan = kepadatan_per_koridor.get(koridor_id, KEPADATAN_FALLBACK)

        adjacency[seg["halte_asal"]].append({
            "tujuan": seg["halte_tujuan"],
            "koridor_id": koridor_id,
            "segmen_id": seg["segmen_id"],
            "bobot_kepadatan": bobot_kepadatan,
            "waktu_tempuh_detik": seg["waktu_tempuh_detik"],
            "tipe": "segmen",
        })

    # Edge "transit": self-loop di halte yang dilewati >= 2 koridor.
    # Untuk tiap koridor target, dibuat satu edge transit. Saat traversal,
    # edge hanya valid bila koridor_aktif sudah ada dan berbeda dengan target
    # (lihat _dijkstra_single).
    for halte_id, koridor_set in graph_data["halte_to_koridor"].items():
        if len(koridor_set) < 2:
            continue
        for koridor_target in koridor_set:
            adjacency[halte_id].append({
                "tujuan": halte_id,
                "koridor_id": koridor_target,
                "segmen_id": None,
                "bobot_kepadatan": 0.0,
                "waktu_tempuh_detik": 0,
                "tipe": "transit",
            })

    return dict(adjacency)


# ----------------------------------------------------------------------
# 3. Dijkstra (k-shortest paths versi sederhana)
# ----------------------------------------------------------------------

def _hitung_skor(sum_kepadatan: float, n_segmen: int, transit_count: int) -> float:
    """Skor jalur: (mean kepadatan * 0.40) + (jumlah_transit * 0.60)."""
    rata = (sum_kepadatan / n_segmen) if n_segmen > 0 else 0.0
    return rata * BOBOT_KEPADATAN + transit_count * BOBOT_TRANSIT


def _signature_path(rute: dict) -> tuple:
    """Tuple unik untuk deduplikasi rute (urutan tipe+segmen+koridor)."""
    return tuple(
        (e["tipe"], e.get("segmen_id"), e.get("koridor_id"))
        for e in rute["path"]
    )


def _dijkstra_single(
    graph: dict[str, list[dict]],
    asal: str,
    tujuan: str,
    maks_transit: int,
    edge_diblokir: set[tuple],
) -> dict | None:
    """Cari satu rute dengan cost minimum.

    State dalam heap:
        (cost, counter, node, koridor_aktif,
         transit_count, sum_kepadatan, n_segmen, path)

    counter dipakai sebagai tie-breaker agar heap tidak membandingkan dict.
    `best_cost` memetakan (node, koridor_aktif) -> cost terendah yang sudah
    dilihat, dipakai untuk pruning state usang.
    """
    counter = 0
    heap: list[tuple] = [(0.0, counter, asal, None, 0, 0.0, 0, [])]
    best_cost: dict[tuple, float] = {(asal, None): 0.0}
    target_terbaik: dict | None = None

    while heap:
        cost, _, node, koridor_aktif, transit_count, sum_kep, n_seg, path = heapq.heappop(heap)

        # Pruning: state ini sudah lebih mahal dari target terbaik
        if target_terbaik is not None and cost >= target_terbaik["cost"]:
            continue

        # State usang (sudah ditemukan cost lebih baik untuk node+koridor sama)
        if cost > best_cost.get((node, koridor_aktif), float("inf")) + 1e-9:
            continue

        # Sampai tujuan; hanya valid setelah pernah naik koridor
        if node == tujuan and koridor_aktif is not None:
            if target_terbaik is None or cost < target_terbaik["cost"]:
                target_terbaik = {
                    "cost": cost,
                    "path": path,
                    "transit_count": transit_count,
                    "sum_kepadatan": sum_kep,
                    "n_segmen": n_seg,
                }
            # tidak kembangkan lebih jauh dari tujuan
            continue

        # Batasi kedalaman transit
        if transit_count > maks_transit:
            continue

        for edge in graph.get(node, []):
            kunci_blokir = (
                node, edge["tipe"], edge["segmen_id"], edge["koridor_id"]
            )
            if kunci_blokir in edge_diblokir:
                continue

            if edge["tipe"] == "transit":
                # Transit hanya valid bila sudah punya koridor aktif yang
                # berbeda dengan koridor target (mencegah self-loop tanpa arti).
                if koridor_aktif is None or koridor_aktif == edge["koridor_id"]:
                    continue
                new_koridor = edge["koridor_id"]
                new_transit = transit_count + 1
                new_sum = sum_kep
                new_n = n_seg
            else:
                # Segmen: harus sesuai koridor aktif (kecuali boarding pertama)
                if koridor_aktif is not None and koridor_aktif != edge["koridor_id"]:
                    continue
                new_koridor = edge["koridor_id"]
                new_transit = transit_count
                new_sum = sum_kep + edge["bobot_kepadatan"]
                new_n = n_seg + 1

            new_cost = _hitung_skor(new_sum, new_n, new_transit)
            new_node = edge["tujuan"]
            state = (new_node, new_koridor)

            if new_cost < best_cost.get(state, float("inf")) - 1e-9:
                best_cost[state] = new_cost
                counter += 1
                edge_entry = {**edge, "asal": node}
                heapq.heappush(heap, (
                    new_cost, counter, new_node, new_koridor,
                    new_transit, new_sum, new_n, path + [edge_entry],
                ))

    return target_terbaik


def dijkstra(
    graph: dict[str, list[dict]],
    asal: str,
    tujuan: str,
    k: int = 3,
    maks_transit: int = MAKS_TRANSIT_DEFAULT,
) -> list[dict]:
    """Cari k rute terbaik (k-shortest paths versi sederhana).

    Strategi: jalankan Dijkstra utama untuk rute #1, lalu untuk tiap edge
    segmen pada rute #1 jalankan ulang Dijkstra dengan edge tersebut diblokir.
    Kumpulkan kandidat unik dan urutkan berdasarkan cost.
    Pendekatan ini lebih sederhana dari Yen klasik namun cukup untuk PoC
    karena memberikan diversitas rute dengan kompleksitas linear terhadap
    panjang rute pertama.
    """
    if asal == tujuan:
        raise ValueError("halte_asal dan halte_tujuan tidak boleh sama")
    if asal not in graph:
        return []

    rute_pertama = _dijkstra_single(graph, asal, tujuan, maks_transit, set())
    if rute_pertama is None:
        return []

    hasil: list[dict] = [rute_pertama]
    signature_terlihat: set[tuple] = {_signature_path(rute_pertama)}

    if k <= 1:
        return hasil

    # Kandidat deviasi: blokir satu segmen rute pertama lalu re-run Dijkstra
    kandidat: list[tuple] = []
    cnt = 0
    for edge in rute_pertama["path"]:
        if edge["tipe"] != "segmen":
            continue
        blokir = {(edge["asal"], "segmen", edge["segmen_id"], edge["koridor_id"])}
        alt = _dijkstra_single(graph, asal, tujuan, maks_transit, blokir)
        if alt is None:
            continue
        sig = _signature_path(alt)
        if sig in signature_terlihat:
            continue
        cnt += 1
        heapq.heappush(kandidat, (alt["cost"], cnt, alt, sig))

    while len(hasil) < k and kandidat:
        _, _, alt, sig = heapq.heappop(kandidat)
        if sig in signature_terlihat:
            continue
        signature_terlihat.add(sig)
        hasil.append(alt)

    return hasil


# ----------------------------------------------------------------------
# 4. Path formatter
# ----------------------------------------------------------------------

def format_rute(rute: dict, graph_data: dict) -> dict:
    """Ubah raw path Dijkstra menjadi response siap tampil di kios.

    Edge segmen berurutan pada koridor yang sama digabung jadi satu "naik".
    Setiap edge transit menghasilkan satu marker "transit" terpisah.
    """
    halte_master: dict = graph_data["halte"]
    koridor_master: dict = graph_data["koridor"]

    path: list[dict] = rute["path"]
    segmen_response: list[dict] = []
    grup_aktif: dict | None = None
    total_detik = 0

    def nama_halte(hid: str) -> str:
        return halte_master.get(hid, {}).get("nama", hid)

    def emit_grup(g: dict) -> None:
        nonlocal total_detik
        kor = koridor_master.get(g["koridor_id"], {})
        rata = sum(g["kepadatan_list"]) / len(g["kepadatan_list"])
        segmen_response.append({
            "tipe": "naik",
            "dari": nama_halte(g["asal"]),
            "ke": nama_halte(g["tujuan_akhir"]),
            "naik_di_id": g["asal"],
            "turun_di_id": g["tujuan_akhir"],
            "koridor_id": g["koridor_id"],
            "nama_koridor": kor.get("nama_pendek") or kor.get("nama_panjang"),
            "naik_di": nama_halte(g["asal"]),
            "turun_di": nama_halte(g["tujuan_akhir"]),
            "kepadatan": round(rata, 3),
            "waktu_menit": round(g["waktu_total_detik"] / 60),
            "jumlah_segmen": len(g["kepadatan_list"]),
            # Detail per-segmen halus untuk plot polyline warna-warni di peta.
            # Tiap entry = satu edge halte->halte dalam koridor yang sama.
            "segmen_detail": g["segmen_detail"],
        })
        total_detik += g["waktu_total_detik"]

    for edge in path:
        if edge["tipe"] == "transit":
            if grup_aktif is not None:
                emit_grup(grup_aktif)
                grup_aktif = None
            segmen_response.append({
                "tipe": "transit",
                "transit_di": nama_halte(edge["asal"]),
                "transit_di_id": edge["asal"],
                "dari_koridor": None,  # diisi di pass kedua
                "ke_koridor": edge["koridor_id"],
            })
        else:  # segmen
            if grup_aktif is None or grup_aktif["koridor_id"] != edge["koridor_id"]:
                if grup_aktif is not None:
                    emit_grup(grup_aktif)
                grup_aktif = {
                    "koridor_id": edge["koridor_id"],
                    "asal": edge["asal"],
                    "tujuan_akhir": edge["tujuan"],
                    "kepadatan_list": [edge["bobot_kepadatan"]],
                    "waktu_total_detik": edge["waktu_tempuh_detik"],
                    "segmen_detail": [{
                        "dari_id": edge["asal"],
                        "ke_id": edge["tujuan"],
                        "kepadatan": round(edge["bobot_kepadatan"], 3),
                        "waktu_menit": round(edge["waktu_tempuh_detik"] / 60),
                    }],
                }
            else:
                grup_aktif["tujuan_akhir"] = edge["tujuan"]
                grup_aktif["kepadatan_list"].append(edge["bobot_kepadatan"])
                grup_aktif["waktu_total_detik"] += edge["waktu_tempuh_detik"]
                grup_aktif["segmen_detail"].append({
                    "dari_id": edge["asal"],
                    "ke_id": edge["tujuan"],
                    "kepadatan": round(edge["bobot_kepadatan"], 3),
                    "waktu_menit": round(edge["waktu_tempuh_detik"] / 60),
                })

    if grup_aktif is not None:
        emit_grup(grup_aktif)

    # Pass kedua: isi dari_koridor pada transit marker berdasarkan grup sebelumnya
    for i, item in enumerate(segmen_response):
        if item.get("tipe") != "transit":
            continue
        for j in range(i - 1, -1, -1):
            if segmen_response[j].get("tipe") == "naik":
                item["dari_koridor"] = segmen_response[j]["koridor_id"]
                break

    rata_kepadatan = (
        rute["sum_kepadatan"] / rute["n_segmen"] if rute["n_segmen"] else 0.0
    )

    return {
        "skor": round(rute["cost"], 4),
        "jumlah_transit": rute["transit_count"],
        "estimasi_menit": round(total_detik / 60),
        "rata_kepadatan": round(rata_kepadatan, 3),
        "segmen": segmen_response,
    }
