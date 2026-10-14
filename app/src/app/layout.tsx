import type { Metadata } from "next";
import Script from "next/script";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { appUrl } from "@/lib/site";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  // Absolute base for og:image and twitter:image. Without it Next emits a
  // RELATIVE image URL, and Slack, iMessage, LinkedIn and X all silently drop
  // the preview — the page still 200s, so nothing looks broken while every
  // shared link renders bare.
  // NEXT_PUBLIC_APP_URL supplies the origin; unset leaves Next's local default.
  metadataBase: appUrl() ?? undefined,
  title: "EngageMate | Instagram Story engagement",
  description:
    "Inspect an active Instagram Story, then choose one account-scoped action: like, reply, or DM.",
  openGraph: {
    type: "website",
    siteName: "EngageMate",
    url: appUrl()?.origin,
    title: "EngageMate | Reply to every Story worth replying to",
    description:
      "Inspect an active Instagram Story, then choose one account-scoped action: like, reply, or DM.",
  },
  twitter: {
    // summary_large_image, not summary: the card is 1200x630, and `summary`
    // would crop it to a small square and cut the wordmark off.
    card: "summary_large_image",
    title: "EngageMate | Reply to every Story worth replying to",
    description:
      "Inspect an active Instagram Story, then choose one account-scoped action: like, reply, or DM.",
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (

      <html
        lang="en"
        suppressHydrationWarning
        className={`${inter.variable} ${jetbrainsMono.variable}`}
      >
        <head>
          <Script src="/theme-init.js" strategy="beforeInteractive" />
        </head>
        <body className="bg-[var(--w-bg)] font-sans antialiased">
          {children}
        </body>
      </html>

  );
}
