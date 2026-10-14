import { Sidebar } from "@/components/dashboard/sidebar";
import { ensureUser } from "@/lib/ensure-user";
import { resolveSelectedAccount } from "@/lib/selected-account";

export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const user = await ensureUser();
  const selection = user
    ? await resolveSelectedAccount(user.id)
    : { accounts: [], selected: null };

  return (
    <div className="flex h-screen flex-col md:flex-row">
      <Sidebar
        accounts={selection.accounts}
        selectedAccountId={selection.selected?.id ?? null}
      />
      <main className="min-w-0 flex-1 overflow-y-auto p-4 sm:p-6 md:p-8">
        {children}
      </main>
    </div>
  );
}
