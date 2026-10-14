import { MarketingNav } from "@/components/marketing/nav";
import { MarketingFooter } from "@/components/marketing/footer";
import { DotGrid } from "@/components/marketing/primitives";

export default function MarketingLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="relative min-h-screen" style={{ background: "var(--w-bg)" }}>
      <DotGrid />
      <MarketingNav />
      <main className="relative z-[1] pt-[76px]">{children}</main>
      <MarketingFooter />
    </div>
  );
}
