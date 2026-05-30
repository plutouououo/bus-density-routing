'use client';

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import maplibregl, { type Map as MapLibreMap, type GeoJSONSource } from 'maplibre-gl';

import {
  type Halte,
  type Rute,
  type SelectionMode,
  buildRouteGeoJSON,
  collectTransitPoints,
  titikTunggalGeoJSON,
} from '@/lib/route-utils';

const KORIDOR_COLOR: Record<string, string> = {
  '1': '#E74C3C',
  '2': '#3498DB',
  '3': '#2ECC71',
  '4': '#F39C12',
  '5': '#9B59B6',
};

const MAKS_RUTE = 3; // sinkron dengan k=3 di backend dijkstra()

type ShapesResponse = GeoJSON.FeatureCollection<GeoJSON.LineString, {
  koridor_id: number | string;
  shape_id: string;
}>;
type HalteRow = Halte;
type PositionsResponse = GeoJSON.FeatureCollection<GeoJSON.Point, {
  bus_id: string;
  koridor_id: number;
  bearing: number;
  next_stop: string;
  eta_minutes: number;
}>;

const EMPTY_FC: PositionsResponse = { type: 'FeatureCollection', features: [] };
const EMPTY_LINE_FC: GeoJSON.FeatureCollection<GeoJSON.LineString> = {
  type: 'FeatureCollection',
  features: [],
};
const EMPTY_POINT_FC: GeoJSON.FeatureCollection<GeoJSON.Point> = {
  type: 'FeatureCollection',
  features: [],
};

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

const log = (...args: unknown[]) => console.log('[SimulationMap]', ...args);
const warn = (...args: unknown[]) => console.warn('[SimulationMap]', ...args);
const err = (...args: unknown[]) => console.error('[SimulationMap]', ...args);

async function fetchJson<T>(path: string): Promise<T> {
  log('fetch →', path);
  const t0 = performance.now();
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} @ ${path}`);
  const data = (await res.json()) as T;
  log('fetch ✓', path, `${Math.round(performance.now() - t0)}ms`);
  return data;
}

type Debug = {
  mounted: boolean;
  containerSize: string;
  mapCreated: boolean;
  styleLoaded: boolean;
  shapesCount: number | null;
  halteCount: number | null;
  positionsCount: number | null;
};

type Props = {
  simTime: number;
  // Fase 5 ↓
  selectionMode: SelectionMode;
  halteAsal: string | null;
  halteTujuan: string | null;
  hasilRute: Rute[] | undefined;
  ruteAktifIdx: number;
  halteMap: Map<string, Halte>;
  onHalteClick: (halteId: string) => void;
};

export function SimulationMap({
  simTime,
  selectionMode,
  halteAsal,
  halteTujuan,
  hasilRute,
  ruteAktifIdx,
  halteMap,
  onHalteClick,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const styleLoadedRef = useRef(false);

  // Ref untuk handler klik halte: handler MapLibre dipasang sekali saat init,
  // tapi kita perlu nilai terbaru dari `selectionMode` & `onHalteClick`.
  const selectionModeRef = useRef(selectionMode);
  const onHalteClickRef = useRef(onHalteClick);
  useEffect(() => {
    selectionModeRef.current = selectionMode;
  }, [selectionMode]);
  useEffect(() => {
    onHalteClickRef.current = onHalteClick;
  }, [onHalteClick]);

  const [debug, setDebug] = useState<Debug>({
    mounted: false,
    containerSize: '?',
    mapCreated: false,
    styleLoaded: false,
    shapesCount: null,
    halteCount: null,
    positionsCount: null,
  });
  const bumpDebug = (patch: Partial<Debug>) => setDebug((d) => ({ ...d, ...patch }));

  const { data: shapes, error: shapesError } = useQuery<ShapesResponse>({
    queryKey: ['shapes'],
    queryFn: () => fetchJson<ShapesResponse>('/api/simulation/shapes'),
    staleTime: Infinity,
    retry: 1,
  });

  const { data: halte, error: halteError } = useQuery<HalteRow[]>({
    queryKey: ['halte'],
    queryFn: () => fetchJson<HalteRow[]>('/api/simulation/halte'),
    staleTime: Infinity,
    retry: 1,
  });

  const { data: positions, error: positionsError } = useQuery<PositionsResponse>({
    queryKey: ['positions', simTime],
    queryFn: () =>
      fetchJson<PositionsResponse>(`/api/simulation/positions?sim_time=${simTime}`),
    staleTime: 0,
    refetchInterval: false,
    placeholderData: (prev) => prev,
    retry: 0,
  });

  // ---- Init map sekali ----
  useEffect(() => {
    log('init effect: containerRef.current =', containerRef.current);
    const container = containerRef.current;
    if (!container) {
      warn('init aborted: container ref is null');
      return;
    }
    if (mapRef.current) {
      warn('init aborted: map already exists');
      return;
    }

    const rect = container.getBoundingClientRect();
    const sizeStr = `${Math.round(rect.width)}×${Math.round(rect.height)}`;
    log('container size at init:', sizeStr);
    bumpDebug({ mounted: true, containerSize: sizeStr });

    if (rect.width === 0 || rect.height === 0) {
      warn('container has 0 dimension — MapLibre canvas will start blank until ResizeObserver fires');
    }

    let map: MapLibreMap;
    try {
      map = new maplibregl.Map({
        container,
        style: {
          version: 8,
          sources: {
            basemap: {
              type: 'raster',
              tiles: [
                'https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
                'https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
                'https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
                'https://d.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png',
              ],
              tileSize: 256,
              attribution: '© OpenStreetMap contributors © CARTO',
            },
          },
          layers: [{ id: 'basemap', type: 'raster', source: 'basemap' }],
        },
        center: [106.827, -6.175],
        zoom: 12,
      });
      log('MapLibre instance created');
      bumpDebug({ mapCreated: true });
    } catch (e) {
      err('MapLibre init failed:', e);
      return;
    }

    map.on('error', (e) => err('MapLibre runtime error:', e.error ?? e));
    map.on('idle', () => log('map idle (tiles + sources finished loading)'));

    const ro = new ResizeObserver((entries) => {
      const r = entries[0]?.contentRect;
      if (r) log('container resized →', `${Math.round(r.width)}×${Math.round(r.height)}`);
      map.resize();
      if (r) bumpDebug({ containerSize: `${Math.round(r.width)}×${Math.round(r.height)}` });
    });
    ro.observe(container);

    map.on('load', () => {
      log('map load event fired (style ready)');
      styleLoadedRef.current = true;
      bumpDebug({ styleLoaded: true });

      // ---- Layer bus (top of stack) ----
      map.addSource('buses', { type: 'geojson', data: EMPTY_FC });
      map.addLayer({
        id: 'buses-layer',
        type: 'circle',
        source: 'buses',
        paint: {
          'circle-radius': 6,
          'circle-color': [
            'match',
            ['get', 'koridor_id'],
            1, KORIDOR_COLOR['1'],
            2, KORIDOR_COLOR['2'],
            3, KORIDOR_COLOR['3'],
            4, KORIDOR_COLOR['4'],
            5, KORIDOR_COLOR['5'],
            '#666',
          ],
          'circle-stroke-color': '#fff',
          'circle-stroke-width': 2,
        },
      });
      log('added buses-layer (empty)');

      // ---- Source kosong untuk overlay rute (di-set datanya kemudian) ----
      // Disisipkan SEBELUM buses-layer agar bus tetap di atas garis rute.
      for (let i = 0; i < MAKS_RUTE; i++) {
        map.addSource(`rute-${i}`, { type: 'geojson', data: EMPTY_LINE_FC });
        map.addLayer(
          {
            id: `rute-line-${i}`,
            type: 'line',
            source: `rute-${i}`,
            layout: { 'line-join': 'round', 'line-cap': 'round' },
            paint: {
              'line-color': ['get', 'warna'],
              'line-width': 3,
              'line-opacity': 0.35,
            },
          },
          'buses-layer',
        );
      }

      // ---- Marker khusus rute aktif ----
      map.addSource('titik-asal', { type: 'geojson', data: EMPTY_POINT_FC });
      map.addLayer(
        {
          id: 'titik-asal-layer',
          type: 'circle',
          source: 'titik-asal',
          paint: {
            'circle-radius': 10,
            'circle-color': '#2563eb', // biru — asal
            'circle-stroke-color': '#ffffff',
            'circle-stroke-width': 3,
          },
        },
        'buses-layer',
      );

      map.addSource('titik-tujuan', { type: 'geojson', data: EMPTY_POINT_FC });
      map.addLayer(
        {
          id: 'titik-tujuan-layer',
          type: 'circle',
          source: 'titik-tujuan',
          paint: {
            'circle-radius': 10,
            'circle-color': '#dc2626', // merah — tujuan
            'circle-stroke-color': '#ffffff',
            'circle-stroke-width': 3,
          },
        },
        'buses-layer',
      );

      map.addSource('titik-transit', { type: 'geojson', data: EMPTY_POINT_FC });
      map.addLayer(
        {
          id: 'titik-transit-layer',
          type: 'circle',
          source: 'titik-transit',
          paint: {
            'circle-radius': 8,
            'circle-color': '#a855f7', // ungu — transit
            'circle-stroke-color': '#ffffff',
            'circle-stroke-width': 2,
          },
        },
        'buses-layer',
      );

      // ---- Klik bus (popup info) ----
      map.on('click', 'buses-layer', (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties as PositionsResponse['features'][number]['properties'];
        const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates;
        new maplibregl.Popup()
          .setLngLat([lng, lat])
          .setHTML(
            `<div style="font-family: ui-sans-serif, system-ui; font-size: 12px; line-height: 1.4">
              <div><b>Bus ${p.bus_id}</b></div>
              <div>Koridor ${p.koridor_id}</div>
              <div>Next: ${p.next_stop}</div>
              <div>ETA: ${p.eta_minutes} min</div>
            </div>`,
          )
          .addTo(map);
      });
      map.on('mouseenter', 'buses-layer', () => (map.getCanvas().style.cursor = 'pointer'));
      map.on('mouseleave', 'buses-layer', () => {
        // Pulihkan cursor sesuai mode seleksi terkini
        const mode = selectionModeRef.current;
        map.getCanvas().style.cursor =
          mode === 'pilih_asal' || mode === 'pilih_tujuan' ? 'crosshair' : '';
      });
    });

    mapRef.current = map;
    return () => {
      log('cleanup: removing map');
      ro.disconnect();
      map.remove();
      mapRef.current = null;
      styleLoadedRef.current = false;
    };
  }, []);

  // ---- Add shape lines saat data datang ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !shapes) return;
    bumpDebug({ shapesCount: shapes.features.length });
    log('shapes received:', shapes.features.length, 'shape lines');
    const apply = () => {
      const source = map.getSource('shapes') as GeoJSONSource | undefined;
      if (source) {
        source.setData(shapes);
        return;
      }
      map.addSource('shapes', { type: 'geojson', data: shapes });
      // Sisipkan polyline koridor PALING BAWAH (sebelum rute-line-0)
      // supaya overlay rute Dijkstra menonjol di atasnya.
      map.addLayer(
        {
          id: 'shapes-layer',
          type: 'line',
          source: 'shapes',
          paint: {
            'line-color': [
              'match',
              ['to-string', ['get', 'koridor_id']],
              '1', KORIDOR_COLOR['1'],
              '2', KORIDOR_COLOR['2'],
              '3', KORIDOR_COLOR['3'],
              '4', KORIDOR_COLOR['4'],
              '5', KORIDOR_COLOR['5'],
              '#666',
            ],
            'line-width': 4,
            'line-opacity': 0.55,
          },
        },
        'rute-line-0',
      );
      log('added shapes-layer');
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [shapes]);

  // ---- Add halte points saat data datang ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !halte) return;
    bumpDebug({ halteCount: halte.length });
    log('halte received:', halte.length);
    const apply = () => {
      if (map.getSource('halte')) return;
      map.addSource('halte', {
        type: 'geojson',
        data: {
          type: 'FeatureCollection',
          features: halte.map((h) => ({
            type: 'Feature',
            geometry: { type: 'Point', coordinates: [h.lng, h.lat] },
            properties: { halte_id: h.halte_id, nama: h.nama },
          })),
        },
      });
      map.addLayer(
        {
          id: 'halte-layer',
          type: 'circle',
          source: 'halte',
          paint: {
            'circle-radius': 3,
            'circle-color': '#1a1a1a',
            'circle-stroke-color': '#e5e5e5',
            'circle-stroke-width': 1,
          },
        },
        'buses-layer',
      );
      log('added halte-layer');

      // Handler klik halte: mode-aware via ref.
      // - Mode pilih_asal / pilih_tujuan → callback ke parent (set state)
      // - Mode lain → popup info halte biasa
      map.on('click', 'halte-layer', (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties as { halte_id: string; nama: string };
        const mode = selectionModeRef.current;
        if (mode === 'pilih_asal' || mode === 'pilih_tujuan') {
          onHalteClickRef.current(p.halte_id);
          return;
        }
        const [lng, lat] = (f.geometry as GeoJSON.Point).coordinates;
        new maplibregl.Popup()
          .setLngLat([lng, lat])
          .setHTML(
            `<div style="font-family: ui-sans-serif, system-ui; font-size: 12px">
              <b>${p.nama}</b><br/><span style="color:#666">${p.halte_id}</span>
            </div>`,
          )
          .addTo(map);
      });

      map.on('mouseenter', 'halte-layer', () => {
        const mode = selectionModeRef.current;
        map.getCanvas().style.cursor =
          mode === 'pilih_asal' || mode === 'pilih_tujuan' ? 'crosshair' : 'pointer';
      });
      map.on('mouseleave', 'halte-layer', () => {
        const mode = selectionModeRef.current;
        map.getCanvas().style.cursor =
          mode === 'pilih_asal' || mode === 'pilih_tujuan' ? 'crosshair' : '';
      });
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [halte]);

  // ---- Update bus positions ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !positions) return;
    bumpDebug({ positionsCount: positions.features.length });
    const apply = () => {
      const src = map.getSource('buses') as GeoJSONSource | undefined;
      src?.setData(positions);
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [positions]);

  // ---- Sinkronkan styling halte + cursor dengan selectionMode ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      const selecting =
        selectionMode === 'pilih_asal' || selectionMode === 'pilih_tujuan';
      map.getCanvas().style.cursor = selecting ? 'crosshair' : '';
      if (map.getLayer('halte-layer')) {
        // Perbesar halte + highlight saat mode seleksi aktif agar mudah diklik.
        map.setPaintProperty('halte-layer', 'circle-radius', selecting ? 7 : 3);
        map.setPaintProperty(
          'halte-layer',
          'circle-stroke-color',
          selecting ? '#fbbf24' : '#e5e5e5',
        );
        map.setPaintProperty('halte-layer', 'circle-stroke-width', selecting ? 2 : 1);
      }
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [selectionMode]);

  // ---- Update overlay rute (lines + transit markers) ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const apply = () => {
      // Set ulang data source semua rute. Source disimpan permanen (di-init
      // saat 'load'), jadi cukup setData + adjust opacity/width per layer.
      for (let i = 0; i < MAKS_RUTE; i++) {
        const src = map.getSource(`rute-${i}`) as GeoJSONSource | undefined;
        const rute = hasilRute?.[i];
        const data = rute ? buildRouteGeoJSON(rute.segmen, halteMap) : EMPTY_LINE_FC;
        src?.setData(data);

        if (map.getLayer(`rute-line-${i}`)) {
          const aktif = i === ruteAktifIdx && !!rute;
          map.setPaintProperty(`rute-line-${i}`, 'line-width', aktif ? 6 : 3);
          map.setPaintProperty(`rute-line-${i}`, 'line-opacity', aktif ? 1 : 0.35);
        }
      }

      // Marker transit hanya untuk rute aktif (mengurangi clutter peta).
      const transitSrc = map.getSource('titik-transit') as GeoJSONSource | undefined;
      const ruteAktif = hasilRute?.[ruteAktifIdx];
      transitSrc?.setData(
        ruteAktif ? collectTransitPoints(ruteAktif.segmen, halteMap) : EMPTY_POINT_FC,
      );
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [hasilRute, ruteAktifIdx, halteMap]);

  // ---- Update marker asal & tujuan ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      const srcA = map.getSource('titik-asal') as GeoJSONSource | undefined;
      const srcT = map.getSource('titik-tujuan') as GeoJSONSource | undefined;
      srcA?.setData(titikTunggalGeoJSON(halteAsal ? halteMap.get(halteAsal) : undefined));
      srcT?.setData(titikTunggalGeoJSON(halteTujuan ? halteMap.get(halteTujuan) : undefined));
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [halteAsal, halteTujuan, halteMap]);

  // ---- Auto-fit bounds ke rute aktif ----
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !hasilRute || hasilRute.length === 0) return;
    const ruteAktif = hasilRute[ruteAktifIdx];
    if (!ruteAktif) return;
    const fc = buildRouteGeoJSON(ruteAktif.segmen, halteMap);
    if (fc.features.length === 0) return;

    // Hitung bounding box dari semua koordinat segmen
    let minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
    for (const f of fc.features) {
      for (const [lng, lat] of f.geometry.coordinates) {
        if (lng < minLng) minLng = lng;
        if (lat < minLat) minLat = lat;
        if (lng > maxLng) maxLng = lng;
        if (lat > maxLat) maxLat = lat;
      }
    }
    if (!Number.isFinite(minLng)) return;
    map.fitBounds(
      [[minLng, minLat], [maxLng, maxLat]],
      { padding: { top: 100, bottom: 100, left: 100, right: 420 }, duration: 800 },
    );
  }, [hasilRute, ruteAktifIdx, halteMap]);

  const error = shapesError || halteError || positionsError;

  return (
    <>
      <div
        ref={containerRef}
        style={{ position: 'absolute', top: 0, right: 0, bottom: 0, left: 0 }}
        className="bg-neutral-900"
      />
      <div className="absolute top-32 right-4 z-20 bg-black/80 text-white text-xs font-mono px-3 py-2 rounded shadow leading-5 pointer-events-none">
        <div className="font-bold mb-1">debug</div>
        <div>mounted: {String(debug.mounted)}</div>
        <div>container: {debug.containerSize}</div>
        <div>map created: {String(debug.mapCreated)}</div>
        <div>style loaded: {String(debug.styleLoaded)}</div>
        <div>shapes: {debug.shapesCount ?? '…'}</div>
        <div>halte: {debug.halteCount ?? '…'}</div>
        <div>positions: {debug.positionsCount ?? '…'}</div>
        <div>mode: {selectionMode}</div>
      </div>
      {error ? (
        <div className="absolute bottom-4 left-4 right-4 z-10 bg-red-600 text-white px-4 py-3 rounded shadow text-sm">
          <div className="font-semibold">Gagal ambil data dari backend ({API_BASE}).</div>
          <div className="mt-1 font-mono text-xs opacity-90">{(error as Error).message}</div>
          <div className="mt-1 text-xs opacity-90">
            Pastikan FastAPI berjalan di port 8000 (`make dev-backend`).
          </div>
        </div>
      ) : null}
    </>
  );
}
