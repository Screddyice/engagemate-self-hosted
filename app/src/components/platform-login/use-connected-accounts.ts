"use client";

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import type { ConnectableAccount } from "@/lib/instagram-account-health";

/** Refresh owned account metadata; this never starts an Instagram browser. */
export function useConnectedAccounts(initialAccounts: ConnectableAccount[] | null) {
  const [accounts, updateAccounts] = useState(initialAccounts);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const current = useRef(accounts);
  const revision = useRef(0);
  current.current = accounts;
  const setAccounts = useCallback((next: SetStateAction<ConnectableAccount[] | null>) => {
    revision.current += 1;
    updateAccounts(next);
  }, []);

  useEffect(() => {
    setAccounts(initialAccounts);
  }, [initialAccounts, setAccounts]);

  const needsInitialLoad = initialAccounts === null;
  useEffect(() => {
    let stopped = false;
    let pending: AbortController | null = null;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      if (stopped || pending || document.visibilityState === "hidden") return;
      const startedAtRevision = revision.current;
      const controller = new AbortController();
      pending = controller;
      timeout = setTimeout(() => controller.abort(), 10_000);
      try {
        const response = await fetch("/api/accounts", {
          cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error("account_status_unavailable");
        const rows: unknown = await response.json();
        if (stopped || controller.signal.aborted || startedAtRevision !== revision.current) return;
        if (Array.isArray(rows) && rows.every((row) => row &&
          typeof row.id === "string" && typeof row.username === "string" &&
          typeof row.healthStatus === "string")) {
          updateAccounts(rows);
          setRefreshFailed(false);
        } else throw new Error("account_status_unavailable");
      } catch {
        // Keep the last owned snapshot; failed reads cannot confirm a login.
        if (!stopped && startedAtRevision === revision.current) setRefreshFailed(true);
      } finally {
        clearTimeout(timeout);
        if (pending === controller) pending = null;
      }
    }
    const onFocus = () => { void refresh(); };
    const timer = setInterval(() => {
      if (current.current === null || current.current.length) void refresh();
    }, 30_000);
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    if (needsInitialLoad) void refresh();
    return () => {
      stopped = true;
      pending?.abort();
      clearTimeout(timeout);
      clearInterval(timer);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [needsInitialLoad]);

  return { accounts, setAccounts, refreshFailed };
}
