'use client';

import {
  type BusRekomendasi,
  type Rute,
  kepadatanKeWarna,
  labelKepadatan,
  normalisasiKepadatanDisplay,
} from '@/lib/route-utils';

// Warna semafor untuk label bus_rekomendasi. Threshold disinkronkan dengan
// backend services/bus_selector.py (<=0.33 Sepi, <=0.66 Sedang, >0.66 Padat),
// jadi UI tidak menghitung ulang — pakai label dari payload langsung.
const LABEL_STYLE: Record<BusRekomendasi['label_kepadatan'], { bg: string; fg: string; dot: string }> = {
  Sepi:   { bg: '#dcfce7', fg: '#166534', dot: '#22c55e' },
  Sedang: { bg: '#fef9c3', fg: '#854d0e', dot: '#eab308' },
  Padat:  { bg: '#fee2e2', fg: '#991b1b', dot: '#ef4444' },
};

function BusRekomendasiBadge({ rek }: { rek: BusRekomendasi }) {
  const style = LABEL_STYLE[rek.label_kepadatan];
  return (
    <div
      className="mt-2 flex items-center gap-2 text-xs px-2 py-1.5 rounded"
      style={{ background: style.bg, color: style.fg }}
    >
      <span>🚌</span>
      <span className="font-semibold">Naik Bus {rek.bus_id}</span>
      <span className="opacity-80">• {rek.eta_menit} mnt lagi •</span>
      <span className="flex items-center gap-1 font-medium">
        <span
          className="inline-block w-2 h-2 rounded-full"
          style={{ background: style.dot }}
        />
        {rek.label_kepadatan}
      </span>
    </div>
  );
}

function KepadatanBar({ value }: { value: number }) {
  const displayValue = normalisasiKepadatanDisplay(value);
  const pct = Math.round(displayValue * 100);
  const warna = kepadatanKeWarna(displayValue);
  const label = labelKepadatan(displayValue);
  return (
    <div className="flex items-center gap-2 text-xs">
      <div className="flex-1 h-2 bg-gray-100 rounded overflow-hidden">
        <div
          style={{ width: `${pct}%`, background: warna }}
          className="h-full transition-all"
        />
      </div>
      <span className="text-gray-600 tabular-nums w-20 text-right">
        {pct}% • {label}
      </span>
    </div>
  );
}

export function RouteCard({ rute }: { rute: Rute }) {
  return (
    <div>
      <div className="text-xs text-gray-500 mb-2">
        Skor{' '}
        <b className="text-gray-900 text-sm tabular-nums">
          {rute.skor.toFixed(2)}
        </b>{' '}
        • {rute.jumlah_transit} transit • ~{rute.estimasi_menit} menit
      </div>
      <div className="mb-4">
        <div className="text-xs text-gray-600 mb-1">Kepadatan rata-rata</div>
        <KepadatanBar value={rute.rata_kepadatan} />
      </div>

      <div className="space-y-3">
        {rute.segmen.map((s, i) => {
          if (s.tipe === 'transit') {
            return (
              <div key={i} className="flex items-start gap-2 text-sm bg-purple-50 px-3 py-2 rounded">
                <span>🔄</span>
                <div>
                  <div className="font-medium text-gray-800">
                    Transit di {s.transit_di}
                  </div>
                  <div className="text-xs text-gray-500">
                    Koridor {s.dari_koridor ?? '?'} → Koridor {s.ke_koridor}
                  </div>
                </div>
              </div>
            );
          }
          return (
            <div
              key={i}
              className="border-l-4 pl-3 py-1"
              style={{ borderColor: kepadatanKeWarna(s.kepadatan) }}
            >
              <div className="text-sm font-semibold">
                🚌 Koridor {s.koridor_id}
                {s.nama_koridor ? <span className="text-gray-600 font-normal"> — {s.nama_koridor}</span> : null}
              </div>
              <div className="text-xs text-gray-700 mt-1">
                Naik di <b>{s.naik_di}</b>
              </div>
              <div className="text-xs text-gray-700">
                Turun di <b>{s.turun_di}</b> <span className="text-gray-500">({s.waktu_menit} menit)</span>
              </div>
              <div className="mt-2">
                <KepadatanBar value={s.kepadatan} />
              </div>
              {s.bus_rekomendasi ? <BusRekomendasiBadge rek={s.bus_rekomendasi} /> : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
