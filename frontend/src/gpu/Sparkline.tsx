import { cx } from "../utils/cx";
import "./sparkline.css";

export interface SparklineProps {
  /** Samples; only the last 120 are drawn. */
  values: number[];
  min?: number;
  max?: number;
  /** viewBox width (coordinate space, scales to container). */
  width?: number;
  /** viewBox height. */
  height?: number;
  /** Add an 8%-opacity area fill under the line. */
  fill?: boolean;
  className?: string;
  label?: string;
}

/**
 * Plain SVG polyline over ≤120 samples. No animation, no markers, 1.5px
 * non-scaling stroke; color comes from `currentColor` so callers tint by state.
 */
export function Sparkline({
  values,
  min = 0,
  max = 100,
  width = 120,
  height = 28,
  fill = false,
  className,
  label,
}: SparklineProps) {
  const data = values.slice(-120);
  if (data.length < 2) {
    return <span className={cx("sparkline", "sparkline--empty", className)} aria-label={label} />;
  }
  const span = max - min || 1;
  const stepX = width / (data.length - 1);
  const points = data.map((value, index) => {
    const x = index * stepX;
    const clamped = Math.min(max, Math.max(min, value));
    const y = height - ((clamped - min) / span) * height;
    return [Math.round(x * 100) / 100, Math.round(y * 100) / 100] as const;
  });
  const line = points.map(([x, y]) => `${x},${y}`).join(" ");
  const area = `0,${height} ${line} ${width},${height}`;
  return (
    <svg
      className={cx("sparkline", className)}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={label}
    >
      {fill && <polygon className="sparkline__area" points={area} />}
      <polyline className="sparkline__line" points={line} />
    </svg>
  );
}
