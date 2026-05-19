/**
 * TransjakartaMap.tsx
 * ─────────────────────────────────────────────────────────────────────────────
 * Drop-in live-tracking map component for TransJakarta BRT.
 *
 * Usage (in your Next.js page / layout):
 *
 *   // Next.js requires dynamic import with SSR disabled for Leaflet
 *   import dynamic from "next/dynamic";
 *   const TransjakartaMap = dynamic(
 *     () => import("@/components/TransjakartaMap"),
 *     { ssr: false }
 *   );
 *
 *   export default function Page() {
 *     return (
 *       <TransjakartaMap
 *         corridors={CORRIDORS}
 *         stops={STOPS}
 *         busesPerCorridor={4}
 *         height="600px"
 *       />
 *     );
 *   }
 *
 * Dependencies (add to your Next.js project):
 *   npm install leaflet react-leaflet
 *   npm install -D @types/leaflet
 *
 * Also add to your global CSS (e.g. globals.css):
 *   @import "leaflet/dist/leaflet.css";
 * ─────────────────────────────────────────────────────────────────────────────
 */

"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import {
  MapContainer,
  TileLayer,
  Polyline,
  CircleMarker,
  Marker,
  Popup,
  useMap,
} from "react-leaflet";
import L from "leaflet";
import { useSimulator } from "./useSimulator";
import type { Bus, Corridor, Stop, TransjakartaMapProps } from "./types";

// ─── Leaflet icon fix (Next.js / webpack asset issue) ────────────────────────
// Leaflet's default marker icons break in bundled environments.
// We use divIcon (pure CSS/SVG) everywhere so no external PNG is needed.

function busIcon(color: string, label: string): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `
      <div style="
        background:${color};
        color:#fff;
        border:2px solid #fff;
        border-radius:50% 50% 50% 0;
        width:28px;
        height:28px;
        display:flex;
        align-items:center;
        justify-content:center;
        font-size:9px;
        font-weight:700;
        font-family:sans-serif;
        box-shadow:0 2px 6px rgba(0,0,0,.35);
        transform:rotate(-45deg);
        cursor:pointer;
      ">
        <span style="transform:rotate(45deg);line-height:1">${label.slice(0, 4)}</span>
      </div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 28],
    popupAnchor: [0, -30],
  });
}

function stopIcon(active: boolean): L.DivIcon {
  return L.divIcon({
    className: "",
    html: `<div style="
      width:10px;height:10px;
      background:${active ? "#fff" : "#ccc"};
      border:2px solid ${active ? "#333" : "#999"};
      border-radius:50%;
      box-shadow:0 1px 3px rgba(0,0,0,.3);
    "></div>`,
    iconSize: [10, 10],
    iconAnchor: [5, 5],
    popupAnchor: [0, -8],
  });
}

// ─── Sub-component: renders a single animated bus marker ─────────────────────

interface BusMarkerProps {
  bus: Bus;
  color: string;
  corridorName: string;
  onClick?: (bus: Bus, corridor: Corridor) => void;
  corridor: Corridor;
}

const BusMarker: React.FC<BusMarkerProps> = ({
  bus,
  color,
  corridorName,
  onClick,
  corridor,
}) => {
  const markerRef = useRef<L.Marker | null>(null);
  const icon = useMemo(() => busIcon(color, bus.label), [color, bus.label]);

  // Smoothly update position without re-mounting the marker
  useEffect(() => {
    markerRef.current?.setLatLng([bus.position.lat, bus.position.lng]);
  }, [bus.position]);

  return (
    <Marker
      ref={markerRef}
      position={[bus.position.lat, bus.position.lng]}
      icon={icon}
      eventHandlers={{
        click: () => onClick?.(bus, corridor),
      }}
    >
      <Popup>
        <div style={{ minWidth: 140 }}>
          <div
            style={{
              fontWeight: 700,
              fontSize: 14,
              color,
              marginBottom: 4,
            }}
          >
            {bus.label}
          </div>
          <div style={{ fontSize: 12, color: "#555" }}>
            <strong>Corridor {corridor.number}</strong> – {corridorName}
          </div>
          <div style={{ fontSize: 11, color: "#888", marginTop: 4 }}>
            Direction: {bus.forward ? "→ Terminus B" : "← Terminus A"}
          </div>
          <div style={{ fontSize: 11, color: "#888" }}>
            {bus.position.lat.toFixed(5)}, {bus.position.lng.toFixed(5)}
          </div>
        </div>
      </Popup>
    </Marker>
  );
};

// ─── Sub-component: auto-fit map bounds when corridors change ─────────────────

interface BoundsFitterProps {
  corridors: Corridor[];
}

const BoundsFitter: React.FC<BoundsFitterProps> = ({ corridors }) => {
  const map = useMap();

  useEffect(() => {
    if (corridors.length === 0) return;
    const allPoints = corridors.flatMap((c) =>
      c.routePoints.map((p) => [p.lat, p.lng] as [number, number])
    );
    if (allPoints.length > 0) {
      map.fitBounds(allPoints, { padding: [40, 40] });
    }
  }, [corridors, map]);

  return null;
};

// ─── Control panel overlay ────────────────────────────────────────────────────

interface ControlPanelProps {
  corridors: Corridor[];
  visibleCorridorIds: Set<string>;
  onToggleCorridor: (id: string) => void;
  busCount: number;
  running: boolean;
  onToggleRunning: () => void;
}

const ControlPanel: React.FC<ControlPanelProps> = ({
  corridors,
  visibleCorridorIds,
  onToggleCorridor,
  busCount,
  running,
  onToggleRunning,
}) => (
  <div
    style={{
      position: "absolute",
      top: 10,
      right: 10,
      zIndex: 1000,
      background: "rgba(255,255,255,0.97)",
      borderRadius: 10,
      boxShadow: "0 2px 12px rgba(0,0,0,.18)",
      padding: "12px 14px",
      minWidth: 190,
      fontFamily: "sans-serif",
    }}
  >
    <div
      style={{
        fontWeight: 700,
        fontSize: 13,
        marginBottom: 8,
        letterSpacing: 0.3,
      }}
    >
      🚌 TransJakarta Live
    </div>

    {/* Running toggle */}
    <button
      onClick={onToggleRunning}
      style={{
        width: "100%",
        marginBottom: 10,
        padding: "5px 0",
        borderRadius: 6,
        border: "none",
        background: running ? "#E63946" : "#2ecc71",
        color: "#fff",
        fontWeight: 700,
        fontSize: 12,
        cursor: "pointer",
      }}
    >
      {running ? "⏸ Pause" : "▶ Resume"}
    </button>

    {/* Corridor toggles */}
    <div style={{ fontSize: 11, color: "#888", marginBottom: 4 }}>
      Corridors
    </div>
    {corridors.map((c) => (
      <label
        key={c.id}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 4,
          cursor: "pointer",
          fontSize: 12,
        }}
      >
        <input
          type="checkbox"
          checked={visibleCorridorIds.has(c.id)}
          onChange={() => onToggleCorridor(c.id)}
        />
        <span
          style={{
            width: 10,
            height: 10,
            borderRadius: "50%",
            background: c.color,
            display: "inline-block",
          }}
        />
        <span>
          {c.number} – {c.name}
        </span>
      </label>
    ))}

    <div
      style={{
        marginTop: 8,
        paddingTop: 8,
        borderTop: "1px solid #eee",
        fontSize: 11,
        color: "#888",
      }}
    >
      {busCount} buses active
    </div>
  </div>
);

// ─── Main component ────────────────────────────────────────────────────────────

const DEFAULT_CENTER = { lat: -6.2, lng: 106.816 };
const DEFAULT_TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const DEFAULT_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

const TransjakartaMap: React.FC<TransjakartaMapProps> = ({
  corridors,
  stops,
  busesPerCorridor = 3,
  baseSpeed = 6,
  center = DEFAULT_CENTER,
  zoom = 13,
  height = "100vh",
  onBusClick,
  onStopClick,
  tileUrl = DEFAULT_TILE_URL,
  tileAttribution = DEFAULT_ATTRIBUTION,
}) => {
  // Which corridors are currently visible
  const [visibleCorridorIds, setVisibleCorridorIds] = useState<Set<string>>(
    () => new Set(corridors.map((c) => c.id))
  );

  const [running, setRunning] = useState(true);

  const visibleCorridors = useMemo(
    () => corridors.filter((c) => visibleCorridorIds.has(c.id)),
    [corridors, visibleCorridorIds]
  );

  // Build a corridor lookup map
  const corridorMap = useMemo(() => {
    const map = new Map<string, Corridor>();
    for (const c of corridors) map.set(c.id, c);
    return map;
  }, [corridors]);

  // Build a stop → color lookup (use first corridor's color if multiple)
  const stopCorridorColor = useMemo(() => {
    const map = new Map<string, string>();
    for (const stop of stops) {
      for (const cid of stop.corridorIds) {
        if (!map.has(stop.id) && corridorMap.has(cid)) {
          map.set(stop.id, corridorMap.get(cid)!.color);
        }
      }
    }
    return map;
  }, [stops, corridorMap]);

  const { buses, setEnabled } = useSimulator({
    corridors: visibleCorridors,
    busesPerCorridor,
    baseSpeed,
    enabled: running,
  });

  const handleToggleRunning = () => {
    setRunning((r) => {
      setEnabled(!r);
      return !r;
    });
  };

  const handleToggleCorridor = (id: string) => {
    setVisibleCorridorIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Filter stops to only those served by visible corridors
  const visibleStops = useMemo(
    () =>
      stops.filter((s) => s.corridorIds.some((cid) => visibleCorridorIds.has(cid))),
    [stops, visibleCorridorIds]
  );

  return (
    <div style={{ position: "relative", width: "100%", height }}>
      <MapContainer
        center={[center.lat, center.lng]}
        zoom={zoom}
        style={{ width: "100%", height: "100%" }}
        // Prevent scroll wheel zoom from hijacking page scroll
        scrollWheelZoom={false}
      >
        <TileLayer url={tileUrl} attribution={tileAttribution} />
        <BoundsFitter corridors={visibleCorridors} />

        {/* ── Route polylines ─────────────────────────────────────── */}
        {visibleCorridors.map((corridor) => (
          <Polyline
            key={corridor.id}
            positions={corridor.routePoints.map((p) => [p.lat, p.lng])}
            color={corridor.color}
            weight={4}
            opacity={0.75}
          />
        ))}

        {/* ── Stop markers ────────────────────────────────────────── */}
        {visibleStops.map((stop) => (
          <CircleMarker
            key={stop.id}
            center={[stop.position.lat, stop.position.lng]}
            radius={5}
            pathOptions={{
              color: stopCorridorColor.get(stop.id) ?? "#555",
              fillColor: "#fff",
              fillOpacity: 1,
              weight: 2,
            }}
            eventHandlers={{
              click: () => onStopClick?.(stop),
            }}
          >
            <Popup>
              <div style={{ fontFamily: "sans-serif" }}>
                <div style={{ fontWeight: 700, fontSize: 13 }}>{stop.name}</div>
                <div style={{ fontSize: 11, color: "#777", marginTop: 2 }}>
                  Corridors:{" "}
                  {stop.corridorIds
                    .map((cid) => corridorMap.get(cid)?.number ?? cid)
                    .join(", ")}
                </div>
              </div>
            </Popup>
          </CircleMarker>
        ))}

        {/* ── Bus markers ─────────────────────────────────────────── */}
        {buses.map((bus) => {
          const corridor = corridorMap.get(bus.corridorId);
          if (!corridor) return null;
          return (
            <BusMarker
              key={bus.id}
              bus={bus}
              color={corridor.color}
              corridorName={corridor.name}
              corridor={corridor}
              onClick={onBusClick}
            />
          );
        })}
      </MapContainer>

      {/* ── Control panel (positioned over map) ─────────────────── */}
      <ControlPanel
        corridors={corridors}
        visibleCorridorIds={visibleCorridorIds}
        onToggleCorridor={handleToggleCorridor}
        busCount={buses.length}
        running={running}
        onToggleRunning={handleToggleRunning}
      />
    </div>
  );
};

export default TransjakartaMap;