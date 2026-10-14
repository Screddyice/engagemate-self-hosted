import type { Metadata } from "next";
import Link from "next/link";
import { UnsubscribeConfirm } from "@/components/marketing/unsubscribe-confirm";
import { Display, Eyebrow, Lede, Panel, Section } from "@/components/marketing/primitives";
import {
  EMAIL_CATEGORY_LABELS,
  verifyUnsubscribe,
} from "@/lib/email-unsubscribe";

export const metadata: Metadata = {
  title: "Unsubscribe from EngageMate emails",
  robots: { index: false, follow: false },
};

type SearchParams = Record<string, string | string[] | undefined>;

function single(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/**
 * Every EngageMate email a user can turn off links here with a signed token.
 * Opening the page changes nothing; the button does, so link scanners in mail
 * clients cannot unsubscribe anyone.
 */
export default async function UnsubscribePage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const u = single(params.u);
  const c = single(params.c);
  const t = single(params.t);
  const verified = verifyUnsubscribe(u, c, t);

  return (
    <Section className="pt-16 sm:pt-20">
      <Eyebrow tone="blue">Email preferences</Eyebrow>
      <div className="mt-4 max-w-[720px]">
        <Display>Unsubscribe</Display>
      </div>
      {verified && u && c && t ? (
        <>
          <Lede>
            Turn off EngageMate {EMAIL_CATEGORY_LABELS[verified.category]}. Your
            account and scheduled engagement keep running.
          </Lede>
          <Panel className="mt-8 max-w-[560px]">
            <UnsubscribeConfirm
              u={u}
              c={c}
              t={t}
              label={EMAIL_CATEGORY_LABELS[verified.category]}
            />
          </Panel>
        </>
      ) : (
        <>
          <Lede>
            This unsubscribe link is incomplete or no longer valid. Sign in and
            open Settings to choose which EngageMate emails you get.
          </Lede>
          <p className="mt-6">
            <Link
              href="/settings"
              className="text-[15px] font-semibold text-[var(--w-blue)] underline"
            >
              Open email settings
            </Link>
          </p>
        </>
      )}
    </Section>
  );
}
