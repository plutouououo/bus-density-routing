# TransJakarta Monte Carlo Simulation Backend

Dokumen ini menjelaskan lapisan Monte Carlo di atas simulasi backend yang sudah ada. Baseline deterministik tetap dipakai untuk membangun trip instance GTFS, tetapi hasil crowding sekarang dieksperimenkan ulang melalui beberapa replikasi agar deliverable akhirnya berupa distribusi load factor, bukan satu kali run.

Simulasi tetap dibatasi pada koridor `1`, `2`, `3`, `4`, dan `5`.

## Hubungan dengan Simulasi Baseline

Pipeline baseline masih berjalan seperti biasa:

- GTFS frequency membentuk trip instance deterministik.
- Ridership harian koridor menjadi sumber demand.
- Load factor trip dan segment dihitung dari alokasi penumpang.
- Dijkstra dan bus selector tetap memakai hasil crowding sebagai edge weight dan density input.

Lapisan Monte Carlo menambahkan:

- replika independen untuk satu skenario tetap,
- seed policy yang bisa direproduksi,
- agregasi statistik load factor,
- analisis sensitivitas routing atas snapshot crowding tiap replikasi.

## Sumber Data

Sumber data utama sama dengan simulasi baseline:

| Tabel | Fungsi |
| --- | --- |
| `halte` | Metadata halte dan koordinat. |
| `koridor` | Metadata koridor. |
| `koridor_halte` | Membership halte per koridor. |
| `segmen` | Edge graph untuk Dijkstra. |
| `shapes` | Polyline visual peta dan path bus. |
| `gtfs_trips` | Pola trip GTFS dan arah koridor. |
| `gtfs_frequencies` | Headway dan jam aktif layanan. |
| `gtfs_stop_times` | Template stop time dan urutan halte. |
| `ridership_harian_turunan` | Demand harian per tanggal dan koridor. |

## Konfigurasi Utama

Konfigurasi utama ada di `backend/services/gtfs_simulation.py` dan `backend/services/monte_carlo.py`.

| Setting | Nilai |
| --- | --- |
| `SCOPED_KORIDOR` | `{"1", "2", "3", "4", "5"}` |
| `BUS_CAPACITY` | `80` penumpang |
| `FALLBACK_LOAD_FACTOR` | `0.5` |
| `MC_DEFAULT_REPLICATIONS` | `100` |
| `MC_POISSON_SCALE_FACTOR` | `25` |
| `MC_SEGMENT_POISSON_LAMBDA` | `1.0` |
| `SIMULATION_RUN_ID` | env value atau `default` |

## Istilah Penting

| Istilah | Arti |
| --- | --- |
| `trip_instance_id` | Satu keberangkatan hasil GTFS frequency. |
| `bus_id` | ID kompatibilitas frontend, nilainya sama dengan `trip_instance_id`. |
| `simulation_run_id` | ID run deterministik untuk baseline. |
| `master_seed` | Seed utama Monte Carlo. |
| `replication_seed` | Seed turunan per replikasi. |
| `active_trip_instances` | Trip instance yang aktif pada `sim_time`. |
| `LFtrip` | Load factor per trip. |
| `LFsegment` | Load factor per segment. |

## Arsitektur Simulasi

Ada dua lapisan yang perlu dibedakan:

1. Simulasi baseline deterministik.
2. Monte Carlo experiment di atas baseline yang sama.

Baseline masih dipakai untuk membangun trip instance dan struktur path. Monte Carlo hanya mengubah sampling crowding, bukan schedule, bukan `Pdaily`, dan bukan struktur GTFS.

```text
GTFS schedule tetap
        |
        v
trip instance deterministik
        |
        v
ridership harian per koridor
        |
        v
Monte Carlo replication loop
        |
        v
trip load factor snapshot per replikasi
        |
        v
segment load factor snapshot per replikasi
        |
        v
routing sensitivity per replikasi
        |
        v
agregasi statistik
```

## Penurunan Ridership Menjadi Crowdings

Ridership harian tetap menjadi sumber total demand, tetapi alokasi per trip sekarang memakai sampling Poisson.

### Ringkasan Alur

```text
jumlah_pelanggan_pemodelan
        |
GTFS menghasilkan N trip instance/hari/koridor
        |
setiap trip mendapat weight baseline dari time band
        |
weight trip di-sampling dengan Poisson
        |
penumpang harian dibagi proporsional terhadap weight sampel
        |
trip_load_factor_i = estimated_passengers_i / BUS_CAPACITY
```

### Trip-Level Monte Carlo

Untuk setiap trip instance, pipeline Monte Carlo membuat weight berbasis time band lalu mengganti seeded bounded random factor dengan Poisson draw.

Secara konsep, dengan rate scaling `K = MC_POISSON_SCALE_FACTOR`:

```text
Rtrip_raw ~ Poisson(lambda_trip * K)
Rtrip = Rtrip_raw / K
Wtrip = Mperiod * Rtrip
Ptrip = Pdaily * (Wtrip / sum(Wtrip))
LFtrip = Ptrip / BUS_CAPACITY
```

Keterangan:

- `Mperiod` tetap mewakili intensitas demand relatif per time band.
- `lambda_trip` dikalibrasi dari heuristic time band yang sudah ada.
- Total penumpang tetap dinormalisasi agar sama dengan `Pdaily`.

Jika semua draw Poisson menghasilkan bobot nol, sistem fallback ke weight time band agar pembagian tetap valid.

### Segment-Level Monte Carlo

Segment-level crowding juga disampling ulang per replikasi.

Pada implementasi saat ini:

```text
Rsegment_raw ~ Poisson(lambda_segment * K)
Rsegment = Rsegment_raw / K
```

Lalu bobot segment tetap dinormalisasi kembali ke load factor trip supaya rata-rata segment tetap mengikuti trip-level base.

Secara praktis:

```text
LFraw_segment = LFtrip * Rsegment
segment_load_factor = normalisasi(LFraw_segment)
```

Tujuannya menjaga struktur segment-level yang bisa dipakai Dijkstra, tanpa merusak skala trip-level.

## Replication Loop

Untuk satu skenario tetap:

- tanggal sama,
- koridor sama,
- GTFS schedule sama,
- `Pdaily` sama,
- `sim_time` sama,

backend menjalankan pipeline `N` kali.

Yang berubah hanya sampling random antar replikasi. Schedule, demand harian, dan struktur graph tidak berubah.

### Seed Policy

Seed policy dibuat agar hasil bisa diulang persis.

```text
master_seed -> replication_seed_0 ... replication_seed_(N-1)
```

Setiap replikasi memakai sub-seed yang deterministik dari master seed, sehingga reviewer atau grader bisa menjalankan eksperimen yang sama dan mendapatkan hasil yang sama.

## Hasil Replikasi yang Disimpan

Setiap replikasi menyimpan full snapshot berikut:

- seluruh `LFtrip` per trip instance,
- seluruh `LFsegment` per segment,
- total passengers yang dialokasikan,
- seed turunan replikasi,
- tanggal sampling per koridor bila ada fallback sample date.

Artinya backend tidak lagi hanya menyimpan run terakhir. Semua replikasi menjadi bagian dari output eksperimen.

## Agregasi Load Factor

Agregasi dihitung per trip dan per segment lintas seluruh replikasi.

Untuk setiap trip/segment:

- mean,
- standard deviation,
- percentile band 5th-95th,
- count,
- min dan max.

Secara ringkas:

```text
LF_mean = average(LF over replications)
LF_std  = standard deviation(LF over replications)
LF_p05  = percentile 5
LF_p95  = percentile 95
```

Jika pengguna hanya butuh deliverable utama, maka hasil agregasi segment adalah output yang paling penting, karena itu yang dipakai routing.

## Routing Sensitivity

Selain agregasi load factor, backend juga menjalankan routing sensitivity analysis pada set origin-destination yang tetap.

Alurnya per replikasi:

1. Ambil snapshot `LFsegment` dari replikasi itu.
2. Bangun graph Dijkstra dengan edge weight crowding snapshot tersebut.
3. Jalankan candidate search, attribute calculation, dan ranking.
4. Catat top-ranked route.
5. Ulangi untuk semua replikasi.

### Scenario yang Dipakai

Skenario routing dipakai pada pola berikut:

- same-corridor,
- 1-transfer,
- 2-transfer.

### Metrik Routing

Backend menghitung:

- recommendation stability rate,
- jumlah route unik yang muncul sebagai top route,
- rata-rata rank correlation antar replikasi,
- opsi Kendall's tau dan Spearman rho antar ranking kandidat.

Definisi stability rate:

```text
stability_rate =
  jumlah replikasi dengan top route paling sering muncul
  / total replikasi
```

Jika top route berubah-ubah antar replikasi, maka eksperimen menunjukkan routing sensitif terhadap crowding uncertainty.

## Alur Integrasi Dijkstra dan Bus Selector

Monte Carlo tidak mengubah logika routing inti. Yang berubah hanya input crowding-nya.

```text
snapshot LFsegment per replikasi
        |
        v
build_graph()
        |
        v
Dijkstra candidate generation
        |
        v
format_rute()
        |
        v
bus selector pada tiap segmen naik
        |
        v
ranking final per replikasi
```

Bus selector tetap berjalan seperti baseline. Perbedaannya adalah density snapshot yang dipakai berasal dari replikasi Monte Carlo tersebut.

## API Utama

| Endpoint | Fungsi |
| --- | --- |
| `/api/simulation/positions` | Posisi active generated trip instances pada `sim_time`. |
| `/api/rute/rekomendasi` | Rekomendasi rute deterministik dengan crowding current snapshot. |
| `/api/rute/monte-carlo` | Jalankan Monte Carlo load factor dan routing sensitivity. |
| `/api/rute/halte/{halte_id}/bus-berikutnya` | Kandidat bus berikutnya pada halte tertentu. |

### Request Monte Carlo

Endpoint utama saat ini:

```text
POST /api/rute/monte-carlo
```

Parameter yang tersedia:

| Parameter | Fungsi |
| --- | --- |
| `replications` | Jumlah replikasi Monte Carlo. Default `100`. |
| `master_seed` | Seed utama untuk reproducibility. |
| `tanggal` | Tanggal skenario. |
| `jam` | Jam analisis. |
| `hari_tipe` | `weekday` atau `weekend`. |
| `sim_time` | Detik dalam hari untuk snapshot aktif. |
| `routing_scenarios` | Daftar OD untuk routing sensitivity. |

## Debug Output

Output eksperimen Monte Carlo menyertakan:

```text
replication_index
master_seed
replication_seed
trip_loads
segment_loads
trip_aggregates
segment_aggregates
routing_sensitivity
```

Untuk routing sensitivity, output per scenario menyertakan:

```text
top_route_stability_rate
unique_top_route_count
top_route_mode_signature
top_route_mode_count
average_spearman_rho
average_kendall_tau
```

## Aturan Penting

Do:

- Gunakan GTFS hanya untuk membentuk trip instance dan timing.
- Gunakan `ridership_harian_turunan` sebagai sumber demand total.
- Simpan semua replikasi, bukan hanya output terakhir.
- Agregasikan mean, std dev, dan percentile band per trip dan per segment.
- Gunakan seed policy yang deterministik dan bisa diulang.
- Jalankan routing sensitivity pada snapshot crowding tiap replikasi.

Do not:

- Jangan ubah schedule atau `Pdaily` antar replikasi.
- Jangan buang hasil replikasi sebelumnya.
- Jangan treat Monte Carlo sebagai perubahan graph topology.
- Jangan anggap output eksperimen sebagai satu run deterministik tunggal.

## Fallback

| Kondisi | Fallback |
| --- | --- |
| Ridership tanggal request tidak ada | Latest date untuk koridor tersebut. |
| Ridership koridor tidak ada | `FALLBACK_LOAD_FACTOR = 0.5`. |
| Poisson draw menghasilkan bobot nol semua | Fallback ke weight time band. |
| Segment tidak punya segment IDs | Segment snapshot kosong untuk trip itu. |
| Routing scenario kosong | Hanya load factor experiment yang dijalankan. |
| Simulasi context tidak tersedia | Endpoint Monte Carlo mengembalikan error 503. |

## Ringkasan Output yang Diinginkan

Deliverable utama Monte Carlo bukan satu hasil final, tetapi distribusi:

- trip load factor mean/std/p05/p95,
- segment load factor mean/std/p05/p95,
- stability routing pada beberapa OD utama.

Dengan kata lain, baseline menjawab "berapa load factor saat ini", sedangkan Monte Carlo menjawab "seberapa stabil load factor dan rute jika demand diresampling berkali-kali".
