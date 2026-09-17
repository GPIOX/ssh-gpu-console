import { cx } from "../utils/cx";
import "./spinner.css";

export interface SpinnerProps {
  size?: number;
  className?: string;
  label?: string;
}

/** Subtle loading spinner (12px default, hairline ring + rotating arc). */
export function Spinner({ size = 12, className, label = "Loading" }: SpinnerProps) {
  return (
    <span
      className={cx("spinner", className)}
      style={{ width: size, height: size }}
      role="status"
      aria-label={label}
    />
  );
}
