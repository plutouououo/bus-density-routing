'use client';

import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import maplibregl, { type Map as MapLibreMap, type GeoJSONSource } from 'maplibre-gl';

const KORIDOR_COLOR: Record<string, string> = {
  '1': '#E74C3C',
  '2': '#3498DB',
  '3': '#2ECC71',
  '4': '#F39C12',
  '5': '#9B59B6',
};

type ShapesResponse = Record<string, [number, number][]>;
type HalteRow = { halte_id: string; nama: string; lat: number; lng: number };
type PositionsResponse = GeoJSON.FeatureCollection<GeoJSON.Point, {
  bus_id: string;
  koridor_id: number;
  bearing: number;
  next_stop: string;
  eta_minutes: number;
}>;

const EMPTY_FC: PositionsResponse = { type: 'FeatureCollection', features: [] };

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

export function SimulationMap({ simTime }: { simTime: number }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const styleLoadedRef = useRef(false);

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

  // Init map once.
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
    log('  html height:', document.documentElement.clientHeight, 'body height:', document.body.clientHeight);
    log('  parent height:', container.parentElement?.clientHeight);
    log('  window.innerHeight:', window.innerHeight);
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
    map.on('dataloading', (e) => log('dataloading', e.dataType, (e as { sourceId?: string }).sourceId ?? ''));
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
      map.on('mouseleave', 'buses-layer', () => (map.getCanvas().style.cursor = ''));
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

  // Add shape lines when data arrives.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !shapes) return;
    bumpDebug({ shapesCount: Object.keys(shapes).length });
    log('shapes received:', Object.keys(shapes).length, 'corridors');
    const apply = () => {
      for (const [koridorId, coords] of Object.entries(shapes)) {
        const srcId = `shape-${koridorId}`;
        if (map.getSource(srcId)) continue;
        map.addSource(srcId, {
          type: 'geojson',
          data: {
            type: 'Feature',
            geometry: { type: 'LineString', coordinates: coords },
            properties: {},
          },
        });
        map.addLayer(
          {
            id: `shape-layer-${koridorId}`,
            type: 'line',
            source: srcId,
            paint: {
              'line-color': KORIDOR_COLOR[koridorId] ?? '#666',
              'line-width': 4,
              'line-opacity': 0.75,
            },
          },
          'buses-layer',
        );
        log('added shape-layer-', koridorId, `(${coords.length} pts)`);
      }
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [shapes]);

  // Add halte points when data arrives.
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

      map.on('click', 'halte-layer', (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties as { halte_id: string; nama: string };
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
    };
    styleLoadedRef.current ? apply() : map.once('load', apply);
  }, [halte]);

  // Update bus positions whenever they change.
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
