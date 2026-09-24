import type { TestResult } from "../api/types";
import { formatAgo, formatDuration, testStatusTone } from "../lib/format";
import { useHub } from "../state/hub";
import { ErrorNote } from "../ui/common";

function Status({ r }: { r: TestResult | undefined }) {
  const t = testStatusTone(r?.status);
  return (
    <div>
      <span className={t.tone ? `${t.tone}-t` : "mute-t"}>{t.label}</span>
      {r && r.duration_ms > 0 && <span className="mute-t small"> · {formatDuration(r.duration_ms)}</span>}
      {r?.failed_step !== null && r?.failed_step !== undefined && <div className="crit-t small">step {r.failed_step + 1} failed</div>}
    </div>
  );
}

/** Latest acceptance test results, one row per test with the simulation and live results side by side. */
export function TestsPane() {
  const { tests } = useHub();
  if (tests.error && !tests.data) return <ErrorNote error={tests.error} />;
  if (!tests.data) return <p className="mute-t">Loading test results…</p>;
  const byName = new Map<string, { sim?: TestResult; live?: TestResult }>();
  for (const r of tests.data.results) {
    const row = byName.get(r.name) ?? {};
    row[r.target] = r;
    byName.set(r.name, row);
  }
  if (byName.size === 0) return <p className="mute-t">No acceptance tests yet. The manifest's tests appear here once they run.</p>;
  return (
    <div className="tests" role="table" aria-label="Acceptance tests">
      <div className="thdr" role="row">
        <span role="columnheader">Acceptance test</span>
        <span role="columnheader">Simulation</span>
        <span role="columnheader">Live site</span>
      </div>
      {[...byName.entries()].map(([name, row]) => {
        const detail = row.live?.detail || row.sim?.detail;
        return (
          <div key={name} className="trow" role="row">
            <div role="rowheader">
              <div>{name}</div>
              {detail && <div className="steps">{detail}</div>}
            </div>
            <div role="cell" data-col="sim">
              <Status r={row.sim} />
            </div>
            <div role="cell" data-col="live">
              <Status r={row.live} />
            </div>
          </div>
        );
      })}
      <p className="mute-t small">Updated {formatAgo(tests.data.updated_at)}.</p>
    </div>
  );
}
