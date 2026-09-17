import type { MouseEventHandler, ReactNode } from "react";
import { cx } from "../utils/cx";
import "./panel.css";

export interface PanelProps {
  children: ReactNode;
  className?: string;
  /** Hover-raises the surface one step (machine panels); semantics stay with the caller. */
  interactive?: boolean;
  onClick?: MouseEventHandler<HTMLDivElement>;
}

/** Raised surface: adjacent surface value + hairline, radius 10px, no shadow. */
export function Panel({ children, className, interactive, onClick }: PanelProps) {
  return (
    <div className={cx("panel", interactive && "panel--interactive", className)} onClick={onClick}>
      {children}
    </div>
  );
}
