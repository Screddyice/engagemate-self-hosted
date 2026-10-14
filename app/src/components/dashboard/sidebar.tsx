"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AccountSwitcher } from "@/components/dashboard/account-switcher";
import type { IgAccountRow } from "@/lib/queries";
import { ThemeToggle } from "@/components/theme-toggle";

const navItems = [
  { href: "/dashboard", label: "Overview", indent: false },
  { href: "/accounts", label: "Accounts", indent: false },
  { href: "/targets", label: "Targets", indent: false },
  { href: "/account-health", label: "Account Health", indent: false },
  { href: "/settings", label: "Settings", indent: false },
];

export function Sidebar({
  accounts = [],
  selectedAccountId = null,
}: {
  accounts?: IgAccountRow[];
  selectedAccountId?: string | null;
}) {
  const pathname = usePathname();

  return (
    <aside className="flex h-auto w-full shrink-0 flex-col border-b bg-background md:h-screen md:w-64 md:border-b-0 md:border-r">
      <div className="flex h-14 items-center border-b px-6">
        <Link href="/dashboard" className="text-lg font-bold">
          EngageMate
        </Link>
      </div>
      <AccountSwitcher
        accounts={accounts}
        selectedAccountId={selectedAccountId}
      />
      <nav className="flex gap-1 overflow-x-auto p-2 md:block md:flex-1 md:space-y-1 md:p-4">
        {navItems.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`block rounded-md px-3 py-2 text-sm font-medium transition-colors ${
              item.indent ? "ml-4 text-xs" : ""
            } ${
              pathname === item.href
                ? "bg-muted text-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground"
            }`}
          >
            {item.label}
          </Link>
        ))}
      </nav>
      <div className="flex items-center justify-between border-t p-4">
        <span className="text-xs text-muted-foreground">Your workspace</span>
        <ThemeToggle />
      </div>
    </aside>
  );
}
