# TransJakarta Simulation Backend

Dokumen ini menjelaskan inti simulasi backend: bagaimana trip bus dibuat dari GTFS, bagaimana jumlah penumpang harian diturunkan menjadi kepadatan peak/off-hour, dan bagaimana hasilnya dipakai oleh peta, Dijkstra, dan rekomendasi bus.

Simulasi saat ini hanya untuk koridor `1`, `2`, `3`, `4`, dan `5`. Koridor `9` belum masuk scope.

## Sumber Data

Tabel struktural tetap menjadi sumber utama untuk graph, rute, dan peta:

| Tabel | Fungsi |
| --- | --- |
| `halte` | Koordinat dan metadata halte. |
| `koridor` | Metadata koridor. |
| `koridor_halte` | Membership dan urutan halte per koridor. |
| `segmen` | Edge graph untuk Dijkstra. |
| `shapes` | Polyline peta dan jalur visual pergerakan bus/rute. |

Tabel GTFS dan ridership dipakai sebagai input simulasi:

| Tabel | Kolom penting | Fungsi |
| --- | --- | --- |
| `gtfs_trips` | `trip_id`, `route_id`, `direction_id`, `trip_headsign`, `block_id`, `shape_id` | Pola trip GTFS, arah koridor, dan shape visual trip. |
| `gtfs_frequencies` | `trip_id`, `start_time`, `end_time`, `headway_secs` | Membuat jadwal keberangkatan deterministik. |
| `gtfs_stop_times` | `trip_id`, `stop_id`, `arrival_time`, `departure_time`, `stop_sequence` | Template pergerakan setiap trip. |
| `ridership_harian_turunan` | `tanggal`, `koridor_id`, `jumlah_pelanggan_pemodelan` | Demand penumpang harian untuk kepadatan. |

GTFS menghasilkan trip instance dan timing. Graph struktural tetap dipakai untuk Dijkstra, sedangkan `shapes` dipakai untuk menggambar pergerakan visual di peta.

## Konfigurasi Utama

Konfigurasi utama ada di `backend/services/gtfs_simulation.py`.

| Setting | Nilai |
| --- | --- |
| `SCOPED_KORIDOR` | `{"1", "2", "3", "4", "5"}` |
| `BUS_CAPACITY` | `80` penumpang |
| `FALLBACK_LOAD_FACTOR` | `0.5` |
| `SIMULATION_RUN_ID` | env value atau `"default"` |
| `OVERLAP_GROUP_THRESHOLD` | `0.80` |
| `MAX_VISIBLE_BUSES_PER_CORRIDOR_DIRECTION` | env value atau `0`, hanya display cap |

## Istilah Penting

`bus_id` yang dibuat backend adalah simulated trip instance, bukan bus fisik.

| Istilah | Arti |
| --- | --- |
| `trip_instance_id` | Satu keberangkatan hasil GTFS frequency. |
| `bus_id` | ID kompatibilitas frontend, nilainya sama dengan `trip_instance_id`. |
| `active_trip_instances` | Trip instance yang sedang aktif pada `sim_time`. |
| Physical bus | Belum dimodelkan. Jangan tafsirkan jumlah active instance sebagai jumlah armada fisik. |

## Alur GTFS Trip Generation

Trip generation terjadi saat startup melalui `load_simulation_context()` dan `_generate_instances()`.

```text
gtfs_trips
+ gtfs_frequencies
+ gtfs_stop_times
+ halte coordinates
+ segmen matching
+ shape_id + shapes
        |
        v
raw GTFS service patterns
        |
        v
exact deduplication
        |
        v
overlap merge
        |
        v
selected representative patterns
        |
        v
generated departure times
        |
        v
simulated trip instances
        |
        v
active_trip_instances at sim_time
```

## Visualisasi Pergerakan

Trip instance tetap memakai `gtfs_stop_times` untuk urutan stop, arrival/departure time, ETA, dan `next_stop`.

Untuk koordinat marker bus di `/api/simulation/positions`, backend tidak lagi menggambar garis lurus antar halte jika data shape tersedia. Saat startup, setiap stop pada trip dipetakan ke titik terdekat di `shapes` berdasarkan `shape_id` dari `gtfs_trips`. Untuk segment stop `A -> B`, backend mengambil potongan polyline shape di antara kedua titik tersebut, lalu menginterpolasi posisi bus berdasarkan progress waktu di sepanjang polyline.

Fallback tetap ada: jika `shape_id` tidak tersedia, shape terlalu pendek, atau segment visual tidak bisa dibangun, posisi bus kembali memakai interpolasi lurus antar halte.

Frontend overlay hasil pencarian rute juga memakai `shapes` untuk menggambar garis rute jika tersedia, dengan fallback ke garis lurus antar halte.

### Formula Trip Generation

Untuk setiap selected representative pattern:

```text
departure_time_n =
  start_time + n * headway_secs
```

dengan syarat:

```text
departure_time_n < end_time
```

Jumlah trip yang dihasilkan oleh satu pattern:

```text
generated_departures_count =
  ceil((end_time - start_time) / headway_secs)
```

Contoh:

```text
start_time = 05:00
end_time = 21:00
headway_secs = 360

generated_trip_instances =
  ceil(57,600 / 360)
  = 160 trip/hari
```

Setiap trip memakai template stop time GTFS yang sama, lalu digeser:

```text
offset =
  generated_departure_time - template_first_departure_time

simulated_arrival_time =
  template_arrival_time + offset

simulated_departure_time =
  template_departure_time + offset
```

Lifecycle:

| Kondisi | Status |
| --- | --- |
| `sim_time < first_stop_departure_time` | Belum aktif. |
| `first_stop_departure_time <= sim_time < last_stop_arrival_time` | Aktif dan tampil di peta. |
| `sim_time >= last_stop_arrival_time` | Selesai. |

Estimasi jumlah instance aktif stabil dari satu pattern:

```text
estimated_active_instances =
  trip_duration_seconds / headway_secs
```

Ini tetap active trip instances, bukan physical bus count.

## Deduplication dan Overlap Merge

Backend tidak langsung membuat trip dari semua row GTFS mentah.

Exact deduplication memakai key:

```text
pattern_key =
  route_id
  direction_id
  ordered stop sequence
  start_time
  end_time
  headway_secs
```

Lalu pattern dalam `route_id + direction_id` yang sangat mirip secara urutan halte digabung:

```text
ordered_stop_overlap >= 0.80
```

Hanya satu representative pattern yang dipakai untuk membuat departure instance. Prioritas representative:

1. Segment match ratio tertinggi ke tabel `segmen`.
2. Stop count tertinggi.
3. `headway_secs` terkecil.
4. Durasi trip terpanjang.
5. Tie-break deterministik dari `trip_id`.

Tujuannya menghindari double-counting pola GTFS yang sebenarnya menggambarkan pergerakan koridor-arah yang sama.

## Penurunan Ridership Menjadi Kepadatan

Crowding dibuat di `generate_crowding()`.

Sumber utama:

```text
ridership_harian_turunan.jumlah_pelanggan_pemodelan
```

Satu nilai ridership berlaku untuk:

```text
tanggal + koridor_id
```

Tabel dummy lama tidak menjadi sumber utama:

```text
kepadatan_bus
kepadatan_historis
segmen_dengan_kepadatan
```

### Ringkasan Alur

```text
jumlah_pelanggan_pemodelan = total penumpang/hari/koridor
        |
GTFS menghasilkan N trip instance/hari/koridor
        |
setiap trip dilihat departure_time-nya
        |
departure_time menentukan time band
        |
setiap time band punya demand weight
        |
setiap trip diberi seeded random factor
        |
raw_weight_i = time_band_weight_i * seeded_random_factor_i
        |
penumpang harian dibagi berdasarkan proporsi raw_weight
        |
estimated_passengers_i
        |
trip_load_factor_i = estimated_passengers_i / BUS_CAPACITY
        |
kategori_kepadatan
```

### Baseline Harian

Daily mean load factor dihitung sebagai referensi dan fallback:

```text
daily_mean_load_factor =
  jumlah_pelanggan_pemodelan
  / (generated_trip_instances_per_corridor * BUS_CAPACITY)
```

Nilai ini bukan cara utama membagi penumpang. Penumpang tidak dibagi rata ke semua trip.

Contoh baseline:

```text
jumlah_pelanggan_pemodelan = 8,000
generated_trip_instances = 160
BUS_CAPACITY = 80

average_passengers_per_trip =
  8,000 / 160
  = 50

daily_mean_load_factor =
  50 / 80
  = 0.625
```

Angka `0.625` hanya rata-rata harian. Trip peak bisa lebih tinggi, trip malam bisa lebih rendah.

### Time Band Demand Weights

Time band merepresentasikan pola demand penumpang transportasi publik, bukan kemacetan jalan.

| Time band | Jam | Weight |
| --- | --- | --- |
| `early` | `05:00-06:00` | `0.70` |
| `morning_peak` | `06:00-09:00` | `1.40` |
| `midday_offpeak` | `09:00-16:00` | `0.85` |
| `evening_peak` | `16:00-20:00` | `1.35` |
| `night` | `20:00-22:00` | `0.55` |
| `late_night` | `22:00-05:00` | `0.20` |

Weight ini bukan persentase total penumpang. Weight adalah intensitas relatif per trip di time band tersebut.

### Seeded Random Variation

Setiap trip diberi variasi deterministik agar trip dalam band yang sama tidak identik.

Seed:

```text
seed_trip =
  hash(tanggal + koridor_id + trip_instance_id + simulation_run_id)
```

Random factor:

```text
seeded_random_factor_i ~ Normal(mean=1.0, std=0.15)
clamp to 0.60-1.40
```

Seeded randomness hanya memberi variasi kepadatan. Randomness tidak membuat trip baru, tidak menghapus trip, dan tidak mengubah `active_trip_instances`.

### Formula Alokasi Penumpang Per Trip

Untuk setiap trip instance:

```text
raw_weight_i =
  time_band_weight_i * seeded_random_factor_i
```

Total bobot dalam satu koridor-hari:

```text
total_weight =
  sum(raw_weight_i for all generated trip instances in corridor/date)
```

Alokasi penumpang:

```text
estimated_passengers_i =
  jumlah_pelanggan_pemodelan * raw_weight_i / total_weight
```

Load factor:

```text
trip_load_factor_i =
  estimated_passengers_i / BUS_CAPACITY
```

Dengan:

```text
BUS_CAPACITY = 80
```

Normalisasi wajib:

```text
sum(estimated_passengers_i for all trips in corridor/date)
= jumlah_pelanggan_pemodelan
```

### Contoh Sederhana

Misal:

```text
jumlah_pelanggan_pemodelan = 8,000 orang/hari
generated_trip_instances = 160 trip/hari
BUS_CAPACITY = 80
```

Tanpa time-band weighting, rata-rata kasar:

```text
8,000 / 160 = 50 penumpang/trip
50 / 80 = 0.625 load factor
```

Namun simulasi memakai weight.

Contoh tanpa random variation:

```text
70 peak trips   * 1.40 = 98.0
60 midday trips * 0.85 = 51.0
30 night trips  * 0.55 = 16.5

total_weight = 165.5
```

Peak trip:

```text
estimated_passengers_peak =
  8,000 * 1.40 / 165.5
  ~= 67.67

trip_load_factor_peak =
  67.67 / 80
  ~= 0.846

kategori_kepadatan = padat
```

Night trip:

```text
estimated_passengers_night =
  8,000 * 0.55 / 165.5
  ~= 26.59

trip_load_factor_night =
  26.59 / 80
  ~= 0.332

kategori_kepadatan = sepi
```

Total semua alokasi tetap:

```text
sum(estimated_passengers_i) = 8,000
```

### Kategori Kepadatan

| Load factor | `kategori_kepadatan` | `label_kepadatan` |
| --- | --- | --- |
| `< 0.50` | `sepi` | `Sepi` |
| `0.50 <= x < 0.80` | `sedang` | `Sedang` |
| `0.80 <= x < 1.00` | `padat` | `Padat` |
| `>= 1.00` | `sangat_padat` | `Padat` |

`sangat_padat` tetap dikirim sebagai `kategori_kepadatan`, tetapi `label_kepadatan` dipetakan ke `Padat` untuk kompatibilitas frontend lama.

## Segment-Level Crowding

Dijkstra bekerja di level `segmen`, jadi trip-level load factor diturunkan lagi menjadi segment-level crowding.

Untuk setiap segment dalam satu trip:

```text
raw_segment_load =
  trip_load_factor * segment_random_factor
```

Segment random factor:

```text
segment_random_factor ~ Normal(mean=1.0, std=0.10)
clamp to 0.80-1.20
```

Normalisasi segment:

```text
average_raw_segment_load =
  average(raw_segment_load across matched segments)

normalization_scale =
  trip_load_factor / average_raw_segment_load

segment_load_factor =
  raw_segment_load * normalization_scale
```

Sehingga:

```text
average(segment_load_factor for one trip)
~= trip_load_factor
```

## Integrasi Dijkstra dan Bus Selector

Dijkstra tetap memakai graph dari tabel `segmen`.

Pada `sim_time`, backend mengambil crowding segment dari trip instance yang sedang aktif:

```text
active_loads_for_segment =
  segment_load_factor dari active_trip_instances
  yang sedang berada pada segmen tersebut

edge_crowding =
  average(active_loads_for_segment)
```

Fallback jika tidak ada active load:

1. Active generated segment load average.
2. Corridor daily mean load factor.
3. `FALLBACK_LOAD_FACTOR = 0.5`.

Bus selector memilih bus rekomendasi dengan:

1. Load factor terendah.
2. ETA tercepat sebagai tie-break.

| Komponen | Sumber kepadatan |
| --- | --- |
| Dijkstra edge weight | Rata-rata generated segment load dari active trip instances. |
| Bus recommendation | Trip-level load factor dari specific trip instance. |
| `/api/simulation/positions` | Active trip instances + posisi mengikuti shape/fallback interpolasi + load factor. |

## API Utama

| Endpoint | Fungsi |
| --- | --- |
| `/api/simulation/positions` | Mengembalikan active generated trip instances pada `sim_time`. |
| `/api/rute/rekomendasi` | Rekomendasi rute dengan Dijkstra dan generated crowding. |
| `/api/rute/halte/{halte_id}/bus-berikutnya` | Kandidat trip berikutnya yang melewati halte. |

Parameter penting:

```text
sim_time
tanggal
simulation_run_id
max_visible_per_corridor_direction
```

`max_visible_per_corridor_direction` hanya pembatas visual untuk `/positions`, bukan perbaikan generation logic.

## Debug Output

Startup GTFS logs mencakup:

```text
raw_frequency_rows
unique_service_patterns_before_overlap_merge
overlap_groups_count
selected_representative_patterns_count
discarded_overlapping_patterns_count
generated_trip_instances_after_overlap_merge
active_trip_instances_at_sample_times
```

Crowding logs mencakup:

```text
tanggal
koridor_id
jumlah_pelanggan_pemodelan
total_generated_trip_instances
total_allocated_passengers
allocation_error
time_band_summary
```

Setiap `time_band_summary` berisi:

```text
time_band
time_range
time_band_weight
trip_count
raw_weight_sum
allocated_passengers
average_trip_load_factor
min_trip_load_factor
max_trip_load_factor
```

Sanity check utama:

```text
total_allocated_passengers ~= jumlah_pelanggan_pemodelan
```

## Aturan Penting

Do:

- Gunakan GTFS hanya untuk membuat trip instance dan timing.
- Gunakan `ridership_harian_turunan` sebagai sumber demand harian.
- Distribusikan demand dengan time-band weight + seeded variation + normalisasi.
- Pertahankan `active_trip_instances` sebagai istilah utama.
- Pertahankan graph, Dijkstra, dan map geometry dari tabel struktural.
- Pertahankan segment-level crowding karena Dijkstra butuh edge-level value.
- Gunakan `shape_id` dan `shapes` untuk posisi visual bus/rute ketika tersedia.

Do not:

- Jangan pakai `kepadatan_bus`, `kepadatan_historis`, atau `segmen_dengan_kepadatan` sebagai sumber utama crowding.
- Jangan bagi penumpang rata ke semua trip sebagai logic utama.
- Jangan pakai seeded randomness saja tanpa baseline time band.
- Jangan ubah jumlah active trip instances dengan randomness.
- Jangan tafsirkan time-band crowding sebagai kemacetan jalan.
- Jangan tafsirkan generated trip instances sebagai armada fisik.

## Fallback

| Kondisi | Fallback |
| --- | --- |
| Ridership tanggal request tidak ada | Latest date untuk koridor tersebut. |
| Ridership koridor tidak ada | `FALLBACK_LOAD_FACTOR = 0.5`. |
| GTFS frequency koridor tidak ada | Tidak ada generated trip; crowding fallback `0.5`. |
| Koordinat halte template tidak ada | Trip template dilewati dan log warning. |
| Segment pair tidak cocok | Log warning; fallback ke segmen koridor jika diperlukan. |
| Shape visual tidak tersedia/cocok | Posisi/rute fallback ke garis lurus antar halte. |
