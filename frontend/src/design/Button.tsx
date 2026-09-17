import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";
import "./button.css";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost";
  children: ReactNode;
}

/**
 * Text button. Primary uses --accent (reserved for primary actions); the
 * destructive verb goes in the label, never in a generic "OK".
 */
export function Button({ variant = "ghost", children, className, type = "button", ...rest }: ButtonProps) {
  return (
    <button type={type} className={cx("btn", `btn--${variant}`, className)} {...rest}>
      {children}
    </button>
  );
}
