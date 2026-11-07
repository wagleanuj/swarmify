import { useState } from "react";
import { expect, test } from "vitest";
import { render } from "vitest-browser-react";

import "./index.css";

import { Sheet } from "./Sheet";

function Harness() {
  const [open, setOpen] = useState(true);
  return (
    <Sheet open={open} onClose={() => setOpen(false)} label="Pick">
      <button type="button" onClick={() => setOpen(false)}>
        Done
      </button>
    </Sheet>
  );
}

test("a closing sheet keeps its body only while it slides out, and that body is inert", async () => {
  const screen = await render(<Harness />);
  const done = screen.getByRole("button", { name: "Done" });
  await done.click();
  const body = screen.container.querySelector(".sheet-body")!;
  expect(body.hasAttribute("inert")).toBe(true);
  await expect.poll(() => body.childElementCount).toBe(0);
});
