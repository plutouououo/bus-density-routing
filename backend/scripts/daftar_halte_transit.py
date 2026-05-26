"""Inspeksi data: daftar halte yang menjadi titik transit antar koridor.

Output:
1. Semua halte yang dilayani >= 2 koridor (kandidat transit point).
2. Untuk tiap pasangan koridor, contoh pasangan (asal, tujuan) yang
   pasti membutuhkan transit (asal eksklusif di K_a, tujuan eksklusif di K_b,
   tidak ada koridor bersama).
3. Beberapa contoh pasangan yang TIDAK butuh transit (asal & tujuan share
   minimal satu koridor) sebagai pembanding.

Jalankan dari folder backend/:
    ./venv/Scripts/python.exe scripts/daftar_halte_transit.py
"""

import asyncio
from collections import defaultdict
from itertools import combinations
import sys
from pathlib import Path

# Pastikan import service dari root backend/
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.dijkstra import load_graph_data
from services.supabase_client import get_client


async def main() -> None:
    sb = get_client()
    print("Memuat data graf dari Supabase ...")
    data = await load_graph_data(sb)

    halte_master = data["halte"]
    halte_to_koridor: dict[str, set[int]] = data["halte_to_koridor"]

    # 1. Halte transit (>= 2 koridor)
    halte_transit = {
        hid: kor for hid, kor in halte_to_koridor.items() if len(kor) >= 2
    }

    print(f"\n=== 1. Halte transit (dilayani >= 2 koridor): {len(halte_transit)} halte ===")
    for hid, kor in sorted(halte_transit.items(), key=lambda x: -len(x[1])):
        nama = halte_master.get(hid, {}).get("nama", hid)
        print(f"  {hid:8s}  {nama:35s}  koridor: {sorted(kor)}")

    # 2. Inverse map: koridor_id -> set halte_id
    koridor_to_halte: dict[int, set[str]] = defaultdict(set)
    for hid, kor_set in halte_to_koridor.items():
        for k in kor_set:
            koridor_to_halte[k].add(hid)

    # Halte eksklusif tiap koridor (tidak overlap dengan koridor lain)
    halte_eksklusif: dict[int, list[str]] = {}
    for k, halte_set in koridor_to_halte.items():
        eksklusif = [h for h in halte_set if len(halte_to_koridor.get(h, set())) == 1]
        halte_eksklusif[k] = eksklusif

    print(f"\n=== 2. Contoh pasangan yang BUTUH transit (1 transit) ===")
    print("    (asal di koridor A eksklusif, tujuan di koridor B eksklusif,")
    print("     A dan B share minimal 1 halte transit)\n")

    koridor_list = sorted(koridor_to_halte.keys())
    contoh_per_pasangan = 3  # batas contoh per pasangan koridor

    for k_a, k_b in combinations(koridor_list, 2):
        # Cek apakah ada halte transit yang menghubungkan k_a dan k_b
        penghubung = [
            (hid, halte_master.get(hid, {}).get("nama", hid))
            for hid, kor in halte_transit.items()
            if k_a in kor and k_b in kor
        ]
        if not penghubung:
            continue

        asal_kandidat = halte_eksklusif.get(k_a, [])[:contoh_per_pasangan]
        tujuan_kandidat = halte_eksklusif.get(k_b, [])[:contoh_per_pasangan]
        if not asal_kandidat or not tujuan_kandidat:
            continue

        nama_penghubung = ", ".join(f"{n} ({h})" for h, n in penghubung[:3])
        print(f"  Koridor {k_a} -> Koridor {k_b}")
        print(f"    Transit via: {nama_penghubung}")
        for asal_id in asal_kandidat:
            asal_nama = halte_master.get(asal_id, {}).get("nama", asal_id)
            for tujuan_id in tujuan_kandidat:
                tujuan_nama = halte_master.get(tujuan_id, {}).get("nama", tujuan_id)
                print(
                    f"      {asal_id} ({asal_nama:30s}) "
                    f"-> {tujuan_id} ({tujuan_nama})"
                )
        print()

    print(f"\n=== 3. Contoh pasangan yang TIDAK butuh transit (pembanding) ===")
    print("    (asal & tujuan share minimal 1 koridor yang sama)\n")
    contoh = 0
    for k in koridor_list:
        halte_di_k = list(koridor_to_halte[k])
        if len(halte_di_k) < 2:
            continue
        a, b = halte_di_k[0], halte_di_k[-1]
        nama_a = halte_master.get(a, {}).get("nama", a)
        nama_b = halte_master.get(b, {}).get("nama", b)
        print(f"  Koridor {k}: {a} ({nama_a}) -> {b} ({nama_b})")
        contoh += 1
        if contoh >= 5:
            break


if __name__ == "__main__":
    asyncio.run(main())
