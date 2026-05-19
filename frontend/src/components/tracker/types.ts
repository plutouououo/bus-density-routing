// ─── Core geographic primitives ──────────────────────────────────────────────

export interface LatLng {
  lat: number;
  lng: number;
}

// ─── Stop ────────────────────────────────────────────────────────────────────

export interface Stop {
  id: string;
  name: string;
  position: LatLng;
  /** Which corridor IDs serve this stop */
  corridorIds: string[];
}

// ─── Corridor (route) ─────────────────────────────────────────────────────────

export interface Corridor {
  id: string;
  /** e.g. "1", "2", "2A" */
  number: string;
  name: string;
  /** Ordered list of stop IDs from terminus A → terminus B */
  stopIds: string[];
  /**
   * Ordered polyline waypoints that define the road geometry of the route.
   * Must start at the first stop and end at the last stop.
   * Include intermediate GPS points to follow roads accurately.
   */
  routePoints: LatLng[];
  color: string;
}

// ─── Simulated Bus ────────────────────────────────────────────────────────────

export interface Bus {
  id: string;
  corridorId: string;
  /** Display name shown in the popup, e.g. "B-7401" */
  label: string;
  /** Index into corridor.routePoints — current position along the route */
  routeIndex: number;
  /** 0.0–1.0 fractional progress between routePoints[routeIndex] and [routeIndex+1] */
  progress: number;
  /** Current interpolated position */
  position: LatLng;
  /** Direction of travel: true = A→B, false = B→A */
  forward: boolean;
  /** Speed multiplier relative to the base simulation speed */
  speedFactor: number;
}

// ─── Component props ──────────────────────────────────────────────────────────

export interface TransjakartaMapProps {
  /**
   * All corridors to show. You can pass a subset to display only certain routes.
   */
  corridors: Corridor[];
  /**
   * All stops. The component renders markers for stops that belong to at least
   * one of the supplied corridors.
   */
  stops: Stop[];
  /**
   * How many simulated buses to spawn per corridor.
   * @default 3
   */
  busesPerCorridor?: number;
  /**
   * Base speed in route-points-per-second for every bus.
   * Each bus is also given a random ±20% speed factor on top of this.
   * @default 0.4
   */
  baseSpeed?: number;
  /**
   * Initial map centre. Defaults to Jakarta city centre.
   */
  center?: LatLng;
  /**
   * Initial zoom level.
   * @default 12
   */
  zoom?: number;
  /**
   * CSS height of the map container.
   * @default "100vh"
   */
  height?: string;
  /**
   * Called whenever the user clicks a bus marker.
   */
  onBusClick?: (bus: Bus, corridor: Corridor) => void;
  /**
   * Called whenever the user clicks a stop marker.
   */
  onStopClick?: (stop: Stop) => void;
  /**
   * Tile layer URL template. Defaults to OpenStreetMap.
   * Swap for a Mapbox/Stadia/etc. tile URL in production.
   */
  tileUrl?: string;
  /**
   * Tile layer attribution string.
   */
  tileAttribution?: string;
}