import { fetchCorridorData } from "@/components/tracker/fetchCorridorData";
import MapClient from "@/components/tracker/MapClient";

export default async function TrackingPage() {
  const { corridors, stops } = await fetchCorridorData();

  return (
    <main style={{ width: "100%", height: "100vh", overflow: "hidden" }}>
      <MapClient
        corridors={corridors}
        stops={stops}
        busesPerCorridor={4}
        baseSpeed={6}
        height="100vh"
      />
    </main>
  );
}
