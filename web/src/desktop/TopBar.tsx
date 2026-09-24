import { useEffect, useRef } from "react";
import { initials } from "../lib/format";
import { useHub } from "../state/hub";
import { useUi } from "../state/ui";
import { Icon } from "../ui/common";
import { HealthChips } from "../ui/HealthChips";

export function TopBar() {
  const hub = useHub();
  const ui = useUi();
  const search = useRef<HTMLInputElement>(null);
  const me = hub.me.data;

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        search.current?.focus();
        search.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <header className="topbar">
      <div className="brand">
        <i aria-hidden="true" />
        uc-hub
      </div>
      <span className="sitepick" title={hub.site.data?.description}>
        {(hub.site.data?.name ?? hub.health.data?.site ?? "…").toUpperCase()}
      </span>
      <label className="search">
        <Icon name="search" size={15} />
        <input
          ref={search}
          type="search"
          value={ui.query}
          placeholder="Search points, devices, tags"
          aria-label="Search points"
          onChange={(e) => {
            ui.setQuery(e.target.value);
            ui.setWorkTab("points");
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") ui.setQuery("");
          }}
        />
        <kbd aria-hidden="true">Ctrl K</kbd>
      </label>
      <div className="health">
        <HealthChips />
      </div>
      {me && (
        <div className="avatar" role="img" aria-label={`Signed in as ${me.user} (${me.roles.join(", ")})`} title={`${me.user} · ${me.roles.join(", ")}`}>
          {initials(me.user)}
        </div>
      )}
    </header>
  );
}
