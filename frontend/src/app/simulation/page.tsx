'use client';

import { useEffect, useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import { useQuery } from '@tanstack/react-query';

import { SimulationControls } from './_components/SimulationControls';
import { RoutePanel } from './_components/RoutePanel';
import { SelectionBanner } from './_components/SelectionBanner';
import type { Halte, Rute, SelectionMode } from '@/lib/route-utils';

const SimulationMap = dynamic(
  () => import('./_components/SimulationMap').then((m) => m.SimulationMap),
  { ssr: false },
);

const START_SECONDS = 5 * 3600;
const END_SECONDS = 23 * 3600;
const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export default function SimulationPage() {
  // Skip SSR — lihat penjelasan di komentar Fase 4 (ekstensi browser
  // inject DOM attribute -> hydration mismatch + map jadi blank).
  const [mounted, setMounted] = useState(false);
  const [simTime, setSimTime] = useState(START_SECONDS);
  const [timeMultiply, setTimeMultiply] = useState(1);
  const [isPlaying, setIsPlaying] = useState(true);

  // ---- State pemilihan rute (Fase 5) ----
  const [selectionMode, setSelectionMode] = useState<SelectionMode>('idle');
  const [halteAsal, setHalteAsal] = useState<string | null>(null);
  const [halteTujuan, setHalteTujuan] = useState<string | null>(null);
  const [ruteAktifIdx, setRuteAktifIdx] = useState(0);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!isPlaying || !mounted) return;
    const id = setInterval(() => {
      setSimTime((t) => {
        const next = t + timeMultiply;
        return next >= END_SECONDS ? START_SECONDS : next;
      });
    }, 1000);
    return () => clearInterval(id);
  }, [isPlaying, timeMultiply, mounted]);

  // Halte master — queryKey 'halte' SAMA dengan yang dipakai SimulationMap
  // supaya TanStack Query dedupe network call jadi satu kali.
  const { data: halteData } = useQuery<Halte[]>({
    queryKey: ['halte'],
    queryFn: async () => {
      const r = await fetch(`${API_BASE}/api/simulation/halte`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    staleTime: Infinity,
    enabled: mounted,
  });

  const halteMap = useMemo(() => {
    const m = new Map<string, Halte>();
    if (halteData) for (const h of halteData) m.set(h.halte_id, h);
    return m;
  }, [halteData]);

  // Jam simulasi → bucket per jam. Query Dijkstra hanya re-trigger saat jam
  // sim berganti, bukan tiap detik sim (yang akan boros + tidak berguna
  // karena kepadatan prediktif granular per jam).
  const simJam = Math.floor(simTime / 3600) % 24;

  const {
    data: hasilRute,
    isFetching: ruteFetching,
    isError: ruteIsError,
    error: ruteError,
  } = useQuery<Rute[]>({
    queryKey: ['rute', halteAsal, halteTujuan, simJam],
    queryFn: async () => {
      // sim_time dikirim sebagai snapshot saat query fire — dipakai bus_selector
      // untuk hitung ETA & memfilter bus yang sudah lewat. Tidak masuk queryKey
      // agar Dijkstra tidak re-run tiap detik; rekomendasi bus tetap valid
      // dalam jendela jam yang sama.
      const r = await fetch(`${API_BASE}/api/rute/rekomendasi`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          halte_asal: halteAsal,
          halte_tujuan: halteTujuan,
          jam: simJam,
          hari_tipe: 'weekday',
          sim_time: simTime,
        }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body?.detail ?? `HTTP ${r.status}`);
      }
      return r.json();
    },
    enabled: selectionMode === 'hasil' && !!halteAsal && !!halteTujuan,
    staleTime: 60_000,
    retry: 1,
  });

  // ---- Handler interaksi ----
  const handleHalteClick = (halteId: string) => {
    if (selectionMode === 'pilih_asal') {
      setHalteAsal(halteId);
      setHalteTujuan(null);
      setSelectionMode('pilih_tujuan');
    } else if (selectionMode === 'pilih_tujuan') {
      if (halteId === halteAsal) return; // abaikan klik halte yang sama
      setHalteTujuan(halteId);
      setRuteAktifIdx(0);
      setSelectionMode('hasil');
    }
  };

  const handleMulaiPilih = () => {
    setSelectionMode('pilih_asal');
    setHalteAsal(null);
    setHalteTujuan(null);
    setRuteAktifIdx(0);
  };

  const handleReset = () => {
    setSelectionMode('idle');
    setHalteAsal(null);
    setHalteTujuan(null);
    setRuteAktifIdx(0);
  };

  const wrapperStyle = {
    position: 'fixed' as const,
    top: 0,
    left: 0,
    width: '100vw',
    height: '100vh',
    overflow: 'hidden' as const,
  };

  if (!mounted) {
    return <div style={wrapperStyle} />;
  }

  return (
    <div style={wrapperStyle}>
      <SimulationMap
        simTime={simTime}
        selectionMode={selectionMode}
        halteAsal={halteAsal}
        halteTujuan={halteTujuan}
        hasilRute={hasilRute}
        ruteAktifIdx={ruteAktifIdx}
        halteMap={halteMap}
        onHalteClick={handleHalteClick}
      />
      <SimulationControls
        simTime={simTime}
        setSimTime={setSimTime}
        timeMultiply={timeMultiply}
        setTimeMultiply={setTimeMultiply}
        isPlaying={isPlaying}
        setIsPlaying={setIsPlaying}
        minSeconds={START_SECONDS}
        maxSeconds={END_SECONDS}
        selectionMode={selectionMode}
        onCariRute={handleMulaiPilih}
        onReset={handleReset}
      />
      <SelectionBanner
        selectionMode={selectionMode}
        halteAsal={halteAsal}
        halteMap={halteMap}
        onBatal={handleReset}
      />
      {selectionMode === 'hasil' && (
        <RoutePanel
          hasilRute={hasilRute}
          isFetching={ruteFetching}
          isError={ruteIsError}
          errorMessage={ruteError instanceof Error ? ruteError.message : null}
          ruteAktifIdx={ruteAktifIdx}
          setRuteAktifIdx={setRuteAktifIdx}
          halteAsal={halteAsal}
          simTime={simTime}
          onReset={handleReset}
        />
      )}
    </div>
  );
}
