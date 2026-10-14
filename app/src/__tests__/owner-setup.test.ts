import {setupDestination} from "@/lib/setup-destination";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { webcrypto } from "node:crypto";
import { NextRequest } from "next/server";
import { POST } from "@/app/api/setup/route";
import middleware from "@/middleware";
import { OWNER_COOKIE, ownerSessionValue, validOwnerSession } from "@/lib/owner-session";

beforeEach(()=>{vi.stubGlobal("crypto",webcrypto);vi.stubEnv("OWNER_ACCESS_TOKEN","x".repeat(48));vi.stubEnv("NEXT_PUBLIC_APP_URL","http://localhost:3030");});
describe("private owner setup",()=>{
 it("requires the generated setup key and issues a private session",async()=>{
  const req=(token:string)=>new Request("http://localhost:3030/api/setup",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token})});
  expect((await POST(req("wrong"))).status).toBe(401);
  const response=await POST(req("x".repeat(48)));expect(response.status).toBe(200);
  const cookie=response.cookies.get(OWNER_COOKIE);expect(cookie?.httpOnly).toBe(true);expect(cookie?.sameSite).toBe("strict");
  expect(await validOwnerSession(cookie?.value)).toBe(true);
  expect(cookie?.value).not.toContain("x".repeat(48));
 });
 it("rejects tampering, expiration and key rotation",async()=>{
  const value=await ownerSessionValue();expect(await validOwnerSession(value!+"a")).toBe(false);
  expect(await validOwnerSession((await ownerSessionValue(Date.now()-31*86400000))!)).toBe(false);
  vi.stubEnv("OWNER_ACCESS_TOKEN","y".repeat(48));expect(await validOwnerSession(value!)).toBe(false);
 });
 it("rejects foreign hosts and cross-origin writes before owner access",async()=>{
  expect((await middleware(new NextRequest("http://localhost:3030/dashboard",{headers:{host:"evil.example"}}))).status).toBe(403);
  expect((await middleware(new NextRequest("http://localhost:3030/api/setup",{method:"POST",headers:{host:"localhost:3030",origin:"https://evil.example"}}))).status).toBe(403);
  expect((await middleware(new NextRequest("http://localhost:3030/api/accounts",{headers:{host:"localhost:3030"}}))).status).toBe(401);
 });
});

it("keeps setup return navigation on the owner's own origin",()=>{
 const origin="http://localhost:3030";
 expect(setupDestination("/mcp/authorize?state=synthetic",origin)).toBe("/mcp/authorize?state=synthetic");
 for(const path of ["//external.example/","/\\external.example/","https://external.example/",null]) expect(setupDestination(path,origin)).toBe("/onboarding");
});
