import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Testing Library only auto-cleans with test globals, which this project does not enable.
afterEach(cleanup);

// jsdom has no PointerEvent; without it pointer events lose `button` and `pointerId`.
if (typeof window !== "undefined" && typeof window.PointerEvent === "undefined") {
  class PointerEventShim extends MouseEvent {
    readonly pointerId: number;
    readonly pointerType: string;

    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init);
      this.pointerId = init.pointerId ?? 1;
      this.pointerType = init.pointerType ?? "mouse";
    }
  }
  window.PointerEvent = PointerEventShim as unknown as typeof PointerEvent;
}
