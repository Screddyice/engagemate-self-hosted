import {describe,it,expect,vi,beforeEach} from "vitest";
import {mintExtensionTicket} from "@/lib/extension-ticket";
import {POST} from "@/app/api/accounts/auth-sync/route";
vi.mock("@/lib/account-limits",()=>({AccountLimitReached:class extends Error{},connectAccount:vi.fn(async input=>({account:{id:"account",username:input.username},created:true}))}));
function request(token:string,body:unknown) {return new Request("http://localhost:3030/api/accounts/auth-sync",{method:"POST",headers:{authorization:`Bearer ${token}`,"Content-Type":"application/json"},body:JSON.stringify(body)});}
describe("owner extension reconciliation",()=>{
 beforeEach(()=>vi.stubEnv("INTERNAL_API_KEY","local-test-signing-key"));
 it("rejects unverified identities",async()=>{expect((await POST(request("user-id",{username:"example"}))).status).toBe(401)});
 it("reconciles only a signed owner ticket",async()=>{const token=mintExtensionTicket("11111111-1111-4111-8111-111111111111");expect((await POST(request(token,{username:"example"}))).status).toBe(201)});
 it("refuses cookie capture on this identity-only route",async()=>{const token=mintExtensionTicket("11111111-1111-4111-8111-111111111111");expect((await POST(request(token,{username:"example",credentials:{sessionid:"synthetic"}}))).status).toBe(400)});
});
