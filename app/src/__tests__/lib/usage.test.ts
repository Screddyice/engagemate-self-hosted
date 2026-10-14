import {describe,expect,it} from "vitest";
import {summarizeUsage,eligibleAccountIds,usagePeriod} from "@/lib/usage";
describe("local owner receipts",()=>{
 it("keeps completed and reserved action counts separate",()=>{expect(summarizeUsage({included:100,billable:5,reserved:2})).toMatchObject({billable:5,reserved:2,projectedOverageCents:0});});
 it("has no paid account tiers",()=>{const accounts=[{id:"a",createdAt:new Date()},{id:"b",createdAt:new Date()}];expect(eligibleAccountIds(accounts,"starter").size).toBe(2);});
 it("uses the UTC calendar month",()=>{expect(usagePeriod({} as never,new Date("2026-08-15T00:00:00Z"))).toEqual({start:new Date("2026-08-01T00:00:00Z"),end:new Date("2026-09-01T00:00:00Z")});});
});
