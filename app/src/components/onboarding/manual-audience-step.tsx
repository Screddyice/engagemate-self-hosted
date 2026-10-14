"use client";
import {useState} from "react";
import {Button} from "@/components/ui/button";
export function ManualAudienceStep({accountId,onSaved}:{accountId?:string;onSaved:(persona:string)=>void}) {
 const [urls,setUrls]=useState("");const [persona,setPersona]=useState("");const [approved,setApproved]=useState(false);const [busy,setBusy]=useState(false);const [error,setError]=useState("");
 return <form className="mt-6 space-y-4 rounded border p-5" onSubmit={async e=>{e.preventDefault();setBusy(true);setError("");try{
  const response=await fetch("/api/onboarding/manual",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ig_account_id:accountId,reference_urls:urls.split(/\n/).map(x=>x.trim()).filter(Boolean),persona:persona.trim(),approve:approved})});
  const body=await response.json();if(!response.ok)throw Error(typeof body.detail==="string" ? body.detail : body.error || "Could not save source audience");onSaved(persona.trim());
 }catch(e){setError(e instanceof Error ? e.message : "Setup failed")}finally{setBusy(false)}}}>
  <h3 className="text-lg font-semibold">Choose your own source audience</h3><p className="text-sm text-muted-foreground">Use this path without profile retrieval or model analysis. The worker discovers public followers of the accounts you explicitly approve.</p>
  <label className="block text-sm">Three to five inspiration account URLs<textarea required value={urls} onChange={e=>setUrls(e.target.value)} placeholder="https://www.instagram.com/example/" className="mt-2 block min-h-28 w-full rounded border bg-background p-3" /></label>
  <label className="block text-sm">Your voice and reply instructions<textarea required maxLength={2000} value={persona} onChange={e=>setPersona(e.target.value)} className="mt-2 block min-h-28 w-full rounded border bg-background p-3" /></label>
  <label className="flex gap-2 text-sm"><input required type="checkbox" checked={approved} onChange={e=>setApproved(e.target.checked)} />I approve public followers of these inspiration accounts as this account&apos;s source audience.</label>
  {error && <p role="alert" className="text-red-600">{error}</p>}<Button disabled={busy || !accountId}>{busy ? "Saving…" : "Save my audience and voice"}</Button>
 </form>;
}
