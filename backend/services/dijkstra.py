"""Service rekomendasi rute Dijkstra untuk TransJakarta (Algoritma 1).

Graf dimodelkan sebagai *directed graph*:
- Node = halte_id unik.
- Edge "segmen" = pasangan (halte_asal, halte_tujuan) per koridor. Edge paralel
  diizinkan saat sepasang halte dilayani oleh lebih dari satu koridor.
- Edge "transit" = self-loop di halte yang muncul di >= 2 koridor. Edge ini
  hanya valid bila state Dijkstra sudah memiliki koridor_aktif yang berbeda
  dengan koridor target.

Alur ranking dua fase:
    fase 1 = estimasi waktu tempuh, total jarak, lalu jumlah transfer
    fase 2 = kepadatan bus/rute setelah bus spesifik dipilih

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

from services.geo import distance_meters

MAKS_TRANSIT_DEFAULT: int = 4  # maksimum 5 koridor (1 boarding + 4 transit)
KEPADATAN_FALLBACK: float = 0.5  # default jika data kepadatan tidak ada
KANDIDAT_RUTE_DEFAULT: int = 5
PRIMARY_WEIGHT_TIME: float = 0.35
PRIMARY_WEIGHT_DISTANCE: float = 0.25
PRIMARY_WEIGHT_TRANSFER: float = 0.40
SCOPED_KORIDOR: set[str] = {"1", "2", "3", "4", "5"}
HALTE_ALIAS_RADIUS_METER: float = 80.0


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
    halte_by_id = {h["halte_id"]: h for h in halte_rows}
    koridor_halte = _fetch_semua(supabase, "koridor_halte")
    existing_halte_to_koridor: dict[str, set[int]] = defaultdict(set)
    for row in koridor_halte:
        existing_halte_to_koridor[str(row["halte_id"])].add(row["koridor_id"])
    koridor_rows = _fetch_semua(
        supabase, "koridor", "koridor_id, nama_pendek, nama_panjang"
    )
    try:
        gtfs_trips = _fetch_semua(
            supabase,
            "gtfs_trips",
            "trip_id, route_id, direction_id, trip_headsign",
        )
        gtfs_stop_times = _fetch_semua(
            supabase,
            "gtfs_stop_times",
            "trip_id, stop_id, arrival_time, departure_time, stop_sequence",
        )
        segmen, synthetic_koridor_halte = _augment_missing_gtfs_segments(
            segmen, halte_by_id, existing_halte_to_koridor, gtfs_trips, gtfs_stop_times
        )
        koridor_halte = koridor_halte + synthetic_koridor_halte
    except Exception as e:
        print(
            f"[dijkstra] WARNING: gagal membangun fallback segmen dari GTFS ({e!r}); "
            "routing hanya memakai tabel segmen"
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
        "halte": halte_by_id,
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
    halte_master: dict = graph_data["halte"]

    def jarak_segmen_meter(halte_asal: str, halte_tujuan: str) -> float:
        asal = halte_master[halte_asal]
        tujuan = halte_master[halte_tujuan]
        return distance_meters(asal["lat"], asal["lng"], tujuan["lat"], tujuan["lng"])

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

        jarak_meter = jarak_segmen_meter(seg["halte_asal"], seg["halte_tujuan"])
        adjacency[seg["halte_asal"]].append({
            "tujuan": seg["halte_tujuan"],
            "koridor_id": koridor_id,
            "segmen_id": seg["segmen_id"],
            "bobot_kepadatan": bobot_kepadatan,
            "waktu_tempuh_detik": seg["waktu_tempuh_detik"],
            "jarak_meter": jarak_meter,
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
                "jarak_meter": 0.0,
                "tipe": "transit",
            })

    # Edge "transit" antar-platform: beberapa halte punya dua ID untuk arah
    # berlawanan pada nama/lokasi yang sama (mis. Pecenongan, Petojo,
    # Kebon Sirih Arah Utara/Selatan). Tanpa edge ini, Dijkstra bisa memilih
    # naik bus memutar hanya untuk mencapai platform sebelah.
    halte_ids = list(halte_master.keys())
    for i, halte_a_id in enumerate(halte_ids):
        halte_a = halte_master[halte_a_id]
        name_a = _normalized_transfer_halte_name(halte_a.get("nama"))
        if not name_a:
            continue
        for halte_b_id in halte_ids[i + 1:]:
            halte_b = halte_master[halte_b_id]
            if _normalized_transfer_halte_name(halte_b.get("nama")) != name_a:
                continue
            try:
                jarak_platform = distance_meters(
                    halte_a["lat"], halte_a["lng"], halte_b["lat"], halte_b["lng"]
                )
            except (TypeError, ValueError, KeyError):
                continue
            if jarak_platform > HALTE_ALIAS_RADIUS_METER:
                continue

            for source_id, target_id in ((halte_a_id, halte_b_id), (halte_b_id, halte_a_id)):
                for koridor_target in graph_data["halte_to_koridor"].get(target_id, set()):
                    adjacency[source_id].append({
                        "tujuan": target_id,
                        "koridor_id": koridor_target,
                        "segmen_id": None,
                        "bobot_kepadatan": 0.0,
                        "waktu_tempuh_detik": 0,
                        "jarak_meter": 0.0,
                        "tipe": "transit",
                    })

    return dict(adjacency)


# ----------------------------------------------------------------------
# 3. Dijkstra (k-shortest paths versi sederhana)
# ----------------------------------------------------------------------

def _candidate_metrics(rute: dict) -> dict[str, float]:
    """Atribut evaluasi kandidat sebelum pemilihan bus spesifik."""
    rata_kepadatan = (
        rute["sum_kepadatan"] / rute["n_segmen"] if rute["n_segmen"] else 0.0
    )
    return {
        "rata_kepadatan": rata_kepadatan,
    }


def _normalize_minmax(value: float, min_value: float, max_value: float) -> float:
    if abs(max_value - min_value) <= 1e-9:
        return 0.0
    return (value - min_value) / (max_value - min_value)


def _apply_candidate_metrics(rute: dict) -> dict:
    rute.update(_candidate_metrics(rute))
    return rute


def _apply_primary_ranking(hasil: list[dict], maks_transit: int) -> None:
    if not hasil:
        return

    waktu_values = [float(r.get("total_waktu_detik", 0.0)) for r in hasil]
    jarak_values = [float(r.get("total_jarak_meter", 0.0)) for r in hasil]
    min_waktu, max_waktu = min(waktu_values), max(waktu_values)
    min_jarak, max_jarak = min(jarak_values), max(jarak_values)

    for rute in hasil:
        waktu_norm = _normalize_minmax(
            float(rute.get("total_waktu_detik", 0.0)), min_waktu, max_waktu
        )
        jarak_norm = _normalize_minmax(
            float(rute.get("total_jarak_meter", 0.0)), min_jarak, max_jarak
        )
        transfer_norm = min(rute["transit_count"] / max(1, maks_transit), 1.0)
        primary_score = (
            PRIMARY_WEIGHT_TIME * waktu_norm
            + PRIMARY_WEIGHT_DISTANCE * jarak_norm
            + PRIMARY_WEIGHT_TRANSFER * transfer_norm
        )
        rute["primary_score"] = primary_score
        rute["time_norm"] = waktu_norm
        rute["distance_norm"] = jarak_norm
        rute["transfer_norm"] = transfer_norm


def _signature_path(rute: dict) -> tuple:
    """Tuple unik untuk deduplikasi rute (urutan halte+segmen+koridor)."""
    return tuple(
        (e.get("asal"), e.get("tujuan"), e["tipe"], e.get("segmen_id"), e.get("koridor_id"))
        for e in rute["path"]
    )


def _koridor_sequence(path: list[dict]) -> list[Any]:
    sequence: list[Any] = []
    for edge in path:
        if edge.get("tipe") != "segmen":
            continue
        koridor_id = edge.get("koridor_id")
        if not sequence or sequence[-1] != koridor_id:
            sequence.append(koridor_id)
    return sequence


def _has_repeated_koridor(rute: dict) -> bool:
    """Tolak kandidat yang keluar lalu masuk lagi ke koridor yang sama."""
    sequence = _koridor_sequence(rute["path"])
    return len(sequence) != len(set(sequence))


def _edge_block_key(edge: dict) -> tuple:
    return (edge["asal"], edge["tipe"], edge["segmen_id"], edge["koridor_id"])


def _build_reverse_graph(graph: dict[str, list[dict]]) -> dict[str, list[dict]]:
    reverse_graph: dict[str, list[dict]] = defaultdict(list)
    for asal, edges in graph.items():
        for edge in edges:
            reverse_graph[edge["tujuan"]].append({**edge, "asal": asal})
    return dict(reverse_graph)


def _metrics_if_valid_path(path: list[dict], maks_transit: int) -> dict | None:
    koridor_aktif = None
    transit_count = 0
    sum_kep = 0.0
    n_seg = 0
    total_jarak_meter = 0.0
    total_waktu_detik = 0

    for idx, edge in enumerate(path):
        if idx > 0 and path[idx - 1]["tujuan"] != edge["asal"]:
            return None

        if edge["tipe"] == "transit":
            if idx > 0 and path[idx - 1].get("tipe") == "transit":
                return None
            if koridor_aktif is None or koridor_aktif == edge["koridor_id"]:
                return None
            koridor_aktif = edge["koridor_id"]
            transit_count += 1
        else:
            if koridor_aktif is not None and koridor_aktif != edge["koridor_id"]:
                return None
            koridor_aktif = edge["koridor_id"]
            sum_kep += edge["bobot_kepadatan"]
            n_seg += 1

        if transit_count > maks_transit:
            return None
        total_jarak_meter += float(edge.get("jarak_meter", 0.0))
        total_waktu_detik += int(edge.get("waktu_tempuh_detik", 0) or 0)

    if koridor_aktif is None:
        return None
    if path and path[-1].get("tipe") == "transit":
        return None

    return {
        "cost": total_jarak_meter,
        "path": path,
        "transit_count": transit_count,
        "sum_kepadatan": sum_kep,
        "n_segmen": n_seg,
        "total_jarak_meter": total_jarak_meter,
        "total_waktu_detik": total_waktu_detik,
    }


def _bidirectional_dijkstra_single(
    graph: dict[str, list[dict]],
    reverse_graph: dict[str, list[dict]],
    asal: str,
    tujuan: str,
    maks_transit: int,
    edge_diblokir: set[tuple],
) -> dict | None:
    """Cari kandidat shortest path dengan bidirectional Dijkstra.

    Search dua arah dipakai untuk candidate generation. Karena validitas rute
    TransJakarta bergantung pada state koridor/transit, path gabungan selalu
    divalidasi ulang memakai aturan traversal yang sama dengan Dijkstra lama.
    """
    counter = 0
    forward_heap: list[tuple] = [(0.0, counter, asal, [])]
    backward_heap: list[tuple] = [(0.0, counter, tujuan, [])]
    best_forward: dict[str, float] = {asal: 0.0}
    best_backward: dict[str, float] = {tujuan: 0.0}
    path_forward: dict[str, list[dict]] = {asal: []}
    path_backward: dict[str, list[dict]] = {tujuan: []}
    target_terbaik: dict | None = None

    def maybe_update(node: str) -> None:
        nonlocal target_terbaik
        if node not in path_forward or node not in path_backward:
            return
        full_path = path_forward[node] + path_backward[node]
        if any(_edge_block_key(edge) in edge_diblokir for edge in full_path):
            return
        kandidat = _metrics_if_valid_path(full_path, maks_transit)
        if kandidat is None:
            return
        if target_terbaik is None or kandidat["cost"] < target_terbaik["cost"]:
            target_terbaik = kandidat

    while forward_heap and backward_heap:
        lower_bound = forward_heap[0][0] + backward_heap[0][0]
        if target_terbaik is not None and lower_bound >= target_terbaik["cost"]:
            break

        if forward_heap[0][0] <= backward_heap[0][0]:
            cost, _, node, path = heapq.heappop(forward_heap)
            if cost > best_forward.get(node, float("inf")) + 1e-9:
                continue
            maybe_update(node)
            for edge in graph.get(node, []):
                edge_entry = {**edge, "asal": node}
                if _edge_block_key(edge_entry) in edge_diblokir:
                    continue
                new_node = edge["tujuan"]
                new_cost = cost + float(edge.get("jarak_meter", 0.0))
                if new_cost < best_forward.get(new_node, float("inf")) - 1e-9:
                    best_forward[new_node] = new_cost
                    path_forward[new_node] = path + [edge_entry]
                    counter += 1
                    heapq.heappush(
                        forward_heap,
                        (new_cost, counter, new_node, path_forward[new_node]),
                    )
        else:
            cost, _, node, path_to_tujuan = heapq.heappop(backward_heap)
            if cost > best_backward.get(node, float("inf")) + 1e-9:
                continue
            maybe_update(node)
            for edge in reverse_graph.get(node, []):
                if _edge_block_key(edge) in edge_diblokir:
                    continue
                new_node = edge["asal"]
                new_cost = cost + float(edge.get("jarak_meter", 0.0))
                if new_cost < best_backward.get(new_node, float("inf")) - 1e-9:
                    best_backward[new_node] = new_cost
                    path_backward[new_node] = [edge] + path_to_tujuan
                    counter += 1
                    heapq.heappush(
                        backward_heap,
                        (new_cost, counter, new_node, path_backward[new_node]),
                    )

    for node in set(path_forward).intersection(path_backward):
        maybe_update(node)
    return target_terbaik


def _dijkstra_single(
    graph: dict[str, list[dict]],
    asal: str,
    tujuan: str,
    maks_transit: int,
    edge_diblokir: set[tuple],
) -> dict | None:
    """Cari satu rute dengan cost jarak minimum.

    State dalam heap:
        (cost, counter, node, koridor_aktif,
         transit_count, sum_kepadatan, n_segmen,
         total_jarak_meter, total_waktu_detik, path)

    counter dipakai sebagai tie-breaker agar heap tidak membandingkan dict.
    `best_cost` memetakan (node, koridor_aktif) -> cost terendah yang sudah
    dilihat, dipakai untuk pruning state usang.
    """
    counter = 0
    heap: list[tuple] = [(0.0, counter, asal, None, 0, 0.0, 0, 0.0, 0, [])]
    best_cost: dict[tuple, float] = {(asal, None): 0.0}
    target_terbaik: dict | None = None

    while heap:
        (
            cost, _, node, koridor_aktif, transit_count, sum_kep, n_seg,
            total_jarak_meter, total_waktu_detik, path,
        ) = heapq.heappop(heap)

        # Pruning: state ini sudah lebih mahal dari target terbaik
        if target_terbaik is not None and cost >= target_terbaik["cost"]:
            continue

        # State usang (sudah ditemukan cost lebih baik untuk node+koridor sama)
        if cost > best_cost.get((node, koridor_aktif), float("inf")) + 1e-9:
            continue

        # Sampai tujuan; hanya valid setelah pernah naik koridor
        if node == tujuan and koridor_aktif is not None:
            if path and path[-1].get("tipe") == "transit":
                continue
            if target_terbaik is None or cost < target_terbaik["cost"]:
                target_terbaik = {
                    "cost": cost,
                    "path": path,
                    "transit_count": transit_count,
                    "sum_kepadatan": sum_kep,
                    "n_segmen": n_seg,
                    "total_jarak_meter": total_jarak_meter,
                    "total_waktu_detik": total_waktu_detik,
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
                if path and path[-1].get("tipe") == "transit":
                    continue
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

            edge_jarak = float(edge.get("jarak_meter", 0.0))
            edge_waktu = int(edge.get("waktu_tempuh_detik", 0) or 0)
            new_cost = cost + edge_jarak
            new_jarak = total_jarak_meter + edge_jarak
            new_waktu = total_waktu_detik + edge_waktu
            new_node = edge["tujuan"]
            state = (new_node, new_koridor)

            if new_cost < best_cost.get(state, float("inf")) - 1e-9:
                best_cost[state] = new_cost
                counter += 1
                edge_entry = {**edge, "asal": node}
                heapq.heappush(heap, (
                    new_cost, counter, new_node, new_koridor,
                    new_transit, new_sum, new_n, new_jarak, new_waktu,
                    path + [edge_entry],
                ))

    return target_terbaik


def dijkstra(
    graph: dict[str, list[dict]],
    asal: str,
    tujuan: str,
    k: int = KANDIDAT_RUTE_DEFAULT,
    maks_transit: int = MAKS_TRANSIT_DEFAULT,
) -> list[dict]:
    """Cari k kandidat rute.

    Strategi: jalankan Dijkstra utama untuk rute #1, lalu untuk tiap edge
    segmen pada rute #1 jalankan ulang Dijkstra dengan edge tersebut diblokir.
    Kumpulkan kandidat unik dan urutkan fase awal berdasarkan waktu tempuh,
    total jarak, lalu jumlah transfer.
    Pendekatan ini lebih sederhana dari Yen klasik namun cukup untuk PoC
    karena memberikan diversitas rute dengan kompleksitas linear terhadap
    panjang rute pertama.
    """
    if asal == tujuan:
        raise ValueError("halte_asal dan halte_tujuan tidak boleh sama")
    if asal not in graph:
        return []

    reverse_graph = _build_reverse_graph(graph)

    rute_pertama = _bidirectional_dijkstra_single(
        graph, reverse_graph, asal, tujuan, maks_transit, set()
    )
    if rute_pertama is None:
        rute_pertama = _dijkstra_single(graph, asal, tujuan, maks_transit, set())
    if rute_pertama is None:
        return []
    if _has_repeated_koridor(rute_pertama):
        return []

    hasil: list[dict] = [rute_pertama]
    signature_terlihat: set[tuple] = {_signature_path(rute_pertama)}

    if k <= 1:
        return [_apply_candidate_metrics(rute_pertama)]

    # Kandidat deviasi: blokir satu segmen rute pertama lalu re-run Dijkstra
    kandidat: list[tuple] = []
    cnt = 0
    for edge in rute_pertama["path"]:
        if edge["tipe"] != "segmen":
            continue
        blokir = {_edge_block_key(edge)}
        alt = _bidirectional_dijkstra_single(
            graph, reverse_graph, asal, tujuan, maks_transit, blokir
        )
        if alt is None:
            alt = _dijkstra_single(graph, asal, tujuan, maks_transit, blokir)
        if alt is None:
            continue
        if _has_repeated_koridor(alt):
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

    for rute in hasil:
        _apply_candidate_metrics(rute)
    _apply_primary_ranking(hasil, maks_transit)

    hasil.sort(key=lambda r: (
        r["primary_score"],
    ))
    return hasil


def _normalize_koridor_id(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def _normalized_transfer_halte_name(value: Any) -> str:
    text = " ".join(str(value or "").lower().split())
    for suffix in (" arah utara", " arah selatan", " arah barat", " arah timur"):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def _parse_gtfs_time(value: Any) -> int | None:
    if value is None:
        return None
    parts = str(value).strip().split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(p) for p in parts)
    except ValueError:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _synthetic_segment_time_detik(
    a: dict,
    b: dict,
    halte_by_id: dict[str, dict],
) -> int:
    dep = _parse_gtfs_time(a.get("departure_time"))
    arr = _parse_gtfs_time(b.get("arrival_time"))
    if dep is not None and arr is not None and arr > dep:
        return max(30, arr - dep)

    halte_a = halte_by_id.get(str(a.get("stop_id")))
    halte_b = halte_by_id.get(str(b.get("stop_id")))
    if halte_a and halte_b:
        jarak = distance_meters(halte_a["lat"], halte_a["lng"], halte_b["lat"], halte_b["lng"])
        # Fallback konservatif: 18 km/jam termasuk perlambatan/dwell pendek.
        return max(30, round(jarak / 5.0))
    return 60


def _augment_missing_gtfs_segments(
    segmen: list[dict],
    halte_by_id: dict[str, dict],
    existing_halte_to_koridor: dict[str, set[int]],
    gtfs_trips: list[dict],
    gtfs_stop_times: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Tambah edge in-memory untuk pasangan GTFS yang belum ada di tabel segmen."""
    existing_pairs = {
        (
            _normalize_koridor_id(row.get("koridor_id")),
            str(row.get("halte_asal")),
            str(row.get("halte_tujuan")),
        )
        for row in segmen
    }
    trips_by_id = {
        str(row.get("trip_id")): row
        for row in gtfs_trips
        if _normalize_koridor_id(row.get("route_id")) in SCOPED_KORIDOR
    }
    stop_times_by_trip: dict[str, list[dict]] = defaultdict(list)
    for row in gtfs_stop_times:
        tid = str(row.get("trip_id"))
        if tid in trips_by_id:
            stop_times_by_trip[tid].append(row)

    synthetic_by_pair: dict[tuple[str, str, str], dict] = {}
    synthetic_koridor_halte: dict[tuple[str, str], dict] = {}
    for tid, rows in stop_times_by_trip.items():
        rows.sort(key=lambda r: int(r.get("stop_sequence") or 0))
        trip = trips_by_id[tid]
        kid = _normalize_koridor_id(trip.get("route_id"))
        try:
            koridor_id: int | str = int(kid)
        except ValueError:
            koridor_id = kid

        for row in rows:
            halte_id = str(row.get("stop_id"))
            # Tambahkan membership hanya untuk halte yang belum punya
            # membership sama sekali di graph utama. Kalau halte sudah punya
            # koridor dari tabel koridor_halte, jangan ubah arti titik transit.
            if halte_id in halte_by_id and not existing_halte_to_koridor.get(halte_id):
                synthetic_koridor_halte[(kid, halte_id)] = {
                    "koridor_id": koridor_id,
                    "halte_id": halte_id,
                }

        for a, b in zip(rows, rows[1:]):
            halte_asal = str(a.get("stop_id"))
            halte_tujuan = str(b.get("stop_id"))
            if halte_asal not in halte_by_id or halte_tujuan not in halte_by_id:
                continue
            memberships_asal = existing_halte_to_koridor.get(halte_asal, set())
            memberships_tujuan = existing_halte_to_koridor.get(halte_tujuan, set())
            kid_int = int(kid) if kid.isdigit() else kid
            if memberships_asal and kid_int not in memberships_asal:
                continue
            if memberships_tujuan and kid_int not in memberships_tujuan:
                continue
            key = (kid, halte_asal, halte_tujuan)
            if key in existing_pairs or key in synthetic_by_pair:
                continue
            synthetic_by_pair[key] = {
                "segmen_id": f"gtfs:{kid}:{halte_asal}:{halte_tujuan}",
                "koridor_id": koridor_id,
                "halte_asal": halte_asal,
                "halte_tujuan": halte_tujuan,
                "waktu_tempuh_detik": _synthetic_segment_time_detik(a, b, halte_by_id),
                "sumber": "gtfs_stop_times_fallback",
            }

    if synthetic_by_pair:
        print(
            "[dijkstra] gtfs_missing_segment_fallback "
            f"added_segments={len(synthetic_by_pair)}"
        )
    return segmen + list(synthetic_by_pair.values()), list(synthetic_koridor_halte.values())


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
        jarak_total = sum(g["jarak_list"])
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
            "jarak_meter": round(jarak_total, 2),
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
                    "jarak_list": [edge.get("jarak_meter", 0.0)],
                    "waktu_total_detik": edge["waktu_tempuh_detik"],
                    "segmen_detail": [{
                        "dari_id": edge["asal"],
                        "ke_id": edge["tujuan"],
                        "kepadatan": round(edge["bobot_kepadatan"], 3),
                        "waktu_menit": round(edge["waktu_tempuh_detik"] / 60),
                        "jarak_meter": round(edge.get("jarak_meter", 0.0), 2),
                    }],
                }
            else:
                grup_aktif["tujuan_akhir"] = edge["tujuan"]
                grup_aktif["kepadatan_list"].append(edge["bobot_kepadatan"])
                grup_aktif["jarak_list"].append(edge.get("jarak_meter", 0.0))
                grup_aktif["waktu_total_detik"] += edge["waktu_tempuh_detik"]
                grup_aktif["segmen_detail"].append({
                    "dari_id": edge["asal"],
                    "ke_id": edge["tujuan"],
                    "kepadatan": round(edge["bobot_kepadatan"], 3),
                    "waktu_menit": round(edge["waktu_tempuh_detik"] / 60),
                    "jarak_meter": round(edge.get("jarak_meter", 0.0), 2),
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

    rata_kepadatan = rute.get("rata_kepadatan")
    if rata_kepadatan is None:
        rata_kepadatan = (
            rute["sum_kepadatan"] / rute["n_segmen"] if rute["n_segmen"] else 0.0
        )
    total_jarak_meter = round(rute.get("total_jarak_meter", 0.0), 2)
    estimasi_menit = round(rute.get("total_waktu_detik", total_detik) / 60)

    return {
        # Field legacy untuk frontend lama. Pada metode dua fase, skor numerik
        # diisi skor primary ranking.
        "skor": round(rute.get("primary_score", 0.0), 4),
        "primary_score": round(rute.get("primary_score", 0.0), 4),
        "total_jarak_meter": total_jarak_meter,
        "jumlah_transit": rute["transit_count"],
        "estimasi_menit": estimasi_menit,
        "rata_kepadatan": round(rata_kepadatan, 3),
        "ranking_method": "primary_score_then_density_rerank",
        "ranking_phase_1": {
            "estimasi_menit": estimasi_menit,
            "total_jarak_meter": total_jarak_meter,
            "jumlah_transit": rute["transit_count"],
            "time_norm": round(rute.get("time_norm", 0.0), 3),
            "distance_norm": round(rute.get("distance_norm", 0.0), 3),
            "transfer_norm": round(rute.get("transfer_norm", 0.0), 3),
            "primary_score": round(rute.get("primary_score", 0.0), 4),
            "weights": {
                "time": PRIMARY_WEIGHT_TIME,
                "distance": PRIMARY_WEIGHT_DISTANCE,
                "transfer": PRIMARY_WEIGHT_TRANSFER,
            },
        },
        "ranking_phase_2": {
            "rata_kepadatan": round(rata_kepadatan, 3),
            "density_norm": round(min(rata_kepadatan, 1.0), 3),
        },
        "segmen": segmen_response,
    }
