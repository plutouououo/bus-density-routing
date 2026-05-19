/**
 * useSimulator.ts
 * ─────────────────────────────────────────────────────────────────────────────
 * A React hook that spawns N simulated buses per corridor and animates them
 * along their routePoints at a configurable speed using requestAnimationFrame.
 *
 * When you later wire up real GPS data (WebSocket / SSE from your Python
 * backend), you can swap this hook out for one that listens to the socket
 * and maps incoming positions to Bus objects — the <TransjakartaMap> component
 * doesn't care where the Bus array comes from.
 * ─────────────────────────────────────────────────────────────────────────────
 */

import { useEffect, useRef, useState, useCallback } from "react";
import type { Bus, Corridor, LatLng } from "./types";

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Linear interpolation between two LatLng points */
function interpolate(a: LatLng, b: LatLng, t: number): LatLng {
  return {
    lat: a.lat + (b.lat - a.lat) * t,
    lng: a.lng + (b.lng - a.lng) * t,
  };
}

/** Haversine distance in metres between two points */
function haversine(a: LatLng, b: LatLng): number {
  const R = 6_371_000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const sinA = Math.sin(dLat / 2) ** 2;
  const sinB = Math.sin(dLng / 2) ** 2;
  const c =
    2 *
    Math.atan2(
      Math.sqrt(sinA + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * sinB),
      Math.sqrt(
        1 - sinA - Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * sinB
      )
    );
  return R * c;
}

/**
 * Pre-compute segment lengths so the bus moves at a constant real-world speed
 * rather than a constant index-step speed (segments vary a lot in length).
 */
function buildSegmentLengths(points: LatLng[]): number[] {
  const lengths: number[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    lengths.push(haversine(points[i], points[i + 1]));
  }
  return lengths;
}

/** Generate the initial bus fleet for all corridors */
function spawnBuses(
  corridors: Corridor[],
  busesPerCorridor: number,
  segmentLengths: Map<string, number[]>
): Bus[] {
  const buses: Bus[] = [];

  for (const corridor of corridors) {
    const points = corridor.routePoints;
    if (points.length < 2) continue;
    const lengths = segmentLengths.get(corridor.id)!;
    const totalSegments = points.length - 1;

    for (let i = 0; i < busesPerCorridor; i++) {
      // Spread buses evenly along the route so they don't all start at the same point
      const segmentIndex = Math.floor((i / busesPerCorridor) * totalSegments);
      const clampedIndex = Math.min(segmentIndex, totalSegments - 1);
      const progress = Math.random(); // random position within the starting segment

      buses.push({
        id: `${corridor.id}-bus-${i}`,
        corridorId: corridor.id,
        label: `B-${corridor.number}${String(1000 + i * 37).slice(1)}`,
        routeIndex: clampedIndex,
        progress,
        position: interpolate(
          points[clampedIndex],
          points[clampedIndex + 1],
          progress
        ),
        // Alternate direction so some buses go A→B, others B→A
        forward: i % 2 === 0,
        // Each bus gets a ±20% random speed factor for realism
        speedFactor: 0.85 + Math.random() * 0.3,
      });
    }
  }

  return buses;
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

export interface UseSimulatorOptions {
  corridors: Corridor[];
  busesPerCorridor?: number;
  /**
   * Base speed in metres-per-second. ~6 m/s ≈ 21 km/h (realistic BRT speed).
   * Increase to speed up the simulation; decrease to slow it down.
   */
  baseSpeed?: number;
  enabled?: boolean;
}

export interface UseSimulatorReturn {
  buses: Bus[];
  /** Pause / resume the simulation */
  setEnabled: (enabled: boolean) => void;
}

export function useSimulator({
  corridors,
  busesPerCorridor = 3,
  baseSpeed = 6,
  enabled: initialEnabled = true,
}: UseSimulatorOptions): UseSimulatorReturn {
  // Pre-compute segment lengths keyed by corridor id
  const segmentLengthsRef = useRef<Map<string, number[]>>(new Map());

  useEffect(() => {
    const map = new Map<string, number[]>();
    for (const corridor of corridors) {
      map.set(corridor.id, buildSegmentLengths(corridor.routePoints));
    }
    segmentLengthsRef.current = map;
  }, [corridors]);

  const [buses, setBuses] = useState<Bus[]>(() =>
    spawnBuses(corridors, busesPerCorridor, segmentLengthsRef.current)
  );
  const [enabled, setEnabled] = useState(initialEnabled);

  // Ref to avoid stale closures inside rAF
  const busesRef = useRef<Bus[]>(buses);
  const enabledRef = useRef(enabled);
  const rafRef = useRef<number | null>(null);
  const lastTimestampRef = useRef<number | null>(null);

  // Keep refs in sync
  useEffect(() => {
    enabledRef.current = enabled;
  }, [enabled]);

  // Re-spawn buses when corridors or busesPerCorridor change
  useEffect(() => {
    const newBuses = spawnBuses(
      corridors,
      busesPerCorridor,
      segmentLengthsRef.current
    );
    busesRef.current = newBuses;
    setBuses(newBuses);
  }, [corridors, busesPerCorridor]);

  // Build a lookup map for corridors
  const corridorMap = useRef<Map<string, Corridor>>(new Map());
  useEffect(() => {
    const map = new Map<string, Corridor>();
    for (const c of corridors) map.set(c.id, c);
    corridorMap.current = map;
  }, [corridors]);

  const tick = useCallback((timestamp: number) => {
    if (!enabledRef.current) {
      lastTimestampRef.current = null;
      rafRef.current = requestAnimationFrame(tick);
      return;
    }

    const dt =
      lastTimestampRef.current === null
        ? 0
        : (timestamp - lastTimestampRef.current) / 1000; // seconds
    lastTimestampRef.current = timestamp;

    if (dt === 0 || dt > 0.5) {
      // Skip first frame or very large gaps (tab was hidden)
      rafRef.current = requestAnimationFrame(tick);
      return;
    }

    const updatedBuses = busesRef.current.map((bus): Bus => {
      const corridor = corridorMap.current.get(bus.corridorId);
      if (!corridor) return bus;

      const points = corridor.routePoints;
      const lengths = segmentLengthsRef.current.get(bus.corridorId);
      if (!lengths || lengths.length === 0) return bus;

      const segmentLength = lengths[bus.routeIndex] || 1;
      // How much 0→1 progress does this bus cover this frame?
      const progressDelta =
        (baseSpeed * bus.speedFactor * dt) / segmentLength;

      let { routeIndex, progress, forward } = bus;
      progress += forward ? progressDelta : -progressDelta;

      // Advance through segments in one direction, bounce at termini
      while (progress >= 1) {
        progress -= 1;
        routeIndex++;
        if (routeIndex >= points.length - 1) {
          // Hit terminus B → reverse
          routeIndex = points.length - 2;
          progress = 1 - progress;
          forward = false;
        }
      }
      while (progress < 0) {
        progress += 1;
        routeIndex--;
        if (routeIndex < 0) {
          // Hit terminus A → reverse
          routeIndex = 0;
          progress = -progress;
          forward = true;
        }
      }

      const position = interpolate(
        points[routeIndex],
        points[routeIndex + 1],
        Math.max(0, Math.min(1, progress))
      );

      return { ...bus, routeIndex, progress, forward, position };
    });

    busesRef.current = updatedBuses;
    setBuses([...updatedBuses]);
    rafRef.current = requestAnimationFrame(tick);
  }, [baseSpeed]);

  useEffect(() => {
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
  }, [tick]);

  return { buses, setEnabled };
}