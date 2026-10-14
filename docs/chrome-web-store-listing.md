# Chrome Web Store listing — EngageMate

The reviewed copy for the EngageMate extension's store listing. Change this
file, not the dashboard, and paste from here at submission time. A reviewer
reads the permission justifications and the reviewer note before the
description, so those sections carry the burden.

`app.example.com` below is a placeholder. Replace it with the origin you set in
`ENGAGEMATE_WEB_ORIGIN` when you package the extension. The project's original
domain is no longer used and must not appear in a listing.

## Listing fields

**Title:** EngageMate — Instagram session bridge

**Summary:** Connect your Instagram account to EngageMate by carrying your
signed-in session across. No password, no engagement.

**Category:** Workflow & Planning

**Support URL:** https://app.example.com/support

**Product URL:** https://app.example.com

**Privacy URL:** https://app.example.com/privacy

## Full description

EngageMate schedules Instagram engagement on your behalf, on your own account,
under limits you set. To do that it needs the Instagram session you are already
signed into.

This extension carries that session across. You sign in to Instagram on
instagram.com the way you always do. The extension reads six session cookies
from instagram.com, posts them to EngageMate over HTTPS, and refreshes them when
Instagram rotates them. EngageMate encrypts them at rest. During Brand setup,
the extension also reads public profile summaries for the source accounts you
enter. EngageMate uses those summaries to shape your audience and voice.

What this extension does not do matters as much. It performs no likes, no
replies, no DMs, no follows, no comments, and no posts. It never sees your
Instagram password, because you type that into Instagram, not into EngageMate.
Engagement runs on EngageMate's servers under the safety controls in your
dashboard: one dedicated proxy per account, a warm-up ramp, and an immediate
stop on the first challenge.

You need an EngageMate account to use this extension.

Privacy policy: https://app.example.com/privacy

## Permission justifications

Paste each into the matching field. A justification that describes a capability
without naming the user-visible feature it serves gets rejected.

**`cookies`** — Reads six Instagram session cookies (`sessionid`, `csrftoken`,
`ds_user_id`, `mid`, `ig_did`, `rur`) so EngageMate can act as the signed-in
user during scheduled runs. This is the extension's only function. No other
cookie on any site is read.

**`storage`** — Stores the verified Instagram handle, the server-selected login
region, and the last sync status so the extension can refresh the connection
without asking the user for technical account details. Session cookies are
never written to storage.

**`alarms`** — Refreshes the captured session every 30 minutes. Instagram
rotates session cookies, and a stale session makes every scheduled run fail
with a login prompt the user cannot see.

**Instagram host access (`https://*.instagram.com/*`, `https://instagram.com/*`)**
— Required to read the six session cookies above and to detect whether the user
is currently signed in. The service worker also reads Instagram's authenticated
self-profile endpoint so it can verify the signed-in account against the
`ds_user_id` cookie. During Brand setup, it reads public profile summaries for
the three to five source handles the user entered. The extension injects no
script into Instagram and never reads Instagram page content.

**EngageMate host access (`https://app.example.com/*`)** — Required to POST the
captured session to the user's own EngageMate account and to run the onboarding
bridge content script on EngageMate's own pages. The packaged manifest lists
exactly the origins set in `ENGAGEMATE_WEB_ORIGIN`; an unconfigured build lists
only `http://localhost:3030/*` for local development.

## Reviewer note

Paste into "Notes for reviewer".

> The service worker captures and refreshes a signed-in Instagram session and
> posts it to the user's own EngageMate account. During Brand setup, it also
> reads bounded public profile summaries for three to five source handles. It
> performs no engagement. Likes, replies, and DMs run server-side in
> the operator's own machine or VM under per-account policy gates.
>
> The single content script runs only on EngageMate's own origins. It forwards
> three message types from the EngageMate onboarding page to the service worker:
> installation ping, connection intent, and bounded public-profile lookup. It
> is not injected into instagram.com.
>
> To test: run your own fork and open it with your generated owner setup key, then sign in to Instagram
> in the same browser, open the Accounts step of onboarding, and click Connect
> Instagram. The extension derives the account and login region without asking
> the user to enter either value.

## Required submission assets

Fork owners must create their own submission screenshots with synthetic account data. No historical screenshots ship with this repository. Do not capture cookies, credentials, real customer content or browser profiles. Typical assets are:

- `01-accounts-install-1280x800.png`
- `02-instagram-sign-in-1280x800.png`
- `03-connected-1280x800.png`
- `promo-small-440x280.png`

Record SHA-256 checksums in your own review notes before distributing the package.

## Packaging

```bash
ENGAGEMATE_WEB_ORIGIN=https://app.example.com python3 scripts/package_extension.py
```

Writes `dist/engagemate-extension-<version>.zip`, where `<version>` is the
`version` field in `extension/manifest.json`. The archive is deterministic:
two builds of one commit produce identical bytes, so the reviewed upload can be
tied back to the source it came from.
