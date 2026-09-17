import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cx } from "../utils/cx";
import { IconButton } from "./IconButton";
import "./dialog.css";

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
}

function trapTab(event: KeyboardEvent, root: HTMLElement | null): void {
  if (root === null) return;
  const items = focusables(root);
  if (items.length === 0) {
    event.preventDefault();
    return;
  }
  const first = items[0];
  const last = items[items.length - 1];
  const active = document.activeElement;
  if (event.shiftKey) {
    if (active === first || !(active instanceof Node) || !root.contains(active)) {
      event.preventDefault();
      last.focus();
    }
  } else if (active === last || !(active instanceof Node) || !root.contains(active)) {
    event.preventDefault();
    first.focus();
  }
}

function isTextEntry(element: HTMLElement): boolean {
  if (element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) return true;
  if (element instanceof HTMLInputElement) {
    const type = element.type;
    return type !== "checkbox" && type !== "radio" && type !== "button" && type !== "submit" && type !== "hidden";
  }
  return false;
}

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  /** Max content width in px. */
  width?: number;
  className?: string;
}

/** Modal dialog: focus trap, Escape close, overlay click close. */
export function Dialog({ open, onClose, title, children, width = 460, className }: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);

  // Hosts re-render while the dialog is open (clock ticks, store updates) and
  // pass a fresh inline closure each time; the focus effect below must key on
  // `open` alone or every re-render would yank focus back out of the form
  // fields to the panel header.
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const raf = requestAnimationFrame(() => {
      const panel = panelRef.current;
      if (panel === null) return;
      const items = focusables(panel);
      const target = items.find(isTextEntry) ?? items[0];
      target?.focus();
    });
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onCloseRef.current();
      } else if (event.key === "Tab") {
        trapTab(event, panelRef.current);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKeyDown);
      restoreFocusRef.current?.focus();
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div
      className="dialog-overlay"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={cx("dialog", className)}
        style={{ maxWidth: width }}
      >
        {title !== undefined && (
          <header className="dialog__head">
            <h2 className="dialog__title">{title}</h2>
            <IconButton label="Close" onClick={onClose}>
              <XIcon />
            </IconButton>
          </header>
        )}
        <div className="dialog__body">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

function XIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}
