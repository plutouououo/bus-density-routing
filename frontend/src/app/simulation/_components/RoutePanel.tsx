'use client';

import type { Rute } from '@/lib/route-utils';
import { RouteCard } from './RouteCard';
import { BusEtaInfo } from './BusEtaInfo';

type Props = {
  hasilRute: Rute[] | undefined;
  isFetching: boolean;
  isError: boolean;
  errorMessage: string | null;
  ruteAktifIdx: number;
  setRuteAktifIdx: (i: number) => void;
  halteAsal: string | null;
  simTime: number;
  onReset: () => void;
};

function SkeletonLoader() {
  return (
    <div className="animate-pulse space-y-3">
      <div className="h-4 bg-gray-200 rounded w-2/3" />
      <div className="h-2 bg-gray-200 rounded" />
      <div className="h-20 bg-gray-100 rounded" />
      <div className="h-20 bg-gray-100 rounded" />
    </div>
  );
}

export function RoutePanel({
  hasilRute,
  isFetching,
  isError,
  errorMessage,
  ruteAktifIdx,
  setRuteAktifIdx,
  halteAsal,
  simTime,
  onReset,
}: Props) {
  // Container panel — selalu render saat selectionMode='hasil' (parent yang
  // mengatur visibility). Konten internal yang berubah tergantung state.
  return (
    <div className="absolute right-4 top-32 bottom-4 w-96 z-20 bg-white rounded-lg shadow-2xl overflow-hidden flex flex-col">
      {/* Header dengan tabs rute */}
      {hasilRute && hasilRute.length > 0 ? (
        <div className="flex border-b bg-gray-50">
          {hasilRute.map((r, i) => (
            <button
              key={i}
              onClick={() => setRuteAktifIdx(i)}
              className={`flex-1 px-2 py-2.5 text-xs font-medium transition-colors ${
                i === ruteAktifIdx
                  ? 'bg-gray-900 text-white'
                  : 'bg-white text-gray-700 hover:bg-gray-100'
              }`}
            >
              Rute {i + 1}
              {i === 0 ? ' ★' : ''}
              <div className="text-[10px] opacity-70 mt-0.5 tabular-nums">
                {r.skor.toFixed(2)} • {r.estimasi_menit}min
              </div>
            </button>
          ))}
          <button
            onClick={onReset}
            className="px-3 text-sm text-red-600 border-l hover:bg-red-50"
            aria-label="Tutup panel rute"
          >
            ✕
          </button>
        </div>
      ) : (
        <div className="flex justify-end p-2 border-b">
          <button
            onClick={onReset}
            className="px-3 py-1 text-sm text-red-600 hover:bg-red-50 rounded"
          >
            ✕ Tutup
          </button>
        </div>
      )}

      {/* Body */}
      <div className="flex-1 overflow-y-auto p-4">
        {isError ? (
          <div className="bg-red-50 border border-red-200 rounded p-3 text-sm text-red-800">
            <div className="font-semibold mb-1">Rute tidak ditemukan</div>
            <div className="text-xs">
              {errorMessage ??
                'Tidak ditemukan rute yang menghubungkan kedua halte ini dalam 5 koridor yang tersedia.'}
            </div>
          </div>
        ) : isFetching && !hasilRute ? (
          <SkeletonLoader />
        ) : hasilRute && hasilRute.length > 0 ? (
          <>
            <RouteCard rute={hasilRute[ruteAktifIdx]} />
            {halteAsal ? <BusEtaInfo halteId={halteAsal} simTime={simTime} /> : null}
          </>
        ) : (
          <div className="text-sm text-gray-500 text-center py-8">
            Tidak ada rute untuk ditampilkan.
          </div>
        )}
      </div>
    </div>
  );
}
