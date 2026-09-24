import { act, fireEvent, render, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { HoldButton } from "./HoldButton";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

function setup(props: Partial<Parameters<typeof HoldButton>[0]> = {}) {
  const onConfirm = vi.fn();
  const { container } = render(<HoldButton label="Hold to apply" onConfirm={onConfirm} {...props} />);
  const button = within(container).getByRole<HTMLButtonElement>("button");
  return { onConfirm, button, container };
}

describe("HoldButton", () => {
  it("confirms only after the full hold", () => {
    const { onConfirm, button } = setup();
    fireEvent.pointerDown(button, { button: 0, pointerId: 1 });
    expect(button.textContent).toBe("Keep holding…");
    act(() => vi.advanceTimersByTime(1400));
    expect(onConfirm).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(150));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(button.disabled).toBe(true);
    fireEvent.pointerUp(button);
    act(() => vi.advanceTimersByTime(5000));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("cancels when released early and explains why", () => {
    const { onConfirm, button, container } = setup();
    fireEvent.pointerDown(button, { button: 0, pointerId: 1 });
    act(() => vi.advanceTimersByTime(600));
    fireEvent.pointerUp(button);
    act(() => vi.advanceTimersByTime(3000));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(button.textContent).toBe("Hold to apply");
    expect(container.querySelector(".holdhint")?.textContent).toMatch(/A short tap does nothing/);
  });

  it("ignores clicks and secondary buttons", () => {
    const { onConfirm, button } = setup();
    fireEvent.click(button);
    fireEvent.pointerDown(button, { button: 2, pointerId: 1 });
    act(() => vi.advanceTimersByTime(3000));
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("works with a held Space or Enter key, and key repeat does not restart the hold", () => {
    const { onConfirm, button } = setup({ durationMs: 1000 });
    fireEvent.keyDown(button, { key: " " });
    act(() => vi.advanceTimersByTime(500));
    fireEvent.keyDown(button, { key: " ", repeat: true });
    act(() => vi.advanceTimersByTime(500));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    const second = setup({ durationMs: 1000 });
    fireEvent.keyDown(second.button, { key: "Enter" });
    act(() => vi.advanceTimersByTime(400));
    fireEvent.keyUp(second.button, { key: "Enter" });
    act(() => vi.advanceTimersByTime(2000));
    expect(second.onConfirm).not.toHaveBeenCalled();
  });

  it("cancels when focus leaves and does nothing while disabled", () => {
    const { onConfirm, button } = setup();
    fireEvent.keyDown(button, { key: "Enter" });
    fireEvent.blur(button);
    act(() => vi.advanceTimersByTime(3000));
    expect(onConfirm).not.toHaveBeenCalled();

    const disabled = setup({ disabled: true });
    fireEvent.pointerDown(disabled.button, { button: 0, pointerId: 1 });
    act(() => vi.advanceTimersByTime(3000));
    expect(disabled.onConfirm).not.toHaveBeenCalled();
  });
});
