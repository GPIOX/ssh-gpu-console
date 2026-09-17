import type { ReactNode } from "react";
import { cx } from "../utils/cx";
import "./empty-state.css";

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  hint?: string;
  /** Optional action affordance (e.g. a Button). */
  action?: ReactNode;
  className?: string;
}

/** Centered empty state: icon, title, optional hint and action. */
export function EmptyState({ icon, title, hint, action, className }: EmptyStateProps) {
  return (
    <div className={cx("empty-state", className)}>
      {icon !== undefined && <div className="empty-state__icon">{icon}</div>}
      <p className="empty-state__title">{title}</p>
      {hint !== undefined && <p className="empty-state__hint">{hint}</p>}
      {action !== undefined && <div className="empty-state__action">{action}</div>}
    </div>
  );
}
