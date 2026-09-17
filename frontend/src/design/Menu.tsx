import { DotsThree } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { cx } from "../utils/cx";
import { IconButton } from "./IconButton";
import "./menu.css";

export interface MenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  danger?: boolean;
  disabled?: boolean;
  onSelect: () => void;
}

export interface MenuProps {
  items: MenuItem[];
  /** Accessible label for the ⋯ trigger. */
  triggerLabel?: string;
  triggerIcon?: ReactNode;
  align?: "left" | "right";
  className?: string;
}

/**
 * ⋯ overflow menu. Row actions live here — never as permanent CRUD buttons.
 * Closes on outside click and Escape; returns focus to the trigger.
 */
export function Menu({ items, triggerLabel = "More actions", triggerIcon, align = "right", className }: MenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div className={cx("menu", className)} ref={rootRef}>
      <IconButton
        ref={triggerRef}
        label={triggerLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {triggerIcon ?? <DotsThree size={16} weight="bold" />}
      </IconButton>
      {open && (
        <div className={cx("menu__pop", `menu__pop--${align}`)} role="menu">
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              role="menuitem"
              className={cx("menu__item", item.danger && "menu__item--danger")}
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.icon !== undefined && <span className="menu__item-icon">{item.icon}</span>}
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
