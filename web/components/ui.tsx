"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { statusLabel, statusTone } from "@/lib/format";

export function StatusPill({ status }: { status: string }) {
  const tone = statusTone(status);
  return (
    <span className={`pill pill-${tone}`}>
      <span className="pill-mark" aria-hidden="true" />
      {statusLabel(status)}
    </span>
  );
}

export function ResultPill({ passed, label }: { passed: boolean | null | undefined; label?: string }) {
  if (passed == null) return <span className="pill pill-idle">{label ?? "Not run"}</span>;
  return (
    <span className={`pill pill-${passed ? "pass" : "fail"}`}>
      <span className="pill-mark" aria-hidden="true" />
      {label ?? (passed ? "Pass" : "Fail")}
    </span>
  );
}

export function Crumbs({ items }: { items: { href?: string; label: string }[] }) {
  return (
    <nav aria-label="Breadcrumb" className="crumbs">
      <ol>
        {items.map((it, i) => (
          <li key={i}>
            {it.href ? <Link href={it.href}>{it.label}</Link> : <span aria-current="page">{it.label}</span>}
          </li>
        ))}
      </ol>
    </nav>
  );
}

export function Panel({
  title,
  id,
  actions,
  children,
  tone,
  sub,
}: {
  title: string;
  id?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  tone?: "gate" | "fail" | "pass";
  sub?: React.ReactNode;
}) {
  const hid = id ? `${id}-h` : undefined;
  return (
    <section className={`panel${tone ? ` panel-${tone}` : ""}`} aria-labelledby={hid} id={id}>
      <div className="panel-head">
        <div>
          <h2 id={hid}>{title}</h2>
          {sub && <p className="panel-sub">{sub}</p>}
        </div>
        {actions && <div className="panel-actions">{actions}</div>}
      </div>
      {children}
    </section>
  );
}

export function ErrorNote({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="note note-fail" role="alert">
      <span>{msg}</span>
      {onRetry && (
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="empty">{children}</p>;
}

export function Skeleton({ lines = 3 }: { lines?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-label="Loading">
      {Array.from({ length: lines }).map((_, i) => (
        <span key={i} style={{ width: `${90 - i * 17}%` }} />
      ))}
    </div>
  );
}

/** Load data with refresh; keeps previous data visible while reloading. */
export function useLoad<T>(fn: (() => Promise<T>) | null, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    if (!fnRef.current) return;
    setLoading(true);
    try {
      const d = await fnRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload, setData };
}

const WHO_KEY = "ccopy.who";
export function useWho(): [string, (v: string) => void] {
  const [who, setWhoState] = useState("");
  useEffect(() => {
    try {
      setWhoState(window.localStorage.getItem(WHO_KEY) ?? "");
    } catch {
      /* ignore */
    }
  }, []);
  const setWho = (v: string) => {
    setWhoState(v);
    try {
      window.localStorage.setItem(WHO_KEY, v);
    } catch {
      /* ignore */
    }
  };
  return [who, setWho];
}
