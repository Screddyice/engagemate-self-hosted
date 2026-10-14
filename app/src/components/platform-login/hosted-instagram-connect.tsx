"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  healthyAccount,
  type ConnectableAccount,
} from "@/lib/instagram-account-health";

type Session = {
  id: string;
  state: "active" | "connected" | "error";
  expires_at: string;
  account?: ConnectableAccount;
  code?: string;
};
const COPY: Record<string, string> = {
  connection_unavailable:
    "EngageMate’s Instagram sign-in service is unavailable. Please try again later.",
  auth_required:
    "Your EngageMate session expired. Refresh this page to sign in again.",
  session_expired:
    "The temporary browser expired. Start a new connection to try again.",
  session_not_found:
    "The temporary browser is no longer available. Start a new connection.",
  session_busy:
    "You already have a connection in progress. Finish or cancel it before starting another.",
  capacity: "All connection browsers are in use. Try again in a few minutes.",
  capacity_unavailable:
    "We need more capacity before connecting another account. Your existing accounts remain connected.",
  proxy_unavailable:
    "EngageMate couldn't verify the dedicated Instagram connection. We retried without changing your account; try again in a minute.",
  account_limit: "You have reached the Instagram account limit on your plan.",
  account_conflict:
    "That Instagram account is already connected to another workspace.",
  connection_capacity:
    "All connection browsers are in use. Try again in a few minutes.",
  connection_in_progress:
    "A connection is already in progress. Finish it in the open tab, or wait for it to expire before trying again.",
  connection_not_found:
    "The temporary browser is no longer available. Start a new connection.",
  connection_cancelled:
    "The connection was cancelled. You can start again when you are ready.",
  connection_closed:
    "The temporary browser closed. Start a new connection to try again.",
  connection_lost:
    "We lost the temporary browser. Start a new connection to try again.",
  instagram_rate_limited:
    "Instagram is limiting sign-in requests. Wait before trying again.",
  account_browser_busy:
    "EngageMate is using this account for an action. Try connecting again once that action finishes.",
  geography_unavailable:
    "We couldn't prepare a connection in your region. Please contact support.",
  session_unavailable:
    "The temporary browser is no longer available. Start a new connection.",
  account_unavailable:
    "This account is unavailable for connection. Please contact support.",
  invalid_username:
    "Enter an Instagram username using letters, numbers, periods or underscores.",
};
const GENERIC =
  "We couldn't complete that request. Please try again later.";
async function request(
  path: string,
  init: RequestInit = {},
  timeout = 20_000,
): Promise<Session> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const r = await fetch(path, {
      ...init,
      cache: "no-store",
      signal: controller.signal,
    });
    if (r.status === 204) return {} as Session;
    const value = (await r.json()) as Session;
    if (!r.ok)
      throw new Error(
        r.status === 401
          ? "auth_required"
          : value.code || "connection_unavailable",
      );
    return value;
  } finally {
    clearTimeout(timer);
  }
}
export function HostedInstagramConnect({
  accounts,
  onConnected,
}: {
  accounts: ConnectableAccount[];
  onConnected: (account: ConnectableAccount) => void;
}) {
  const [username, setUsername] = useState(
    accounts[0]?.username ?? "",
  );
  const [session, setSession] = useState<Session | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verifiedAccount, setVerifiedAccount] =
    useState<ConnectableAccount | null>(null);
  const [reconnecting, setReconnecting] = useState(false);
  const connectedAccount = verifiedAccount ?? healthyAccount(accounts);
  const [pollWarning, setPollWarning] = useState(false);
  const callback = useRef(onConnected);
  callback.current = onConnected;
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    // A later owned snapshot takes precedence over an earlier login receipt.
    // Keep typed credentials and an active temporary browser untouched.
    setVerifiedAccount((current) => current
      ? accounts.find((account) => account.id === current.id) ?? null
      : null);
  }, [accounts]);
  useEffect(() => {
    if (!reconnecting && !busy && !session) setUsername(accounts[0]?.username ?? "");
  }, [accounts, reconnecting, busy, session]);

  useEffect(() => {
    if (!session || session.state !== "active") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      if (cancelled) return;
      if (Date.now() >= Date.parse(session!.expires_at)) {
        setSession(null);
        setError(COPY.session_expired);
        return;
      }
      try {
        const next = await request(`/api/instagram/connect/${session!.id}`);
        if (cancelled) return;
        setPollWarning(false);
        if (next.state === "connected" && next.account) {
          setVerifiedAccount(next.account);
          setReconnecting(false);
          setSession(null);
          setError(null);
          callback.current(next.account);
          return;
        }
        if (next.state === "error") {
          setSession(null);
          setError(COPY[next.code ?? ""] ?? GENERIC);
          return;
        }
      } catch (e) {
        if (cancelled) return;
        const code = e instanceof Error ? e.message : "";
        if (
          [
            "session_expired",
            "session_not_found",
            "connection_not_found",
            "session_unavailable",
            "auth_required",
          ].includes(code)
        ) {
          setSession(null);
          setError(COPY[code]);
          return;
        }
        setPollWarning(true);
      }
      if (!cancelled) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [session]);

  async function start(e: React.FormEvent) {
    e.preventDefault();
    // Open synchronously from the click. Remove the opener before loading the
    // isolated viewer, so parent analytics cannot inspect its sign-in document.
    const tab = window.open("about:blank", "_blank");
    if (tab) {
      tab.opener = null;
      tab.document.title = "Preparing Instagram connection";
    }
    setBusy(true);
    setError(null);
    const requestedUsername = username.trim().replace(/^@/, "");
    const reconnectAccount = accounts.find(
      (account) => account.username.toLowerCase() === requestedUsername.toLowerCase(),
    );
    try {
      const next = await request(
        "/api/instagram/connect",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: requestedUsername,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
            ...(reconnectAccount ? { account_id: reconnectAccount.id } : {}),
          }),
        },
        100_000,
      );
      if (!mounted.current) {
        tab?.close();
        return;
      }
      if (
        !/^[a-f0-9-]{36}$/i.test(next.id) ||
        next.state !== "active" ||
        !Number.isFinite(Date.parse(next.expires_at))
      )
        throw new Error("connection_unavailable");
      setSession(next);
      if (tab && !tab.closed)
        tab.location.replace(`/api/instagram/connect/${next.id}/view`);
    } catch (e) {
      tab?.close();
      if (mounted.current)
        setError(COPY[e instanceof Error ? e.message : ""] ?? GENERIC);
    } finally {
      if (mounted.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!session) return;
    setBusy(true);
    setError(null);
    try {
      await request(`/api/instagram/connect/${session.id}`, {
        method: "DELETE",
      });
      setSession(null);
    } catch {
      setError(
        "We couldn't confirm cancellation. Try again; the temporary browser expires after 10 minutes.",
      );
    } finally {
      setBusy(false);
    }
  }
  if (connectedAccount && !session && !busy && !reconnecting)
    return (
      <div className="space-y-2 rounded-lg border p-4 text-sm">
        <p className={connectedAccount.healthStatus === "active" ? "text-green-400" : "text-amber-400"}>
          {connectedAccount.healthStatus === "active" ? "Instagram connected" : "Instagram sign-in saved"}
        </p>
        <p>@{connectedAccount.username}</p>
        {connectedAccount.healthStatus !== "active" && (
          <p className="text-muted-foreground">
            Automation remains {connectedAccount.healthStatus}.{" "}
            <a className="text-blue-400 underline" href="/account-health">
              Review account health
            </a>{" "}
            before continuing.
          </p>
        )}
        <Button type="button" variant="outline" onClick={() => setReconnecting(true)}>
          Reconnect Instagram
        </Button>
      </div>
    );
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Sign in using a temporary browser hosted by EngageMate. You can complete
        Instagram verification there. No download or extension is needed.
      </p>
      {!session ? (
        <form onSubmit={start} className="space-y-3">
          <label
            htmlFor="hosted-instagram-username"
            className="block text-sm font-medium"
          >
            Instagram username
          </label>
          <input
            id="hosted-instagram-username"
            value={username}
            onChange={(e) => { setUsername(e.target.value); setReconnecting(true); }}
            required
            pattern="@?[A-Za-z0-9_.]{1,30}"
            maxLength={31}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            disabled={busy}
            placeholder="your_username"
            className="w-full rounded-md border bg-background px-3 py-2"
          />
          <Button type="submit" variant="gradient" disabled={busy}>
            {busy ? "Preparing your connection…" : "Connect Instagram"}
          </Button>
          {busy && (
            <p role="status" className="text-sm text-muted-foreground">
              Preparing your temporary browser can take up to 90 seconds.
            </p>
          )}
        </form>
      ) : (
        <div className="space-y-3 rounded-lg border p-4">
          <p role="status" className="text-sm">
            Finish signing in in the connection tab, then choose{" "}
            <strong>Finish connecting</strong>. This page will update once we
            verify your account.
          </p>
          <a
            href={`/api/instagram/connect/${session.id}/view`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-block text-sm text-blue-400 underline"
          >
            Open Instagram connection tab
          </a>
          <p className="text-xs text-muted-foreground">
            If your browser blocked the new tab, use the link above. The
            temporary browser expires after 10 minutes.
          </p>
          {pollWarning && (
            <p role="status" className="text-sm text-amber-400">
              Reconnecting to check your progress…
            </p>
          )}
          <Button
            type="button"
            variant="outline"
            disabled={busy}
            onClick={cancel}
          >
            Cancel connection
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="text-sm text-red-400">
          {error}
        </p>
      )}
    </div>
  );
}
