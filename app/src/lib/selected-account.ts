import { cookies } from "next/headers";
import { getUserAccounts, type IgAccountRow } from "@/lib/queries";

export const SELECTED_ACCOUNT_COOKIE = "engagemate_ig_account";

export async function resolveSelectedAccount(userId: string): Promise<{
  accounts: IgAccountRow[];
  selected: IgAccountRow | null;
}> {
  const accounts = await getUserAccounts(userId);
  const requested = (await cookies()).get(SELECTED_ACCOUNT_COOKIE)?.value;
  const selected =
    accounts.find((account) => account.id === requested) ?? accounts[0] ?? null;
  return { accounts, selected };
}
