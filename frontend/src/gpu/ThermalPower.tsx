import { cx } from "../utils/cx";
import { formatPower, formatTemperature } from "../utils/format";
import "./thermal-power.css";

const HOT_WARN_C = 75;
const HOT_CRIT_C = 85;
const COLD_C = 40;

export type ThermalTone = "cold" | "neutral" | "warn" | "crit" | "empty";

export function thermalToneFor(celsius: number | null): ThermalTone {
  if (celsius === null) return "empty";
  if (celsius >= HOT_CRIT_C) return "crit";
  if (celsius >= HOT_WARN_C) return "warn";
  if (celsius <= COLD_C) return "cold";
  return "neutral";
}

export interface ThermalPowerProps {
  temperatureC: number | null;
  powerWatts: number | null;
  powerLimitWatts: number | null;
  className?: string;
}

/** Color-coded temp + mono power draw ("64° 210/350W"). */
export function ThermalPower({ temperatureC, powerWatts, powerLimitWatts, className }: ThermalPowerProps) {
  const tone = thermalToneFor(temperatureC);
  return (
    <span className={cx("thermal", className)}>
      <span className={cx("thermal__temp", `thermal__temp--${tone}`)}>
        {formatTemperature(temperatureC)}
      </span>
      <span className="thermal__power tnum">{formatPower(powerWatts, powerLimitWatts)}</span>
    </span>
  );
}
