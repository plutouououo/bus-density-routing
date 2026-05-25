'use client';

type Props = {
  simTime: number;
  setSimTime: (s: number) => void;
  timeMultiply: number;
  setTimeMultiply: (n: number) => void;
  isPlaying: boolean;
  setIsPlaying: (b: boolean) => void;
  minSeconds: number;
  maxSeconds: number;
};

const SPEEDS = [1, 5, 10, 60];

function formatClock(s: number): string {
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

export function SimulationControls({
  simTime,
  setSimTime,
  timeMultiply,
  setTimeMultiply,
  isPlaying,
  setIsPlaying,
  minSeconds,
  maxSeconds,
}: Props) {
  return (
    <div className="absolute top-4 left-4 right-4 z-10 bg-white/95 backdrop-blur rounded-lg shadow-lg px-4 py-3 text-gray-900">
      <div className="flex flex-wrap items-center gap-4">
        <button
          onClick={() => setIsPlaying(!isPlaying)}
          className="w-10 h-10 rounded-full bg-gray-900 text-white flex items-center justify-center hover:bg-gray-700"
          aria-label={isPlaying ? 'Pause' : 'Play'}
        >
          {isPlaying ? '⏸' : '▶'}
        </button>

        <div className="font-mono text-lg tabular-nums">
          Jam: <span className="font-bold">{formatClock(simTime)}</span>
        </div>

        <div className="flex items-center gap-2">
          <span className="text-sm text-gray-600">Kecepatan:</span>
          {SPEEDS.map((s) => (
            <button
              key={s}
              onClick={() => setTimeMultiply(s)}
              className={`px-3 py-1 rounded text-sm font-medium border ${
                timeMultiply === s
                  ? 'bg-gray-900 text-white border-gray-900'
                  : 'bg-white text-gray-700 border-gray-300 hover:bg-gray-100'
              }`}
            >
              {s}×
            </button>
          ))}
        </div>
      </div>

      <input
        type="range"
        min={minSeconds}
        max={maxSeconds}
        step={1}
        value={simTime}
        onChange={(e) => setSimTime(Number(e.target.value))}
        className="w-full mt-3 accent-gray-900"
      />
    </div>
  );
}
