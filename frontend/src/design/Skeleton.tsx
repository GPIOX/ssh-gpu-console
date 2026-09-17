import { cx } from "../utils/cx";
import "./skeleton.css";

export interface SkeletonProps {
  width?: number | string;
  height?: number | string;
  radius?: string;
  className?: string;
}

/** Shape-matched loading placeholder. Static surface block — no shimmer loop. */
export function Skeleton({ width = "100%", height = 12, radius = "4px", className }: SkeletonProps) {
  return (
    <span
      className={cx("skeleton", className)}
      style={{ width, height, borderRadius: radius }}
      aria-hidden="true"
    />
  );
}
