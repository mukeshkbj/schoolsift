"use client";

import { useMemo, useState } from "react";
import type { SchoolSource } from "../lib/contracts";

const PAGE = 50;
const COLLAPSE_AT = 5;

const FILTERS = [
  { key: "suggested", label: "Suggested" },
  { key: "confirmed", label: "Trusted" },
  { key: "rejected", label: "Ignored" },
] as const;
type Filter = (typeof FILTERS)[number]["key"];

export function relativeDate(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const days = Math.floor((Date.now() - then) / 86_400_000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days}d ago`;
  if (days < 30) return `${Math.floor(days / 7)}w ago`;
  return new Date(then).toLocaleDateString();
}

type DomainGroup = {
  domain: string;
  rows: SchoolSource[];
  total: number;
  pendingIds: string[];
  collapsible: boolean;
  collapsed: boolean;
};

type Props = {
  sources: SchoolSource[];
  canEdit: boolean;
  busyKey: string | null;
  onSource: (id: string, action: "confirm" | "reject") => Promise<void>;
  onBulkSources: (
    action: "confirm" | "reject",
    ids: string[],
    onProgress: (done: number) => void,
  ) => Promise<void>;
};

export default function SendersView({
  sources,
  canEdit,
  busyKey,
  onSource,
  onBulkSources,
}: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("suggested");
  const [visible, setVisible] = useState(PAGE);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [bulk, setBulk] = useState<{ done: number; total: number } | null>(
    null,
  );

  const counts = useMemo(() => {
    const c: Record<Filter, number> = {
      suggested: 0,
      confirmed: 0,
      rejected: 0,
    };
    for (const s of sources) c[s.status] += 1;
    return c;
  }, [sources]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const matched = sources.filter(
      (s) =>
        s.status === filter &&
        (q === "" ||
          s.sender_email.toLowerCase().includes(q) ||
          s.sender_domain.toLowerCase().includes(q)),
    );
    const byDomain = new Map<string, SchoolSource[]>();
    for (const s of matched) {
      const list = byDomain.get(s.sender_domain) ?? [];
      list.push(s);
      byDomain.set(s.sender_domain, list);
    }
    const all = [...byDomain.entries()].sort(
      (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]),
    );
    let remaining = visible;
    let hidden = 0;
    const shown: DomainGroup[] = [];
    for (const [domain, items] of all) {
      const forced = expanded.has(domain);
      const collapsible = items.length > COLLAPSE_AT;
      const collapsed = collapsible && !forced;
      let rows: SchoolSource[];
      if (collapsed) {
        rows = [];
      } else if (forced) {
        rows = items;
      } else {
        rows = items.slice(0, Math.max(0, remaining));
        remaining -= rows.length;
        hidden += items.length - rows.length;
        if (rows.length === 0) continue;
      }
      shown.push({
        domain,
        rows,
        total: items.length,
        pendingIds: items
          .filter((s) => s.status === "suggested")
          .map((s) => s.id),
        collapsible,
        collapsed,
      });
    }
    return { shown, hidden };
  }, [sources, query, filter, visible, expanded]);

  const toggleDomain = (domain: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(domain)) next.delete(domain);
      else next.add(domain);
      return next;
    });

  const runBulk = async (
    action: "confirm" | "reject",
    ids: string[],
  ) => {
    setBulk({ done: 0, total: ids.length });
    try {
      await onBulkSources(action, ids, (done) =>
        setBulk({ done, total: ids.length }),
      );
    } finally {
      setBulk(null);
    }
  };

  return (
    <div className="senders-pane">
      <h2 className="rail-heading">Senders</h2>
      <p className="connection-empty">
        SchoolSift reads full message content only for senders you trust.
        Everything else stays headers-only.
      </p>
      <div className="senders-toolbar">
        <label className="field senders-search">
          <span className="field-label">Search senders</span>
          <input
            type="search"
            value={query}
            placeholder="Address or domain"
            onChange={(e) => {
              setQuery(e.target.value);
              setVisible(PAGE);
            }}
          />
        </label>
        <div className="sender-chips" role="group" aria-label="Filter senders">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`sender-chip${filter === f.key ? " is-active" : ""}`}
              aria-pressed={filter === f.key}
              onClick={() => {
                setFilter(f.key);
                setVisible(PAGE);
              }}
            >
              {f.label} ({counts[f.key]})
            </button>
          ))}
        </div>
      </div>

      {groups.shown.length === 0 ? (
        <p className="connection-empty">
          {filter === "suggested"
            ? "No senders waiting — SchoolSift suggests school senders after a sync."
            : `No ${FILTERS.find((f) => f.key === filter)?.label.toLowerCase()} senders.`}
        </p>
      ) : (
        <>
          {groups.shown.map((g) => (
            <section
              key={g.domain}
              className="sender-domain"
              aria-label={g.domain}
            >
              <div className="sender-domain-head">
                <h3 className="sender-domain-name">{g.domain}</h3>
                <span className="connection-meta">
                  {g.total} sender{g.total === 1 ? "" : "s"}
                </span>
                {g.collapsible && (
                  <button
                    type="button"
                    className="btn btn-quiet btn-sm"
                    aria-expanded={!g.collapsed}
                    onClick={() => toggleDomain(g.domain)}
                  >
                    {g.collapsed
                      ? `Show ${g.total} senders`
                      : "Show fewer"}
                  </button>
                )}
                {canEdit && g.pendingIds.length > 0 && (
                  <span className="sender-domain-actions">
                    <button
                      type="button"
                      className="btn btn-secondary btn-sm"
                      disabled={busyKey !== null || bulk !== null}
                      onClick={() =>
                        void runBulk("confirm", g.pendingIds)
                      }
                    >
                      Trust all in domain
                    </button>
                    <button
                      type="button"
                      className="btn btn-quiet btn-sm"
                      disabled={busyKey !== null || bulk !== null}
                      onClick={() =>
                        void runBulk("reject", g.pendingIds)
                      }
                    >
                      Ignore all in domain
                    </button>
                  </span>
                )}
              </div>
              {!g.collapsed && (
                <ol className="sender-rows">
                  {g.rows.map((s) => (
                    <li key={s.id} className="sender-row">
                      <div className="sender-row-info">
                        <span className="sender-addr">
                          {s.sender_email}
                        </span>
                        <span className="sender-date">
                          last seen {relativeDate(s.last_seen_at)}
                          {s.status === "confirmed"
                            ? " · trusted"
                            : s.status === "rejected"
                              ? " · ignored"
                              : ""}
                        </span>
                      </div>
                      {canEdit && s.status === "suggested" && (
                        <div className="sender-row-actions">
                          <button
                            type="button"
                            className="btn btn-primary btn-sm"
                            disabled={busyKey !== null || bulk !== null}
                            aria-busy={busyKey === `source-confirm:${s.id}`}
                            onClick={() =>
                              void onSource(s.id, "confirm")
                            }
                          >
                            Trust sender
                            {busyKey === `source-confirm:${s.id}` && (
                              <span className="btn-busy" aria-hidden="true" />
                            )}
                          </button>
                          <button
                            type="button"
                            className="btn btn-quiet btn-sm"
                            disabled={busyKey !== null || bulk !== null}
                            aria-busy={busyKey === `source-reject:${s.id}`}
                            onClick={() =>
                              void onSource(s.id, "reject")
                            }
                          >
                            Ignore sender
                            {busyKey === `source-reject:${s.id}` && (
                              <span className="btn-busy" aria-hidden="true" />
                            )}
                          </button>
                        </div>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </section>
          ))}
          {groups.hidden > 0 && (
            <button
              type="button"
              className="btn btn-secondary senders-more"
              onClick={() => setVisible((v) => v + PAGE)}
            >
              Show 50 more
            </button>
          )}
        </>
      )}
      {bulk !== null && (
        <p role="status" className="connection-meta">
          Updating senders… {bulk.done}/{bulk.total}
        </p>
      )}
    </div>
  );
}
