export interface ConnectableAccount {
  id: string;
  username: string;
  healthStatus?: string | null;
}

export function isHealthyAccount(account: ConnectableAccount): boolean {
  return (account.healthStatus ?? "active").toLowerCase() === "active";
}

export function healthyAccount(
  accounts: ConnectableAccount[],
): ConnectableAccount | undefined {
  return accounts.find(isHealthyAccount);
}
