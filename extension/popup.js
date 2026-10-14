// EngageMate popup: one user-approved Instagram connection action.
// Identity and login geography are resolved by the service worker.

// Chrome exposes `chrome`; Safari also exposes the promise-based `browser`.
const ext =
  typeof globalThis.browser?.runtime?.sendMessage === "function"
    ? globalThis.browser
    : globalThis.chrome;

/**
 * Message the service worker and call back once with (error, response).
 * Chrome answers through the callback and runtime.lastError. A browser that
 * returns a promise instead settles through the promise. Whichever arrives
 * first wins.
 */
function sendToWorker(message, callback) {
  let settled = false;
  const finish = (error, response) => {
    if (settled) return;
    settled = true;
    callback(error, response);
  };
  try {
    const pending = ext.runtime.sendMessage(message, (response) => {
      finish(ext.runtime.lastError || null, response);
    });
    if (pending && typeof pending.then === "function") {
      pending.then(
        (response) => finish(null, response),
        (error) => finish(error || new Error("message failed")),
      );
    }
  } catch (error) {
    finish(error);
  }
}

const els = {
  authBadge: document.getElementById("auth-badge"),
  statusMsg: document.getElementById("status-msg"),
  igStatus: document.getElementById("ig-status"),
  connectBtn: document.getElementById("connect-btn"),
};

const ERROR_COPY = {
  auth_required:
    "Sign in to EngageMate in this browser, then click Connect Instagram again.",
  ig_session_missing:
    "Sign in on the Instagram tab, then reopen EngageMate if the connection does not finish.",
  identity_discovery_failed:
    "EngageMate could not identify the signed-in Instagram account. Reopen Instagram and try again.",
  identity_rate_limited:
    "Instagram is temporarily limiting account checks. Wait a few minutes and try again.",
  geo_discovery_failed:
    "EngageMate could not prepare the connection. Check your EngageMate sign-in and try again.",
  instagram_open_failed:
    "EngageMate could not open Instagram. Open instagram.com, sign in, then try again.",
  instagram_access_required:
    "Allow EngageMate on instagram.com in your browser's extension settings, then click Connect Instagram again.",
  network_error:
    "EngageMate could not complete the connection. Check your network and try again.",
  server_error:
    "EngageMate could not complete the connection. Try again in a moment.",
};

function showMessage(text, kind) {
  els.statusMsg.textContent = text;
  els.statusMsg.classList.remove("hidden", "success", "error");
  els.statusMsg.classList.add(kind === "success" ? "success" : "error");
}

function clearMessage() {
  els.statusMsg.classList.add("hidden");
  els.statusMsg.textContent = "";
}

function showConnectionError(code) {
  showMessage(
    ERROR_COPY[code] ||
      "EngageMate could not complete the connection. Try again.",
    "error",
  );
}

function applyStatus(status) {
  const authenticated = Boolean(status.auth?.authenticated);
  els.authBadge.textContent = authenticated ? "Signed in" : "Sign in required";
  els.authBadge.classList.toggle("badge-connected", authenticated);
  els.authBadge.classList.toggle("badge-disconnected", !authenticated);

  const account = status.account;
  const connecting =
    authenticated &&
    !status.lastErrorCode &&
    status.connectionRequested &&
    status.instagram?.loggedIn &&
    !account;
  els.igStatus.textContent = account
    ? "Connected"
    : connecting
      ? "Connecting…"
      : "Reconnect Instagram";
  els.connectBtn.disabled = Boolean(connecting);
  els.connectBtn.textContent = account
    ? "Reconnect Instagram"
    : connecting
      ? "Connecting…"
      : "Connect Instagram";

  if (status.lastErrorCode) {
    showConnectionError(status.lastErrorCode);
  } else if (!authenticated && status.connectionRequested) {
    showConnectionError("auth_required");
  } else if (connecting) {
    showMessage("Finishing your Instagram connection…", "success");
  }
}

function loadStatus() {
  sendToWorker({ type: "GET_STATUS" }, (error, status) => {
    if (error) {
      showConnectionError("");
      return;
    }
    applyStatus(status || {});
  });
}

els.connectBtn.addEventListener("click", () => {
  clearMessage();
  els.connectBtn.disabled = true;
  els.connectBtn.textContent = "Connecting…";

  sendToWorker({ type: "CONNECT_INSTAGRAM" }, (error, result) => {
    els.connectBtn.disabled = false;
    els.connectBtn.textContent = "Connect Instagram";

    if (error) {
      showConnectionError("");
      return;
    }
    if (result?.status === "opening_instagram") {
      showMessage(
        "Instagram opened in a new tab. Sign in there and EngageMate will finish the connection.",
        "success",
      );
      loadStatus();
      return;
    }
    if (!result?.success) {
      showConnectionError(result?.code);
      loadStatus();
      return;
    }

    showMessage("Instagram connected", "success");
    loadStatus();
  });
});

loadStatus();
