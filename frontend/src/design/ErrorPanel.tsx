import { cx } from "../utils/cx";
import { Button } from "./Button";
import "./error-panel.css";

export interface ErrorPanelProps {
  title: string;
  detail?: string | null;
  onRetry?: () => void;
  retryLabel?: string;
  className?: string;
}

/** Explicit error surface: title, detail (mono), retry. Covers auth/host-key/offline. */
export function ErrorPanel({ title, detail, onRetry, retryLabel = "Retry", className }: ErrorPanelProps) {
  return (
    <div className={cx("error-panel", className)} role="alert">
      <p className="error-panel__title">{title}</p>
      {detail !== undefined && detail !== null && detail !== "" && (
        <p className="error-panel__detail mono">{detail}</p>
      )}
      {onRetry !== undefined && (
        <div className="error-panel__actions">
          <Button variant="ghost" onClick={onRetry}>
            {retryLabel}
          </Button>
        </div>
      )}
    </div>
  );
}
