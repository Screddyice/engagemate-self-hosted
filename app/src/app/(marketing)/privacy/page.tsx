import type { Metadata } from "next";
import {
  Display,
  Divider,
  Eyebrow,
  Lede,
  Panel,
  Section,
  SectionHeading,
} from "@/components/marketing/primitives";
import { supportEmail } from "@/lib/site";

export const metadata: Metadata = {
  title: "EngageMate connection privacy",
  description:
    "How EngageMate handles Instagram session data through hosted sign-in and extension connections, and how to request deletion.",
};

/** Describe both connection methods without implying hosted input bypasses EngageMate. */
export default function ConnectionPrivacyPage() {
  const SUPPORT_EMAIL = supportEmail();
  return (
    <>
      <Section className="pt-16 sm:pt-20">
        <Eyebrow tone="blue">Privacy</Eyebrow>
        <div className="mt-4 max-w-[880px]">
          <Display>
            How EngageMate handles
            <br />
            your connection.
          </Display>
        </div>
        <Lede>
          EngageMate uses a signed-in Instagram session to perform the actions
          you configure. Your connection may use hosted sign-in or the Chrome
          extension. This page explains what each method handles and what we
          keep.
        </Lede>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>Hosted Instagram sign-in</SectionHeading>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          When hosted sign-in is available in your account, EngageMate opens an
          isolated, temporary browser on our servers. A separate sign-in tab
          shows EngageMate controls for each recognized Instagram step. We relay
          the values you enter, including credentials and verification codes, to
          Instagram in that browser. Your input passes through EngageMate.
        </p>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          The sign-in page has no analytics or session-replay scripts. We do not
          show or store the temporary browser screen, and we do not store your
          password, verification codes, or input logs. The temporary browser
          expires after 10 minutes and closes when you finish or cancel.
        </p>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          After you choose Finish connecting, we verify that the signed-in
          Instagram account matches the username you entered and save its
          encrypted session for the actions you configure. We use your
          browser&apos;s time zone to select an available connection region.
          Closing the temporary browser does not delete the saved session or
          account connection records.
        </p>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>Cookies the extension reads</SectionHeading>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          On instagram.com, and nowhere else, the extension reads six session
          cookies:
        </p>
        <ul className="mt-4 max-w-[720px] list-disc space-y-1 pl-5 text-[15px] leading-relaxed text-[var(--w-muted)]">
          <li>
            <code>sessionid</code>
          </li>
          <li>
            <code>csrftoken</code>
          </li>
          <li>
            <code>ds_user_id</code>
          </li>
          <li>
            <code>mid</code>
          </li>
          <li>
            <code>ig_did</code>
          </li>
          <li>
            <code>rur</code>
          </li>
        </ul>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          It reads no other cookie, on Instagram or on any other site.
        </p>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>Where extension data goes</SectionHeading>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          The extension posts those cookies and your browser&apos;s user-agent
          string to EngageMate over HTTPS. EngageMate reads the connection
          request&apos;s public IP address to choose a nearby execution proxy and
          uses the browser time zone if IP geolocation fails. EngageMate sends
          the IP address to ipwho.is for this lookup. The IP address is not
          stored. EngageMate encrypts the Instagram cookies at rest and never
          returns them to a web page, an API response, or a log line.
        </p>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>Signing in with the extension</SectionHeading>
        <Panel>
          <p className="text-[15px] leading-relaxed text-[var(--w-muted)]">
            With the extension, you sign in to Instagram on instagram.com in
            your own browser. Instagram may ask for 2FA or a security check;
            you complete that step there while EngageMate waits for the
            session. The extension transfers session cookies. It does not read
            or send the password you type into Instagram&apos;s login form.
          </p>
        </Panel>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>What the extension does not do</SectionHeading>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          The extension performs no likes, replies, DMs, follows, comments, or
          posts. It captures and refreshes a session. Engagement runs on
          EngageMate&apos;s servers, under the limits and safety controls you set
          in your dashboard.
        </p>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>Retention and deletion</SectionHeading>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          EngageMate keeps a stored session while the account stays connected.
          To request deletion of a saved session or your account data, contact{" "}
          {SUPPORT_EMAIL ?? "the operator of this EngageMate service"}. Removing the extension from Chrome stops new
          captures; closing a hosted sign-in tab ends your view of that browser.
          Neither action deletes the session EngageMate has already saved.
          You can revoke the connected browser session through Instagram.
          Logging out of a different browser does not revoke a hosted session.
        </p>
      </Section>

      <Divider />

      <Section tight>
        <SectionHeading>Contact</SectionHeading>
        <p className="mt-4 max-w-[720px] text-[15px] leading-relaxed text-[var(--w-muted)]">
          Questions about this policy, or a deletion request:{" "}
          {SUPPORT_EMAIL ? (
            <a className="underline" href={`mailto:${SUPPORT_EMAIL}`}>
              {SUPPORT_EMAIL}
            </a>
          ) : (
            "contact the operator of this EngageMate service"
          )}
          .
        </p>
      </Section>
    </>
  );
}
