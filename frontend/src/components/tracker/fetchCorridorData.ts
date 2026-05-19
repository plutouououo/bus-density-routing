/**
 * fetchCorridorData.ts
 * ─────────────────────────────────────────────────────────────────────────────
 * Fetches corridors, stops, and road-following route geometry from Supabase.
 * routePoints come from the `route_points` table (populated by
 * generate_route_points.py via OSRM), giving buses real road paths.
 *
 * Falls back to straight stop-to-stop lines if a corridor has no route_points
 * yet (e.g. the script hasn't been run for that corridor).
 * ─────────────────────────────────────────────────────────────────────────────
 */

import { createClient } from "@/utils/supabase/server";
import type { Corridor, Stop } from "./types";

// ─── Corridor colour palette (index matches corridor_code order) ──────────────
// Add more colours if you have more than 5 corridors.
const CORRIDOR_COLORS: Record<number, string> = {
  1: "#E63946", // red    — Blok M – Kota
  2: "#2196F3", // blue   — Pulogadung – Harmoni
  3: "#FF9800", // orange — Kalideres – Pasar Baru
  5: "#4CAF50", // green  — Kampung Melayu – Ancol
  9: "#9C27B0", // purple — Pinang Ranti – Pluit
};
const FALLBACK_COLOR = "#607D8B";

// ─── Raw Supabase row types ───────────────────────────────────────────────────

interface CorridorRow {
  id: string;
  corridor_code: number;
  name: string;
}

interface StopRow {
  id: string;
  stop_code: string;
  name: string;
  corridor_id: string;
  corridor_code: number;
  sequence: number;
  latitude: number;
  longitude: number;
}

interface RoutePointRow {
  corridor_id: string;
  sequence: number;
  latitude: number;
  longitude: number;
}

// ─── Main fetch function ──────────────────────────────────────────────────────

export interface CorridorData {
  corridors: Corridor[];
  stops: Stop[];
}

export async function fetchCorridorData(): Promise<CorridorData> {
  const supabase = await createClient();

  // Fetch corridors
  const { data: corridorRows, error: corridorError } = await supabase
    .from("corridors")
    .select("id, corridor_code, name")
    .order("corridor_code", { ascending: true });

  if (corridorError) {
    throw new Error(`Failed to fetch corridors: ${corridorError.message}`);
  }

  // Fetch all stops ordered by corridor then sequence
  const { data: stopRows, error: stopError } = await supabase
    .from("stops")
    .select(
      "id, stop_code, name, corridor_id, corridor_code, sequence, latitude, longitude"
    )
    .order("corridor_code", { ascending: true })
    .order("sequence", { ascending: true });

  if (stopError) {
    throw new Error(`Failed to fetch stops: ${stopError.message}`);
  }

  // Fetch road-following route points (populated by generate_route_points.py)
  const { data: routePointRows, error: routeError } = await supabase
    .from("route_points")
    .select("corridor_id, sequence, latitude, longitude")
    .order("corridor_id", { ascending: true })
    .order("sequence", { ascending: true });

  if (routeError) {
    throw new Error(`Failed to fetch route_points: ${routeError.message}`);
  }

  // Group stops by corridor_id
  const stopsByCorridor = new Map<string, StopRow[]>();
  for (const stop of stopRows as StopRow[]) {
    const list = stopsByCorridor.get(stop.corridor_id) ?? [];
    list.push(stop);
    stopsByCorridor.set(stop.corridor_id, list);
  }

  // Group route points by corridor_id
  const routePointsByCorridor = new Map<string, RoutePointRow[]>();
  for (const rp of routePointRows as RoutePointRow[]) {
    const list = routePointsByCorridor.get(rp.corridor_id) ?? [];
    list.push(rp);
    routePointsByCorridor.set(rp.corridor_id, list);
  }

  // Build Corridor[] objects
  const corridors: Corridor[] = (corridorRows as CorridorRow[]).map((row) => {
    const corridorStops = stopsByCorridor.get(row.id) ?? [];
    const roadPoints = routePointsByCorridor.get(row.id) ?? [];

    // Use road geometry if available; fall back to straight stop-to-stop lines
    const routePoints =
      roadPoints.length >= 2
        ? roadPoints.map((p) => ({ lat: p.latitude, lng: p.longitude }))
        : corridorStops.map((s) => ({ lat: s.latitude, lng: s.longitude }));

    return {
      id: row.id,
      number: String(row.corridor_code),
      name: row.name,
      color: CORRIDOR_COLORS[row.corridor_code] ?? FALLBACK_COLOR,
      stopIds: corridorStops.map((s) => s.id),
      routePoints,
    };
  });

  // Build Stop[] objects
  // A stop may belong to multiple corridors (interchange stops).
  const stopMap = new Map<string, Stop>();
  for (const row of stopRows as StopRow[]) {
    const existing = stopMap.get(row.id);
    if (existing) {
      if (!existing.corridorIds.includes(row.corridor_id)) {
        existing.corridorIds.push(row.corridor_id);
      }
    } else {
      stopMap.set(row.id, {
        id: row.id,
        name: row.name,
        position: { lat: row.latitude, lng: row.longitude },
        corridorIds: [row.corridor_id],
      });
    }
  }

  return {
    corridors,
    stops: Array.from(stopMap.values()),
  };
}