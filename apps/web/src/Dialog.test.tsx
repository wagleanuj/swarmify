import { useState } from "react";
import { expect, test } from "vitest";
import { userEvent } from "vitest/browser";
import { render } from "vitest-browser-react";

import "./index.css";

import { useConfirm } from "./Dialog";

function Harness({ danger = true }: { danger?: boolean }) {
  const [confirm, dialog] = useConfirm();
  const [said, setSaid] = useState("");
  return (
    <>
      <button
        type="button"
        onClick={async () => setSaid(String(await confirm({ title: "Abort Weekly?", lines: ["Every agent stops."], action: "Abort run", danger })))}
      >
        Ask
      </button>
      <output>{said}</output>
      {dialog}
    </>
  );
}

test("a confirm opens modal with its title and lines, focus on Cancel, the action in danger red", async () => {
  const screen = await render(<Harness />);
  await screen.getByRole("button", { name: "Ask" }).click();
  const dialog = screen.getByRole("dialog", { name: "Abort Weekly?" });
  await expect.element(dialog).toBeVisible();
  await expect.element(dialog).toHaveAccessibleDescription("Every agent stops.");
  expect((dialog.element() as HTMLDialogElement).matches(":modal")).toBe(true);
  await expect.element(dialog.getByRole("button", { name: "Cancel" })).toHaveFocus();
  const action = dialog.getByRole("button", { name: "Abort run" });
  await expect.element(action).toHaveClass("danger");
  expect(getComputedStyle(action.element()).color).toBe("rgb(240, 122, 142)"); // --pill-negative-ink
  await action.click();
  await expect.element(screen.getByRole("status")).toHaveTextContent("true");
  await expect.element(dialog).not.toBeInTheDocument();
});

test("Escape cancels, and so does Cancel", async () => {
  const screen = await render(<Harness danger={false} />);
  await screen.getByRole("button", { name: "Ask" }).click();
  const dialog = screen.getByRole("dialog");
  await expect.element(dialog.getByRole("button", { name: "Abort run" })).toHaveClass("primary");
  await userEvent.keyboard("{Escape}");
  await expect.element(dialog).not.toBeInTheDocument();
  await expect.element(screen.getByRole("status")).toHaveTextContent("false");
  await screen.getByRole("button", { name: "Ask" }).click();
  await screen.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
  await expect.element(screen.getByRole("dialog")).not.toBeInTheDocument();
});
