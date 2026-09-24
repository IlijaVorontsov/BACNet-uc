/**
 * Mock mode: routes the app's `/api/*` requests to an in-browser `MockServer`
 * by wrapping `window.fetch`. Everything else (fonts, assets) still goes to
 * the network. Enabled with `?mock=1` or `VITE_MOCK=1` (see src/main.tsx).
 */

import { MockServer, type MockServerOptions } from "./server";

export function installMock(opts: MockServerOptions = {}, target: Window = window): MockServer {
  const server = new MockServer(opts);
  const realFetch = target.fetch.bind(target);
  const mockFetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(input instanceof Request ? input.url : String(input), target.location.href);
    if (url.origin !== target.location.origin || !url.pathname.startsWith("/api/")) return realFetch(input, init);
    return server.fetch(input instanceof Request ? new Request(input, init) : new Request(url, init));
  };
  target.fetch = mockFetch;
  return server;
}
