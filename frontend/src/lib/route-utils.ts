// Utilitas tipe + konstruksi GeoJSON untuk overlay rute Dijkstra di MapLibre.
// Konsumen: SimulationMap, RoutePanel, RouteCard.

export type Halte = {
  halte_id: string;
  nama: string;
  lat: number;
  lng: number;
};

export type SegmenDetail = {
  dari_id: string;
  ke_id: string;
  kepadatan: number;
  waktu_menit: number;
  jarak_meter?: number;
};

export type BusRekomendasi = {
  bus_id: string;
  kepadatan: number;
  label_kepadatan: 'Sepi' | 'Sedang' | 'Padat';
  eta_menit: number;
  kategori_kepadatan?: 'sepi' | 'sedang' | 'padat' | 'sangat_padat';
  selection_score?: number;
  selection_reason?: 'next_bus_safe_density' | 'bus_score';
  safe_density_threshold?: number;
  eta_norm?: number;
  earliest_eta_menit?: number;
  extra_wait_menit?: number;
  extra_wait_norm?: number;
  estimated_passengers?: number;
  capacity?: number;
  candidate_count?: number;
  considered_candidate_count?: number;
  candidate_debug?: Array<{
    bus_id: string;
    eta_menit: number;
    kepadatan: number;
    score: number;
  }>;
};

export type NaikItem = {
  tipe: 'naik';
  dari: string;
  ke: string;
  naik_di_id: string;
  turun_di_id: string;
  koridor_id: number;
  nama_koridor: string | null;
  naik_di: string;
  turun_di: string;
  kepadatan: number;
  waktu_menit: number;
  jarak_meter?: number;
  jumlah_segmen: number;
  segmen_detail: SegmenDetail[];
  // Diisi oleh Algoritma 2 (backend services/bus_selector.py). Null bila tidak
  // ada bus kandidat (sudah lewat semua / kepadatan tidak tersedia).
  bus_rekomendasi?: BusRekomendasi | null;
};

export type TransitItem = {
  tipe: 'transit';
  transit_di: string;
  transit_di_id: string;
  dari_koridor: number | null;
  ke_koridor: number;
};

export type RuteSegmen = NaikItem | TransitItem;

export type Rute = {
  skor: number;
  primary_score?: number;
  total_jarak_meter?: number;
  jumlah_transit: number;
  estimasi_menit: number;
  rata_kepadatan: number;
  density_source?: 'selected_bus' | 'mixed_edge_fallback';
  density_norm?: number;
  ranking_method?: 'primary_score_then_density_rerank';
  ranking_phase_1?: {
    estimasi_menit: number;
    total_jarak_meter: number;
    jumlah_transit: number;
    time_norm: number;
    distance_norm: number;
    transfer_norm: number;
    primary_score: number;
    weights: {
      time: number;
      distance: number;
      transfer: number;
    };
  };
  ranking_phase_2?: {
    rata_kepadatan: number;
    density_norm: number;
  };
  segmen: RuteSegmen[];
};

export type SelectionMode = 'idle' | 'pilih_asal' | 'pilih_tujuan' | 'hasil';

export function normalisasiKepadatanDisplay(kepadatan: number): number {
  if (!Number.isFinite(kepadatan)) return 0;
  return Math.max(0, Math.min(kepadatan, 1));
}

// Pemetaan kepadatan (0..1) ke warna semafor. Threshold 0.4/0.7 dipilih
// supaya distribusi merah/kuning/hijau seimbang untuk kepadatan jam sibuk
// TransJakarta tipikal (mean ~0.5, std ~0.2).
export function kepadatanKeWarna(kepadatan: number): string {
  const value = normalisasiKepadatanDisplay(kepadatan);
  if (value < 0.4) return '#2ECC71'; // hijau — sepi
  if (value < 0.7) return '#F39C12'; // kuning — sedang
  return '#E74C3C'; // merah — padat
}

export function labelKepadatan(kepadatan: number): 'sepi' | 'sedang' | 'padat' {
  const value = normalisasiKepadatanDisplay(kepadatan);
  if (value < 0.4) return 'sepi';
  if (value < 0.7) return 'sedang';
  return 'padat';
}

// Bangun FeatureCollection LineString. Tiap fine-grained segmen = 1 Feature
// dengan property `warna` yang dipakai oleh paint expression `['get', 'warna']`
// pada layer line MapLibre. Ini memberikan warna per-segmen tanpa membuat
// banyak layer terpisah.
export function buildRouteGeoJSON(
  segmen: RuteSegmen[],
  halteMap: Map<string, Halte>,
): GeoJSON.FeatureCollection<GeoJSON.LineString> {
  const features: GeoJSON.Feature<GeoJSON.LineString>[] = [];
  for (const s of segmen) {
    if (s.tipe !== 'naik') continue;
    for (const d of s.segmen_detail) {
      const a = halteMap.get(d.dari_id);
      const b = halteMap.get(d.ke_id);
      if (!a || !b) continue;
      features.push({
        type: 'Feature',
        geometry: {
          type: 'LineString',
          coordinates: [
            [a.lng, a.lat],
            [b.lng, b.lat],
          ],
        },
        properties: {
          warna: kepadatanKeWarna(d.kepadatan),
          kepadatan: normalisasiKepadatanDisplay(d.kepadatan),
          koridor_id: s.koridor_id,
        },
      });
    }
  }
  return { type: 'FeatureCollection', features };
}

// Kumpulkan titik transit dari rute aktif untuk dirender sebagai marker
// khusus (bentuk berbeda dari halte biasa).
export function collectTransitPoints(
  segmen: RuteSegmen[],
  halteMap: Map<string, Halte>,
): GeoJSON.FeatureCollection<GeoJSON.Point> {
  const features: GeoJSON.Feature<GeoJSON.Point>[] = [];
  for (const s of segmen) {
    if (s.tipe !== 'transit') continue;
    const h = halteMap.get(s.transit_di_id);
    if (!h) continue;
    features.push({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [h.lng, h.lat] },
      properties: {
        nama: h.nama,
        dari_koridor: s.dari_koridor,
        ke_koridor: s.ke_koridor,
      },
    });
  }
  return { type: 'FeatureCollection', features };
}

// FeatureCollection berisi 1 titik untuk source single-point seperti
// titik asal / titik tujuan.
export function titikTunggalGeoJSON(
  halte: Halte | undefined,
): GeoJSON.FeatureCollection<GeoJSON.Point> {
  if (!halte) return { type: 'FeatureCollection', features: [] };
  return {
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [halte.lng, halte.lat] },
        properties: { nama: halte.nama, halte_id: halte.halte_id },
      },
    ],
  };
}
