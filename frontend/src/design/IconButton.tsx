import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";
import { cx } from "../utils/cx";
import "./icon-button.css";

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Accessible label — icon-only buttons must name their action. */
  label: string;
  danger?: boolean;
  children?: ReactNode;
  ref?: Ref<HTMLButtonElement>;
}

/** 28px square ghost button for icon actions (⋯ menus, back arrows, close). */
export function IconButton({ label, danger, children, className, type = "button", ref, ...rest }: IconButtonProps) {
  return (
    <button
      ref={ref}
      type={type}
      className={cx("icon-btn", danger && "icon-btn--danger", className)}
      aria-label={label}
      title={label}
      {...rest}
    >
      {children}
    </button>
  );
}
