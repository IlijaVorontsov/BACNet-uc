import { Fragment, useEffect, useMemo, useState } from "react";
import type { Point, PointsPage } from "../api/types";
import { formatReading } from "../lib/format";
import { useHub, useResource } from "../state/hub";
import { useLive, type LiveValue } from "../state/streams";
import type { Scope } from "../state/ui";
import { Chip, ErrorNote } from "../ui/common";

const LIMIT = 200;

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function ValueCell({ point, live }: { point: Point; live: LiveValue | undefined }) {
  const r = live?.reading;
  const q = r?.quality ?? "good";
  return (
    <td className={`v q-${q}`} title={r ? `${q}${r.error ? `: ${r.error}` : ""} · ${new Date(r.ts * 1000).toLocaleTimeString()}` : undefined}>
      <span key={live?.changedAt ?? 0} className={live && live.changedAt > 0 ? "flash" : undefined}>
        {formatReading(r, point)}
      </span>
      {q !== "good" && <span className="qual">{q}</span>}
    </td>
  );
}

/** Brick class names are long; allow line breaks after underscores. */
function Tag({ text }: { text: string }) {
  const parts = text.split("_");
  return (
    <span className="tag">
      {parts.map((part, i) => (
        <Fragment key={i}>
          {i > 0 && (
            <>
              _<wbr />
            </>
          )}
          {part}
        </Fragment>
      ))}
    </span>
  );
}

function sourceText(source: string): { kind: string; rest: string } {
  const i = source.indexOf(":");
  return i > 0 ? { kind: source.slice(0, i), rest: source.slice(i + 1) } : { kind: source, rest: "" };
}

/** Point table of the selected scope, filtered by the top-bar search, with live values. */
export function PointsPane({ scope, query }: { scope: Scope; query: string }) {
  const hub = useHub();
  const q = useDebounced(query.trim(), 200);
  const siteData = hub.site.data;
  const params = useMemo(
    () => ({
      q: q || undefined,
      device: scope.kind === "device" ? scope.name : undefined,
      space: scope.kind === "space" ? scope.id : undefined,
      limit: LIMIT,
    }),
    [q, scope],
  );
  const page = useResource<PointsPage>((s) => hub.client.points(params, s), [hub.client, params, siteData]);
  const points = page.data?.points ?? [];
  const ids = useMemo(() => points.map((p) => p.id), [points]);
  const { values } = useLive(scope.kind === "device" && !q ? { device: scope.name } : { ids });
  const showDevice = scope.kind !== "device";

  const merged = (p: Point & { reading?: LiveValue["reading"] }): LiveValue | undefined =>
    values.get(p.id) ?? (p.reading ? { reading: p.reading, changedAt: 0 } : undefined);

  return (
    <div className="points">
      <ErrorNote error={page.error} />
      {page.data && (
        <p className="count mute-t small" aria-live="polite">
          {page.data.total === 0
            ? q
              ? `No points match “${q}”.`
              : "No points here yet."
            : page.data.total > points.length
              ? `Showing ${points.length} of ${page.data.total} points. Narrow the search to see the rest.`
              : `${page.data.total} points`}
        </p>
      )}
      {points.length > 0 && (
        <div className="tablewrap">
          <table className="pts">
            <thead>
              <tr>
                <th scope="col">Point</th>
                <th scope="col" className="r">
                  Value
                </th>
                <th scope="col" className="r">
                  Priority
                </th>
                <th scope="col" className="col-tags">
                  Tags
                </th>
                <th scope="col" className="col-src">
                  Source
                </th>
              </tr>
            </thead>
            <tbody>
              {points.map((p) => {
                const live = merged(p);
                const src = sourceText(p.source);
                return (
                  <tr key={p.id} data-point={p.id}>
                    <th scope="row" className="pt">
                      <span className="pname">
                        {p.name}
                        {p.safety !== "normal" && (
                          <Chip tone={p.safety === "life-safety" ? "crit" : "warn"} className="mini">
                            {p.safety}
                          </Chip>
                        )}
                      </span>
                      <span className="pid">
                        {showDevice && `${p.device} · `}
                        {p.obj}
                        {p.source && <span className="src-inline"> · {p.source}</span>}
                      </span>
                    </th>
                    <ValueCell point={p} live={live} />
                    <td className="prio">{live?.reading.priority !== undefined ? `@${live.reading.priority}` : p.commandable ? "–" : ""}</td>
                    <td className="col-tags">
                      {p.tags.map((t) => (
                        <Tag key={t} text={t} />
                      ))}
                    </td>
                    <td className="src col-src" title={p.source}>
                      {src.kind} {src.rest && <b>{src.rest}</b>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
