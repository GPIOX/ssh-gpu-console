import type { ReactNode } from "react";
import { cx } from "../utils/cx";
import "./section.css";

export interface SectionProps {
  /** Micro-label section title (10.5px / 600 / uppercase / tracked). */
  title: string;
  /** Right-aligned meta (mono), e.g. freshness or counts. */
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Section({ title, meta, children, className }: SectionProps) {
  return (
    <section className={cx("section", className)}>
      <header className="section__head">
        <h2 className="section__title micro-label">{title}</h2>
        {meta !== undefined && <div className="section__meta mono tnum">{meta}</div>}
      </header>
      <div className="section__body">{children}</div>
    </section>
  );
}
