/** Registers public/sw.js, which caches the app shell (never /api). Production builds only. */
export function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) return;
  const register = (): void => {
    navigator.serviceWorker
      .register(`${import.meta.env.BASE_URL}sw.js`, { scope: import.meta.env.BASE_URL })
      .catch((err: unknown) => console.warn("service worker registration failed", err));
  };
  if (document.readyState === "complete") register();
  else window.addEventListener("load", register, { once: true });
}
