import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ApiClient, initAuthToken } from "./api/client";
import { App } from "./App";
import { registerServiceWorker } from "./pwa";
import "./styles/tokens.css";
import "./styles/app.css";

const MOCK_KEY = "uc-hub.mock";

/** `?mock=1` turns mock mode on for this tab (it survives reloads), `?mock=0` off; VITE_MOCK=1 forces it. */
function mockRequested(): boolean {
  if (import.meta.env.VITE_MOCK === "1") return true;
  const param = new URLSearchParams(window.location.search).get("mock");
  try {
    if (param === "1") sessionStorage.setItem(MOCK_KEY, "1");
    else if (param === "0") sessionStorage.removeItem(MOCK_KEY);
    return sessionStorage.getItem(MOCK_KEY) === "1";
  } catch {
    return param === "1";
  }
}

async function boot(): Promise<void> {
  const mock = mockRequested();
  if (mock) {
    const { installMock } = await import("./api/mock/install");
    const speed = Number(new URLSearchParams(window.location.search).get("mockSpeed"));
    installMock({ speed: Number.isFinite(speed) && speed > 0 ? speed : 1 });
  }
  const client = new ApiClient({ token: mock ? null : initAuthToken() });
  const root = document.getElementById("root");
  if (!root) throw new Error("index.html has no #root element");
  createRoot(root).render(
    <StrictMode>
      <App client={client} mock={mock} />
    </StrictMode>,
  );
  if (import.meta.env.PROD) registerServiceWorker();
}

void boot();
