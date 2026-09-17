import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { GpuLane } from "../gpu/GpuLane";
import { GpuLaneExpanded } from "../gpu/GpuLaneExpanded";
import { ThermalPower } from "../gpu/ThermalPower";
import { VramGauge } from "../gpu/VramGauge";
import { availabilityView } from "../gpu/availability";
import type { GpuInfo } from "../types/models";

function makeGpu(overrides: Partial<GpuInfo> = {}): GpuInfo {
  return {
    index: 0,
    uuid: "GPU-0",
    name: "NVIDIA GeForce RTX 4090",
    utilization_percent: 72.4,
    vram_used_b: 19541268941,
    vram_total_b: 25769803776,
    vram_percent: 75.8,
    temperature_c: 64,
    power_watts: 210,
    power_limit_watts: 350,
    fan_percent: 55,
    availability: "active",
    process_count: 1,
    ...overrides,
  };
}

describe("GpuLane", () => {
  it("renders the compressed lane: name, label, availability, owners", () => {
    render(<GpuLane gpu={makeGpu()} owners={["demo"]} />);
    expect(screen.getByText("GPU0")).toBeTruthy();
    expect(screen.getByText("72%")).toBeTruthy();
    expect(screen.getByText("ACTIVE")).toBeTruthy();
    expect(screen.getByText("users: demo")).toBeTruthy();
    expect(screen.getByText("64°")).toBeTruthy();
    expect(screen.getByText("210/350W")).toBeTruthy();
    expect(screen.getByText("18.2/24.0 GB")).toBeTruthy();
  });

  it("renders the availability vocabulary per state", () => {
    const { rerender } = render(<GpuLane gpu={makeGpu({ availability: "free" })} />);
    expect(screen.getByText("FREE")).toBeTruthy();
    rerender(<GpuLane gpu={makeGpu({ availability: "saturated" })} />);
    expect(screen.getByText("SATURATED")).toBeTruthy();
    rerender(<GpuLane gpu={makeGpu({ availability: "unavailable" })} />);
    expect(screen.getByText("N/A")).toBeTruthy();
  });

  it("renders an em dash when no owners hold the GPU", () => {
    render(<GpuLane gpu={makeGpu()} />);
    expect(screen.getByText("—")).toBeTruthy();
  });
});

describe("VramGauge", () => {
  it("labels used/total in one unit", () => {
    render(<VramGauge usedB={19541268941} totalB={25769803776} />);
    expect(screen.getByText("18.2/24.0 GB")).toBeTruthy();
  });

  it("renders N/A without telemetry", () => {
    render(<VramGauge usedB={null} totalB={null} />);
    expect(screen.getByText("N/A")).toBeTruthy();
  });

  it("derives percent from used/total when absent", () => {
    render(<VramGauge usedB={12_884_901_888} totalB={25769803776} />);
    // 50% → 9 of 18 segments filled
    expect(document.querySelectorAll(".meter__seg--filled")).toHaveLength(9);
  });
});

describe("ThermalPower", () => {
  it("color-codes temperature tiers", () => {
    const { rerender } = render(
      <ThermalPower temperatureC={35} powerWatts={20} powerLimitWatts={350} />,
    );
    expect(document.querySelector(".thermal__temp--cold")).toBeTruthy();
    rerender(<ThermalPower temperatureC={78} powerWatts={300} powerLimitWatts={350} />);
    expect(document.querySelector(".thermal__temp--warn")).toBeTruthy();
    rerender(<ThermalPower temperatureC={90} powerWatts={320} powerLimitWatts={350} />);
    expect(document.querySelector(".thermal__temp--crit")).toBeTruthy();
  });

  it("renders N/A pair when nothing is reported", () => {
    render(<ThermalPower temperatureC={null} powerWatts={null} powerLimitWatts={null} />);
    const text = document.querySelector(".thermal")?.textContent ?? "";
    expect(text).toContain("N/A");
  });
});

describe("availabilityView", () => {
  it("maps availability to label + dot", () => {
    expect(availabilityView("free")).toEqual({ labelKey: "free", dot: "online", tone: "free" });
    expect(availabilityView("active")).toEqual({ labelKey: "active", dot: "active", tone: "active" });
    expect(availabilityView("saturated")).toEqual({
      labelKey: "saturated",
      dot: "stale",
      tone: "saturated",
    });
    expect(availabilityView("unavailable")).toEqual({ labelKey: "unavailable", dot: "unknown", tone: "na" });
  });
});

describe("GpuLaneExpanded", () => {
  it("adds hero numeral, sparklines, and the process rows slot", () => {
    const history = Array.from({ length: 120 }, (_unused, i) => 50 + Math.sin(i / 5) * 20);
    render(
      <GpuLaneExpanded
        gpu={makeGpu()}
        utilizationHistory={history}
        vramHistory={history}
        owners={["demo"]}
      >
        <div className="proc-row">40211 demo python train.py 17.8 GB</div>
      </GpuLaneExpanded>,
    );
    expect(screen.getByText("72.4%")).toBeTruthy(); // hero numeral, one decimal
    expect(screen.getByLabelText("GPU utilization")).toBeTruthy();
    expect(screen.getByLabelText("GPU VRAM history")).toBeTruthy();
    expect(document.querySelectorAll(".gpu-lane-x polyline")).toHaveLength(2);
    expect(screen.getByText("users: demo")).toBeTruthy();
    expect(screen.getByText(/40211 demo python/)).toBeTruthy();
  });

  it("renders without history or process slot", () => {
    render(<GpuLaneExpanded gpu={makeGpu()} />);
    expect(document.querySelector(".gpu-lane-x polyline")).toBeNull();
    expect(document.querySelector(".gpu-lane-x__procs")).toBeNull();
  });
});
