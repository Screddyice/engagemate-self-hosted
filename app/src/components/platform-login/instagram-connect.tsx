"use client";

/**
 * The one Instagram connection surface, shared by onboarding and the dashboard.
 *
 * Two rules shape it. The server, not the extension, decides when an account is
 * connected — the extension can only report what it attempted, so `onConnected`
 * fires on a `/api/accounts` row and nothing else. And every failure renders
 * from a fixed copy table keyed by a whitelisted code, so an upstream message
 * can never reach the DOM.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { HostedInstagramConnect } from "./hosted-instagram-connect";
import { Button } from "@/components/ui/button";
import { appHost } from "@/lib/site";
import {
  EXTENSION_CONNECT_V2,
  INSTAGRAM_SIGN_IN_TIMEOUT_MS,
  REQUIRED_EXTENSION_VERSION,
  createExtensionRequest,
  isDesktopSafari,
  isMobileBrowser,
  onExtensionStatus,
  requestExtensionInstance,
  safariExtensionDownloadUrl,
  type ExtensionInstance,
  type ExtensionStatusMessage,
} from "@/lib/extension-bridge";
import {
  healthyAccount,
  type ConnectableAccount,
} from "@/lib/instagram-account-health";

export type { ConnectableAccount } from "@/lib/instagram-account-health";

interface InstagramConnectProps {
  hostedLoginEnabled?: boolean;
  accounts: ConnectableAccount[];
  onConnected: (account: ConnectableAccount) => void;
}

type ConnectionState =
  | { kind: "checking_account" }
  | { kind: "desktop_required" }
  | { kind: "extension_missing" }
  | { kind: "extension_ready" }
  | { kind: "preparing_connection" }
  | { kind: "opening_instagram" }
  | { kind: "waiting_for_session" }
  | { kind: "syncing" }
  | { kind: "connected" }
  | { kind: "error"; code: string };

// Which install instructions this desktop browser can follow. Safari cannot
// load the unpacked Chrome package, so it gets the Safari app when one is
// published and a pointer to a Chromium browser when it is not.
type InstallTarget =
  | { kind: "chromium" }
  | { kind: "safari"; downloadUrl: string }
  | { kind: "safari_pending" };

const OPEN_INSTAGRAM_TIMEOUT_MS = 15_000;
const PING_INTERVAL_MS = 1_000;
const POLL_INTERVAL_MS = 2_000;
const DIRECT_DOWNLOAD_URL = `/downloads/engagemate-extension-${REQUIRED_EXTENSION_VERSION}.zip`;

// Every string a failure may render. A code outside this table falls through to
// the generic entry rather than printing whatever the extension sent.
const ERROR_COPY: Record<string, string> = {
  connection_unavailable:
    "EngageMate couldn't start the connection. Check your connection and try again in a moment.",
  extension_unresponsive:
    "The EngageMate extension didn't respond. Refresh this page, check that the extension is enabled, then try again.",
  ig_session_missing:
    "You are not signed in to Instagram in this browser yet. Open Instagram, sign in to Instagram, then try again.",
  auth_required:
    "Your EngageMate session expired. Refresh this page to sign in to EngageMate again, then retry.",
  account_conflict:
    "That Instagram account is already connected to another EngageMate workspace.",
  account_limit:
    "You have reached the Instagram account limit on your current plan.",
  invalid_connection_input:
    "EngageMate could not start the connection. Refresh this page and try again.",
  identity_discovery_failed:
    "EngageMate could not identify the signed-in Instagram account. Reopen Instagram and try again.",
  identity_rate_limited:
    "Instagram is temporarily limiting account checks. Wait a few minutes and try again.",
  geo_discovery_failed:
    "EngageMate could not prepare the connection. Check your EngageMate sign-in and try again.",
  instagram_open_failed:
    "EngageMate could not open Instagram. Open instagram.com yourself, sign in, then try again.",
  instagram_access_required:
    "The EngageMate extension is not allowed on instagram.com. Allow it in your browser's extension settings (in Safari: Settings, Extensions, EngageMate), then try again.",
  timeout:
    "We didn't see a connected account after 10 minutes. Finish the Instagram sign-in or security check, then try again.",
};

const GENERIC_ERROR =
  "EngageMate couldn't complete the connection. Try again in a moment.";

export function InstagramConnect(props: InstagramConnectProps) {
  return props.hostedLoginEnabled ? <HostedInstagramConnect accounts={props.accounts} onConnected={props.onConnected} /> : <LegacyInstagramConnect {...props} />;
}

function LegacyInstagramConnect({
  accounts,
  onConnected,
}: InstagramConnectProps) {
  const [state, setState] = useState<ConnectionState>({
    kind: "checking_account",
  });
  const [installTarget, setInstallTarget] = useState<InstallTarget>({
    kind: "chromium",
  });

  const cleanupsRef = useRef<Array<() => void>>([]);
  const extensionRef = useRef<ExtensionInstance | null>(null);
  const attemptRef = useRef(0);
  const statusCleanupRef = useRef<(() => void) | null>(null);
  const mountedRef = useRef(true);

  const track = useCallback((cleanup: () => void) => {
    cleanupsRef.current.push(cleanup);
    return cleanup;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      cleanupsRef.current.forEach((cleanup) => cleanup());
      cleanupsRef.current = [];
    };
  }, []);

  // Decide the entry state once: mobile is a dead end, an already-healthy
  // account needs no extension at all, and everyone else starts hunting for it.
  useEffect(() => {
    if (isMobileBrowser()) {
      setState({ kind: "desktop_required" });
      return;
    }
    if (isDesktopSafari()) {
      const downloadUrl = safariExtensionDownloadUrl();
      setInstallTarget(
        downloadUrl
          ? { kind: "safari", downloadUrl }
          : { kind: "safari_pending" },
      );
    }
    const healthy = healthyAccount(accounts);
    if (healthy) {
      setState({ kind: "connected" });
      return;
    }
    setState({ kind: "extension_missing" });
  }, [accounts]);

  // Ping until a matching content script answers. A newly loaded unpacked
  // extension joins after the user refreshes this page.
  useEffect(() => {
    if (state.kind !== "extension_missing") return;

    let cancelled = false;

    const ping = async () => {
      try {
        const instance = await requestExtensionInstance();
        if (cancelled) return;
        extensionRef.current = instance;
        setState({ kind: "extension_ready" });
      } catch {
        // Keep polling while the current package is absent or still loading.
      }
    };

    void ping();
    const interval = setInterval(() => void ping(), PING_INTERVAL_MS);

    return track(() => {
      cancelled = true;
      clearInterval(interval);
    });
  }, [state.kind, track]);

  const applyStatus = useCallback((message: ExtensionStatusMessage) => {
    if (message.status === "error") {
      setState({ kind: "error", code: message.code ?? "sync_failed" });
      return;
    }
    if (message.status === "connected") {
      // The extension believes it worked. The server still has to agree.
      setState({ kind: "syncing" });
      return;
    }
    if (message.status === "installed") return;
    setState({ kind: "waiting_for_session" });
  }, []);

  // A loaded extension can stop responding before it opens Instagram. The
  // longer sign-in timer starts only after acknowledgement, so bound this gap.
  useEffect(() => {
    if (
      state.kind !== "opening_instagram" &&
      state.kind !== "preparing_connection"
    ) return;
    const timeout = setTimeout(() => {
      attemptRef.current += 1;
      statusCleanupRef.current?.();
      statusCleanupRef.current = null;
      setState({
        kind: "error",
        code: state.kind === "preparing_connection"
          ? "connection_unavailable"
          : "extension_unresponsive",
      });
    }, OPEN_INSTAGRAM_TIMEOUT_MS);
    return () => clearTimeout(timeout);
  }, [state.kind]);

  // Poll for the server's own view. This is the only thing that can report
  // success, because it is the only thing that proves the row exists.
  useEffect(() => {
    if (state.kind !== "waiting_for_session" && state.kind !== "syncing")
      return;

    let cancelled = false;

    const poll = async () => {
      try {
        const response = await fetch("/api/accounts");
        if (!response.ok || cancelled) return;
        const rows = (await response.json()) as ConnectableAccount[];
        const healthy = healthyAccount(Array.isArray(rows) ? rows : []);
        if (!healthy || cancelled) return;
        setState({ kind: "connected" });
        onConnected(healthy);
      } catch {
        // A failed poll is not a failed connection; the next tick retries.
      }
    };

    const interval = setInterval(poll, POLL_INTERVAL_MS);
    const timeout = setTimeout(() => {
      if (cancelled) return;
      setState({ kind: "error", code: "timeout" });
    }, INSTAGRAM_SIGN_IN_TIMEOUT_MS);

    return track(() => {
      cancelled = true;
      clearInterval(interval);
      clearTimeout(timeout);
    });
  }, [state.kind, onConnected, track]);

  const signIn = async () => {
    const instance = extensionRef.current;
    if (!instance) {
      setState({ kind: "extension_missing" });
      return;
    }
    statusCleanupRef.current?.();
    statusCleanupRef.current = null;
    const attempt = ++attemptRef.current;
    const isCurrent = () => mountedRef.current && attemptRef.current === attempt;
    setState({ kind: "preparing_connection" });
    try {
      const response = await fetch("/api/extension/ticket", { method: "POST" });
      if (!isCurrent()) return;
      if (!response.ok) {
        setState({
          kind: "error",
          code: response.status === 401 ? "auth_required" : "connection_unavailable",
        });
        return;
      }
      const body = (await response.json()) as { ticket?: unknown };
      if (!isCurrent()) return;
      if (
        typeof body.ticket !== "string" ||
        !/^em_xt_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(body.ticket)
      ) {
        throw new Error("ticket unavailable");
      }
      setState({ kind: "opening_instagram" });
      const nonce = createExtensionRequest(EXTENSION_CONNECT_V2, {
        extensionId: instance.extensionId,
        ticket: body.ticket,
      });
      statusCleanupRef.current = track(onExtensionStatus(
        nonce,
        (message) => { if (isCurrent()) applyStatus(message); },
        { instance },
      ));
    } catch {
      if (isCurrent()) {
        setState({ kind: "error", code: "connection_unavailable" });
      }
    }
  };

  // Deciding between mobile, connected, and missing takes an effect. Rendering
  // the live form first would flash an enabled sign-in button on mobile.
  if (state.kind === "checking_account") {
    return (
      <p className="text-sm text-muted-foreground">Checking your connection…</p>
    );
  }

  if (state.kind === "desktop_required") {
    return (
      <div className="rounded-lg border border-violet-500/30 bg-violet-500/5 p-4">
        <p className="text-sm font-medium text-violet-300">
          Continue on desktop
        </p>
        <p className="mt-1 text-sm text-muted-foreground">
          Connecting Instagram needs the EngageMate Chrome extension, which
          installs on a desktop browser. Open this page on your computer to
          finish setup.
        </p>
      </div>
    );
  }

  if (state.kind === "connected") {
    return (
      <div className="flex items-center justify-between rounded-lg border p-4">
        <span className="font-medium text-pink-500">Instagram</span>
        <span className="rounded-full bg-green-500/10 px-3 py-1 text-xs font-medium text-green-400">
          Connected
        </span>
      </div>
    );
  }

  const busy =
    state.kind === "preparing_connection" ||
    state.kind === "opening_instagram" ||
    state.kind === "waiting_for_session" ||
    state.kind === "syncing";

  const progress =
    state.kind === "preparing_connection"
      ? "Preparing your connection…"
      : state.kind === "opening_instagram"
        ? "Opening Instagram in a new tab…"
        : state.kind === "waiting_for_session"
          ? "Waiting for you to finish signing in to Instagram, including 2FA or a security check…"
          : state.kind === "syncing"
            ? "Confirming the connection with EngageMate…"
            : null;

  return (
    <div className="space-y-4">
      {state.kind === "extension_missing" &&
        installTarget.kind === "safari" && (
          <div className="rounded-lg border border-violet-500/30 bg-violet-500/5 p-4">
            <p className="text-sm font-medium text-violet-300">
              Add EngageMate for Safari
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              The Safari extension reads your Instagram session in your own
              browser. EngageMate never sees or stores your Instagram password.
            </p>
            <div className="mt-4 space-y-3">
              <Button asChild variant="gradient">
                <a href={installTarget.downloadUrl}>
                  Download EngageMate for Safari
                </a>
              </Button>
              <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
                <li>
                  Move the downloaded EngageMate app to your Applications
                  folder, then open it once.
                </li>
                <li>
                  In Safari, open{" "}
                  <strong className="text-foreground">
                    Settings, then Extensions
                  </strong>
                  , and turn on EngageMate.
                </li>
                <li>
                  Allow EngageMate on{" "}
                  <code className="text-foreground">instagram.com</code> and{" "}
                  <code className="text-foreground">
                    {appHost() ?? "this site"}
                  </code>
                  .
                </li>
                <li>Return to EngageMate and refresh this page.</li>
              </ol>
              <p className="text-xs text-muted-foreground">
                Requires Safari 18 or later on a Mac. After the refresh,
                EngageMate detects the extension and enables sign-in.
              </p>
            </div>
          </div>
        )}

      {state.kind === "extension_missing" &&
        installTarget.kind === "safari_pending" && (
          <div className="rounded-lg border border-violet-500/30 bg-violet-500/5 p-4">
            <p className="text-sm font-medium text-violet-300">
              Connect from Chrome, Edge, Brave, or Arc
            </p>
            <p className="mt-1 text-sm text-muted-foreground">
              Open EngageMate in Chrome, Edge, Brave, or Arc on this computer
              to connect Instagram. Safari support is pending.
            </p>
          </div>
        )}

      {state.kind === "extension_missing" && installTarget.kind === "chromium" && (
        <div className="rounded-lg border border-violet-500/30 bg-violet-500/5 p-4">
          <p className="text-sm font-medium text-violet-300">
            Add the EngageMate extension
          </p>
          <p className="mt-1 text-sm text-muted-foreground">
            The extension reads your Instagram session in your own browser.
            EngageMate never sees or stores your Instagram password.
          </p>
          <div className="mt-4 space-y-3">
            <Button asChild variant="gradient">
              <a href={DIRECT_DOWNLOAD_URL} download>
                Download the EngageMate extension
              </a>
            </Button>
            <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
              <li>Unzip the downloaded file.</li>
              <li>
                Open{" "}
                <code className="text-foreground">chrome://extensions</code> and
                enable Developer mode.
              </li>
              <li>
                Choose{" "}
                <strong className="text-foreground">Load unpacked</strong> and
                select the extracted folder.
              </li>
              <li>Keep the extracted folder on this computer.</li>
              <li>Return to EngageMate and refresh this page.</li>
            </ol>
            <p className="text-xs text-muted-foreground">
              After the refresh, EngageMate detects the extension and enables
              sign-in.
            </p>
          </div>
        </div>
      )}

      <p className="text-sm text-muted-foreground">
        EngageMate will connect the Instagram account signed in to this browser.
      </p>

      <Button
        variant="gradient"
        onClick={signIn}
        disabled={state.kind === "extension_missing" || busy}
      >
        Connect Instagram
      </Button>

      {progress && <p className="text-sm text-muted-foreground">{progress}</p>}

      {state.kind === "error" && (
        <p className="text-sm text-red-400">
          {ERROR_COPY[state.code] ?? GENERIC_ERROR}
        </p>
      )}
    </div>
  );
}
