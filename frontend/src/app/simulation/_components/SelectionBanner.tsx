'use client';

import type { Halte, SelectionMode } from '@/lib/route-utils';

type Props = {
  selectionMode: SelectionMode;
  halteAsal: string | null;
  halteMap: Map<string, Halte>;
  onBatal: () => void;
};

export function SelectionBanner({ selectionMode, halteAsal, halteMap, onBatal }: Props) {
  if (selectionMode !== 'pilih_asal' && selectionMode !== 'pilih_tujuan') return null;
  const namaAsal = halteAsal ? halteMap.get(halteAsal)?.nama ?? halteAsal : null;

  return (
    <div className="absolute top-24 left-1/2 -translate-x-1/2 z-20 bg-blue-600 text-white text-sm px-5 py-2.5 rounded-full shadow-lg flex items-center gap-3 animate-in fade-in">
      {selectionMode === 'pilih_asal' ? (
        <span>👆 Klik halte <b>asal</b> di peta</span>
      ) : (
        <span>
          👆 Klik halte <b>tujuan</b>{' '}
          <span className="opacity-80">(asal: {namaAsal})</span>
        </span>
      )}
      <button
        onClick={onBatal}
        className="text-xs underline opacity-90 hover:opacity-100"
      >
        batal
      </button>
    </div>
  );
}
