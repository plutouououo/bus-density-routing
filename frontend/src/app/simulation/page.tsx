'use client';

import { useEffect, useState } from 'react';
import dynamic from 'next/dynamic';
import { SimulationControls } from './_components/SimulationControls';

const SimulationMap = dynamic(
  () => import('./_components/SimulationMap').then((m) => m.SimulationMap),
  { ssr: false },
);

const START_SECONDS = 5 * 3600;
const END_SECONDS = 23 * 3600;

export default function SimulationPage() {
  // Skip SSR for this page entirely: browser extensions (Grammarly, form
  // fillers) inject DOM attributes between server render and hydration, which
  // breaks React 19 hydration and leaves the dynamic-imported map in a half-
  // mounted state. Rendering null on server + first client tick keeps the two
  // outputs identical, so there's nothing to hydrate.
  const [mounted, setMounted] = useState(false);
  const [simTime, setSimTime] = useState(START_SECONDS);
  const [timeMultiply, setTimeMultiply] = useState(1);
  const [isPlaying, setIsPlaying] = useState(true);

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
      <SimulationMap simTime={simTime} />
      <SimulationControls
        simTime={simTime}
        setSimTime={setSimTime}
        timeMultiply={timeMultiply}
        setTimeMultiply={setTimeMultiply}
        isPlaying={isPlaying}
        setIsPlaying={setIsPlaying}
        minSeconds={START_SECONDS}
        maxSeconds={END_SECONDS}
      />
    </div>
  );
}
