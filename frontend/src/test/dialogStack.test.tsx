/**
 * Dialog modal stack: nested dialogs (a dialog opening another dialog) each
 * register a document keydown listener, so Escape / Tab / overlay clicks must
 * only act on the top-most open dialog, and focus restore must walk back down
 * the stack (child → its opener, parent → the original page trigger).
 */

import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { Dialog } from "../design";

/** Parent dialog opens a child dialog — the NewTransferDialog pattern. */
function NestedDialogsHarness() {
  const [parentOpen, setParentOpen] = useState(false);
  const [childOpen, setChildOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setParentOpen(true)}>Open parent</button>
      <Dialog open={parentOpen} onClose={() => setParentOpen(false)} title="Parent dialog">
        <input aria-label="Parent field" />
        <button onClick={() => setChildOpen(true)}>Open child</button>
      </Dialog>
      <Dialog open={childOpen} onClose={() => setChildOpen(false)} title="Child dialog">
        <input aria-label="Child field" />
        <button onClick={() => setChildOpen(false)}>Child action</button>
      </Dialog>
    </div>
  );
}

async function openParentDialog(): Promise<void> {
  fireEvent.click(screen.getByRole("button", { name: "Open parent" }));
  await screen.findByRole("dialog", { name: "Parent dialog" });
}

/** Opens parent, then child; settles after each dialog's rAF initial focus. */
async function openNestedDialogs(): Promise<void> {
  await openParentDialog();
  await waitFor(() => {
    expect(document.activeElement).toBe(screen.getByLabelText("Parent field"));
  });
  const opener = screen.getByRole("button", { name: "Open child" });
  opener.focus(); // a real click focuses the opener before the click lands
  fireEvent.click(opener);
  await waitFor(() => {
    expect(document.activeElement).toBe(screen.getByLabelText("Child field"));
  });
}

describe("Dialog modal stack", () => {
  it("single dialog: Escape closes it", async () => {
    render(<NestedDialogsHarness />);
    await openParentDialog();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Parent dialog" })).toBeNull();
  });

  it("nested: first Escape closes ONLY the child; parent stays open", async () => {
    render(<NestedDialogsHarness />);
    await openNestedDialogs();

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Child dialog" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Parent dialog" })).toBeTruthy();
  });

  it("nested: second Escape then closes the parent", async () => {
    render(<NestedDialogsHarness />);
    await openNestedDialogs();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "Parent dialog" })).toBeTruthy();
    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.queryByRole("dialog", { name: "Child dialog" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Parent dialog" })).toBeNull();
  });

  it("nested: Tab wraps within the child's focusables only", async () => {
    render(<NestedDialogsHarness />);
    await openNestedDialogs();
    const child = screen.getByRole("dialog", { name: "Child dialog" });
    within(child).getByRole("button", { name: "Child action" }).focus();

    fireEvent.keyDown(document, { key: "Tab" });

    // From the last child focusable, Tab wraps back to the child's first one.
    expect(document.activeElement).toBe(within(child).getByLabelText("Close"));
    expect(child.contains(document.activeElement)).toBe(true);
    const parent = screen.getByRole("dialog", { name: "Parent dialog" });
    expect(parent.contains(document.activeElement)).toBe(false);
  });

  it("nested: Shift+Tab wraps within the child's focusables only", async () => {
    render(<NestedDialogsHarness />);
    await openNestedDialogs();
    const child = screen.getByRole("dialog", { name: "Child dialog" });
    within(child).getByLabelText("Close").focus();

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });

    // From the first child focusable, Shift+Tab wraps to the child's last one.
    expect(document.activeElement).toBe(
      within(child).getByRole("button", { name: "Child action" }),
    );
    expect(child.contains(document.activeElement)).toBe(true);
    const parent = screen.getByRole("dialog", { name: "Parent dialog" });
    expect(parent.contains(document.activeElement)).toBe(false);
  });

  it("focus restore walks back down the stack: child → opener, parent → trigger", async () => {
    render(<NestedDialogsHarness />);
    const trigger = screen.getByRole("button", { name: "Open parent" });
    trigger.focus(); // a real click leaves the trigger focused
    fireEvent.click(trigger);
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText("Parent field"));
    });
    const opener = screen.getByRole("button", { name: "Open child" });
    opener.focus();
    fireEvent.click(opener);
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText("Child field"));
    });

    fireEvent.keyDown(document, { key: "Escape" }); // child closes
    expect(document.activeElement).toBe(opener);

    fireEvent.keyDown(document, { key: "Escape" }); // parent closes
    expect(screen.queryByRole("dialog", { name: "Parent dialog" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });
});
