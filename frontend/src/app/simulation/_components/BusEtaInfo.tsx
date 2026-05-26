'use client';

import { useQuery } from '@tanstack/react-query';

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

type BusItem = {
  bus_id: string;
  koridor_id: number;
  eta_detik: number;
  eta_menit: number;
};

type Props = {
  halteId: string;
  simTime: number;
};

export function BusEtaInfo({ halteId, simTime }: Props) {
  // Bucket simTime per 30 detik sim — query Dijkstra-side ringan tapi tidak
  // perlu rerun tiap detik. ETA tetap akurat ±30 detik sim.
  const simBucket = Math.floor(simTime / 30) * 30;

  const { data, isFetching, isError } = useQuery<BusItem[]>({
    queryKey: ['bus-berikutnya', halteId, simBucket],
    queryFn: async () => {
      const r = await fetch(
        `${API_BASE}/api/rute/halte/${halteId}/bus-berikutnya?sim_time=${simBucket}`,
      );
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    enabled: !!halteId,
    staleTime: 30_000,
  });

  if (isError) {
    return (
      <div className="mt-4 pt-4 border-t text-xs text-red-600">
        Gagal memuat info bus berikutnya.
      </div>
    );
  }

  if (!data || data.length === 0) {
    return (
      <div className="mt-4 pt-4 border-t text-xs text-gray-500">
        {isFetching
          ? 'Memuat info bus berikutnya…'
          : 'Tidak ada bus terjadwal setelah waktu ini.'}
      </div>
    );
  }

  return (
    <div className="mt-4 pt-4 border-t">
      <div className="text-xs font-semibold text-gray-700 mb-2">
        🚏 Bus berikutnya di halte asal:
      </div>
      <ul className="space-y-1 text-xs">
        {data.map((b) => (
          <li
            key={b.bus_id}
            className="flex justify-between items-center bg-gray-50 px-2 py-1 rounded"
          >
            <span>Koridor {b.koridor_id}</span>
            <span className="font-mono tabular-nums text-gray-700">
              ~{b.eta_menit} menit{' '}
              <span className="text-gray-400 text-[10px]">({b.bus_id})</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
