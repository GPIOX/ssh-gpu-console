import type { ReactNode } from "react";
import { cx } from "../utils/cx";
import "./chip.css";

export type ChipTone = "neutral" | "ok" | "warn" | "crit" | "accent" | "cold";

export interface ChipProps {
  tone?: ChipTone;
  /** Render contents in mono (identifiers, values). */
  mono?: boolean;
  children: ReactNode;
  className?: string;
  title?: string;
}

/** Compact inline chip: micro text on surface-2 with a hairline border. */
export function Chip({ tone = "neutral", mono, children, className, title }: ChipProps) {
  return (
    <span className={cx("chip", `chip--${tone}`, mono && "chip--mono", className)} title={title}>
      {children}
    </span>
  );
}
