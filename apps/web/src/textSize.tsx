/** Text size, per device (Settings, This device). Zoom is off so the app feels native, so this
 * is the reading aid instead: it scales the root font size, and every size in the app is rem. */
import { useState } from "react";

export const TEXT_SIZES = [
  ["default", "Default"],
  ["large", "Large"],
  ["larger", "Larger"],
] as const;
export type TextSize = (typeof TEXT_SIZES)[number][0];
const KEY = "swarmify.textSize";

export function textSize(): TextSize {
  try {
    const saved = localStorage.getItem(KEY);
    return saved === "large" || saved === "larger" ? saved : "default";
  } catch {
    return "default";
  }
}

/** Put the saved size on <html>, where base.css turns it into --text-scale. */
export function applyTextSize(size: TextSize = textSize()) {
  const root = document.documentElement;
  if (size === "default") delete root.dataset.textSize;
  else root.dataset.textSize = size;
}

export function setTextSize(size: TextSize) {
  try {
    if (size === "default") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, size);
  } catch {
    // Private mode: the size holds until the page closes.
  }
  applyTextSize(size);
}

export function TextSizePicker() {
  const [size, setSize] = useState(textSize);
  return (
    <fieldset className="segments">
      <legend>Text size</legend>
      {TEXT_SIZES.map(([value, label]) => (
        <label key={value}>
          <input
            type="radio"
            name="text-size"
            value={value}
            checked={size === value}
            onChange={() => {
              setTextSize(value);
              setSize(value);
            }}
          />
          <span>{label}</span>
        </label>
      ))}
    </fieldset>
  );
}
