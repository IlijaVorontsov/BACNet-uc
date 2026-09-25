import { useHub } from "../state/hub";
import { Chip } from "./common";

/** Gateway reachability, device counts and mode, shown in the top bar and the phone header. */
export function HealthChips({ compact = false }: { compact?: boolean }) {
  const hub = useHub();
  const site = hub.site.data;
  const offline = site ? site.summary.devices - site.summary.online : 0;
  // The error stays until a health check succeeds again, also while a retry is in flight.
  const down = hub.health.error !== null;
  const unauthorized = hub.me.error?.status === 401 || hub.health.error?.status === 401;

  const status = unauthorized ? (
    <Chip tone="crit" dot title="Open the sign-in link with ?token= that your hub admin gave you.">
      Sign-in needed
    </Chip>
  ) : down ? (
    <Chip tone="crit" dot title={hub.health.error?.message}>
      {compact ? "Offline" : "Gateway unreachable"}
    </Chip>
  ) : null;
  // The phone header has room for one chip: the most important one.
  if (compact) {
    if (status) return status;
    if (offline > 0) return <Chip tone="warn" dot>{offline} offline</Chip>;
    if (hub.mock) return <Chip tone="acc">Mock</Chip>;
    return hub.health.data ? <Chip tone="ok" dot>Online</Chip> : null;
  }
  return (
    <>
      {status ??
        (hub.health.data && (
          <Chip tone="ok" dot>
            Gateway online
          </Chip>
        ))}
      {site && <Chip className="secondary">{site.summary.devices} devices</Chip>}
      {offline > 0 && (
        <Chip tone="warn" dot>
          {offline} offline
        </Chip>
      )}
      {hub.health.data && !hub.health.data.llm.configured && <Chip tone="warn">No LLM</Chip>}
      {hub.mock && (
        <Chip tone="acc" className="secondary">
          Mock data
        </Chip>
      )}
    </>
  );
}
