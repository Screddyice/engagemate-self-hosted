"use client";

import Link from "next/link";
import { Card } from "@/components/ui/card";

const platforms = [
  {
    id: "instagram",
    name: "Instagram",
    description: "Likes and Story replies from active Stories",
    available: true,
  },
];

export default function ConnectAccountPage() {
  return (
    <div>
      <h1 className="text-2xl font-bold">Connect Instagram</h1>
      <p className="mt-1 text-muted-foreground">
        Connect the Instagram account EngageMate will use for Story engagement.
      </p>

      <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {platforms.map((p) => {
          const card = (
            <Card
              className={`p-6 transition-colors ${
                p.available
                  ? "hover:border-primary"
                  : "cursor-not-allowed opacity-50"
              }`}
            >
              <h3 className="font-semibold">{p.name}</h3>
              <p className="mt-2 text-sm text-muted-foreground">
                {p.description}
              </p>
              {!p.available && (
                <span className="mt-3 inline-block rounded-full bg-muted px-2 py-1 text-xs">
                  Coming soon
                </span>
              )}
            </Card>
          );

          return p.available ? (
            <Link
              key={p.id}
              href={`/accounts/connect/${p.id}`}
              className="rounded-xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
            >
              {card}
            </Link>
          ) : (
            <div key={p.id}>{card}</div>
          );
        })}
      </div>
    </div>
  );
}
