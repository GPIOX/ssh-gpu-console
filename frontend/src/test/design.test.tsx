import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import {
  Button,
  Chip,
  Dialog,
  EmptyState,
  ErrorPanel,
  Menu,
  Panel,
  Section,
  SegmentedMeter,
  Skeleton,
  Spinner,
  StatusDot,
  dotStatusFromServerStatus,
} from "../design";
import { Sparkline } from "../gpu/Sparkline";

describe("SegmentedMeter", () => {
  it("fills segments proportionally with an adjacent mono label", () => {
    render(<SegmentedMeter value={72} segments={10} />);
    const strip = document.querySelector(".meter__strip");
    // 10 segments + the shared continuous-fill node (Warm primitive)
    expect(strip?.querySelectorAll(".meter__seg")).toHaveLength(10);
    expect(strip?.querySelectorAll(".meter__seg--filled")).toHaveLength(7);
    expect(screen.getByText("72%")).toBeTruthy();
  });

  it("renders an empty track and N/A for null", () => {
    render(<SegmentedMeter value={null} segments={10} />);
    expect(document.querySelectorAll(".meter__seg--filled")).toHaveLength(0);
    expect(screen.getByText("N/A")).toBeTruthy();
  });

  it("derives state color from the value in auto mode", () => {
    const { rerender } = render(<SegmentedMeter value={30} segments={10} />);
    expect(document.querySelector(".meter__seg--ok")).toBeTruthy();
    rerender(<SegmentedMeter value={80} segments={10} />);
    expect(document.querySelector(".meter__seg--warn")).toBeTruthy();
    rerender(<SegmentedMeter value={95} segments={10} />);
    expect(document.querySelector(".meter__seg--crit")).toBeTruthy();
  });

  it("honors explicit state and value text overrides", () => {
    render(
      <SegmentedMeter value={20} state="crit" segments={5} valueText="18.2/24.0 GB" />,
    );
    expect(document.querySelector(".meter__seg--crit")).toBeTruthy();
    expect(screen.getByText("18.2/24.0 GB")).toBeTruthy();
  });
});

describe("StatusDot", () => {
  it("renders variant classes", () => {
    const { rerender } = render(<StatusDot status="online" />);
    expect(document.querySelector(".status-dot--online")).toBeTruthy();
    rerender(<StatusDot status="stale" />);
    expect(document.querySelector(".status-dot--stale")).toBeTruthy();
    rerender(<StatusDot status="offline" />);
    expect(document.querySelector(".status-dot--offline")).toBeTruthy();
    rerender(<StatusDot status="error" />);
    expect(document.querySelector(".status-dot--error")).toBeTruthy();
  });

  it("pulses only while connecting", () => {
    const { rerender } = render(<StatusDot status="connecting" />);
    expect(document.querySelector(".status-dot--pulse")).toBeTruthy();
    rerender(<StatusDot status="online" />);
    expect(document.querySelector(".status-dot--pulse")).toBeNull();
  });

  it("supports the ring and labels", () => {
    render(<StatusDot status="online" ring label="server online" />);
    expect(document.querySelector(".status-dot--ring")).toBeTruthy();
    expect(screen.getByLabelText("server online")).toBeTruthy();
  });

  it("maps the ServerStatus taxonomy", () => {
    expect(dotStatusFromServerStatus("online")).toBe("online");
    expect(dotStatusFromServerStatus("reconnecting")).toBe("connecting");
    expect(dotStatusFromServerStatus("degraded")).toBe("stale");
    expect(dotStatusFromServerStatus("timeout")).toBe("offline");
    expect(dotStatusFromServerStatus("authentication_failed")).toBe("error");
    expect(dotStatusFromServerStatus("host_key_error")).toBe("error");
    expect(dotStatusFromServerStatus("unknown")).toBe("unknown");
  });
});

describe("Sparkline", () => {
  it("draws one polyline over the samples", () => {
    const { container } = render(<Sparkline values={[10, 50, 30, 80]} />);
    const polyline = container.querySelector("polyline");
    expect(polyline?.getAttribute("points")?.trim().split(" ")).toHaveLength(4);
    expect(container.querySelector("polygon")).toBeNull();
  });

  it("renders nothing for fewer than two samples", () => {
    const { container } = render(<Sparkline values={[42]} />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("adds an 8%-opacity area fill on demand", () => {
    const { container } = render(<Sparkline values={[1, 2, 3]} fill />);
    expect(container.querySelector("polygon")).toBeTruthy();
  });

  it("clips to the last 120 samples", () => {
    const values = Array.from({ length: 130 }, (_unused, i) => i);
    const { container } = render(<Sparkline values={values} />);
    expect(container.querySelector("polyline")?.getAttribute("points")?.trim().split(" ")).toHaveLength(
      120,
    );
  });
});

describe("Menu", () => {
  const items = [
    { id: "test", label: "Test connection", onSelect: vi.fn() },
    { id: "del", label: "Delete server", danger: true, onSelect: vi.fn() },
  ];

  it("opens on trigger click and runs the chosen action", () => {
    render(<Menu items={items} triggerLabel="Server actions" />);
    fireEvent.click(screen.getByLabelText("Server actions"));
    expect(screen.getByRole("menu")).toBeTruthy();
    fireEvent.click(screen.getByText("Delete server"));
    expect(items[1].onSelect).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("closes on Escape and on outside pointer down", () => {
    const { rerender } = render(<Menu items={items} triggerLabel="Server actions" />);
    fireEvent.click(screen.getByLabelText("Server actions"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();

    rerender(<Menu items={items} triggerLabel="Server actions" />);
    fireEvent.click(screen.getByLabelText("Server actions"));
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });
});

describe("Dialog", () => {
  it("renders into a portal with title, and Escape closes", () => {
    const onClose = vi.fn();
    render(
      <Dialog open onClose={onClose} title="Terminate process 40211">
        <p>detail</p>
      </Dialog>,
    );
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Terminate process 40211")).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("renders nothing when closed", () => {
    render(
      <Dialog open={false} onClose={() => undefined} title="Nope">
        <p>hidden</p>
      </Dialog>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("puts initial focus on the first text field, not the header close button", async () => {
    render(
      <Dialog open onClose={() => undefined} title="Add project">
        <input aria-label="Name" />
      </Dialog>,
    );
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText("Name"));
    });
  });

  it("keeps field focus when the host re-renders with a fresh onClose closure", async () => {
    const onClose = vi.fn();
    const view = render(
      <Dialog open onClose={onClose} title="Add project">
        <input aria-label="Name" />
      </Dialog>,
    );
    const name = screen.getByLabelText("Name") as HTMLInputElement;
    await waitFor(() => {
      expect(document.activeElement).toBe(name);
    });
    fireEvent.change(name, { target: { value: "dinov2" } });

    // Hosts like ProjectsTab re-render every clock tick and pass a new inline
    // closure — the dialog must not steal focus back out of the field. The
    // steal (when present) fires inside a rAF, so wait one frame out.
    view.rerender(
      <Dialog open onClose={() => undefined} title="Add project">
        <input aria-label="Name" />
      </Dialog>,
    );
    await new Promise((resolve) => requestAnimationFrame(() => resolve(undefined)));
    expect(document.activeElement).toBe(name);
    expect(name.value).toBe("dinov2");
  });

  it("Escape still closes after the onClose closure changed mid-flight", async () => {
    const first = vi.fn();
    const second = vi.fn();
    const view = render(
      <Dialog open onClose={first} title="Add project">
        <input aria-label="Name" />
      </Dialog>,
    );
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText("Name"));
    });
    view.rerender(
      <Dialog open onClose={second} title="Add project">
        <input aria-label="Name" />
      </Dialog>,
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});

describe("static primitives", () => {
  it("Panel, Section, Chip, Skeleton, Spinner, EmptyState, ErrorPanel, Button", () => {
    render(
      <div>
        <Panel className="probe">panel-body</Panel>
        <Section title="GPU lanes" meta="3 free">
          content
        </Section>
        <Chip tone="warn" mono>
          stale · 42s
        </Chip>
        <Skeleton width={80} height={10} />
        <Spinner label="loading fleet" />
        <EmptyState title="No servers" hint="Add one to begin." />
        <ErrorPanel title="Authentication failed" detail="permission denied" />
        <Button variant="primary">Trust host key</Button>
      </div>,
    );
    expect(document.querySelector(".panel.probe")).toBeTruthy();
    expect(screen.getByText("GPU lanes")).toBeTruthy();
    expect(screen.getByText("3 free")).toBeTruthy();
    expect(document.querySelector(".chip--warn.chip--mono")).toBeTruthy();
    expect(document.querySelector(".skeleton")).toBeTruthy();
    expect(screen.getByLabelText("loading fleet")).toBeTruthy();
    expect(screen.getByText("No servers")).toBeTruthy();
    expect(screen.getByRole("alert")).toBeTruthy();
    expect(screen.getByText("Trust host key")).toBeTruthy();
  });
});
