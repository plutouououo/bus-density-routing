"use client";

import dynamic from "next/dynamic";
import type { TransjakartaMapProps } from "./types";

const TransjakartaMap = dynamic(() => import("./TransjakartaMap"), {
  ssr: false,
  loading: () => (
    <div
      style={{
        height: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "#f5f5f5",
        fontSize: 16,
        color: "#888",
      }}
    >
      Loading map…
    </div>
  ),
});

export default function MapClient(props: TransjakartaMapProps) {
  return <TransjakartaMap {...props} />;
}
