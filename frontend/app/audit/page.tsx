"use client";
import { InfoTip, Tooltip } from "@/components/ui/Tooltip";
import { useEffect, useState } from "react";
import { api, AuditEvent, AuditPage } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { Input, Select } from "@/components/ui/Field";
import { useLanguage } from "@/components/LanguageProvider";
const kindKeys = ["rule","alias","port_forward","one_to_one","outbound_nat","host","user","auth","backup","ipsec_tunnel","openvpn_instance","wireguard_tunnel","certificate","acme","notification"];
const actionKeys = ["deploy","confirm","rollback","remove","retry-rollback","delete","create","edit","reboot","update"];
const statusKeys = ["requested","accepted","rejected","interrupted","success","pending","error","reverted","revert_failed","removed","submitted","acknowledged","starting","unknown","denied"];
export default function AuditHistory() {
  const {t,ts,errorMessage,locale}=useLanguage();
  const lbl=(ns:string,key:string)=>{const k=`audit.${ns}.${key}`;const v=t(k);return v===k?key:v;};
  const kinds:Record<string,string>=Object.fromEntries(kindKeys.map(k=>[k,lbl("kind",k)]));
  const actions:Record<string,string>=Object.fromEntries(actionKeys.map(k=>[k,lbl("action",k)]));
  const statuses:Record<string,string>=Object.fromEntries(statusKeys.map(k=>[k,lbl("status",k)]));
  const [data,setData]=useState<AuditPage | null>(null);
  const [kind,setKind]=useState("");const [status,setStatus]=useState("");const [search,setSearch]=useState("");
  const [offset,setOffset]=useState(0);const [error,setError]=useState("");const [reload,setReload]=useState(0);const [exporting,setExporting]=useState(false);
  const [before,setBefore]=useState(()=>new Date().toISOString());
  const [selected,setSelected]=useState<AuditEvent | null>(null);
  useEffect(()=>{let active=true;setData(null);setError("");const timer=setTimeout(()=>{api.audit({kind,status,q:search,offset,limit:50,before}).then(d=>{if(active)setData(d);}).catch(e=>{if(active)setError(errorMessage(e));});},200);return()=>{active=false;clearTimeout(timer);};},[kind,status,search,offset,reload,before]);
  async function download() {
    setExporting(true);setError("");
    try { const all: AuditEvent[]=[];let cursor=0;while(true){const d=await api.audit({kind,status,q:search,offset:cursor,limit:200,before});all.push(...d.items);cursor+=d.items.length;if(cursor>=d.total || !d.items.length)break;}
      const url=URL.createObjectURL(new Blob([JSON.stringify({exported_at:new Date().toISOString(),through:before,events:all},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='opnexus-audit.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }catch(e){setError(errorMessage(e,'audit.exportFailed'));}finally{setExporting(false);}
  }
  return <div className="space-y-5"><div className="flex flex-wrap justify-between gap-3"><h1 className="flex items-center gap-2 text-2xl font-semibold">{t("audit.title")}<InfoTip text={t("audit.intro")} size={18} /></h1><div className="flex gap-2"><Button variant="ghost" onClick={()=>{setBefore(new Date().toISOString());setOffset(0);setReload(n=>n+1);}}>{t("audit.refresh")}</Button><Button disabled={exporting} onClick={download}>{exporting?t('audit.exporting'):t('audit.exportJson')}</Button></div></div><div className="grid gap-3 sm:grid-cols-3"><Input aria-label={t("audit.searchLabel")} placeholder={t("audit.searchPlaceholder")} value={search} onChange={e=>{setSearch(e.target.value);setOffset(0);}}/><Select aria-label={t("audit.typeLabel")} value={kind} onChange={e=>{setKind(e.target.value);setOffset(0);}}><option value="">{t("audit.allTypes")}</option>{Object.entries(kinds).map(([v,l])=><option key={v} value={v}>{l}</option>)}</Select><Select aria-label={t("audit.resultLabel")} value={status} onChange={e=>{setStatus(e.target.value);setOffset(0);}}><option value="">{t("audit.allResults")}</option>{Object.entries(statuses).map(([v,l])=><option key={v} value={v}>{l}</option>)}</Select></div>{error && <p role="alert" className="text-red-600">{error}</p>}{!data && !error && <p>{t("common.loading")}</p>}{data && <><p className="text-sm text-slate-500">{t("audit.events",{total:data.total,from:data.total?offset+1:0,to:Math.min(offset+50,data.total)})}</p><div className="space-y-2">{data.items.map(e=><button key={e.id} onClick={()=>setSelected(selected?.id===e.id?null:e)} className="block w-full rounded-xl border border-slate-200 bg-white p-4 text-left text-sm"><div className="flex flex-wrap justify-between gap-2"><strong>{actions[e.action]??e.action} · {e.entity_name || kinds[e.kind] || e.kind}{e.host_name?` · ${e.host_name}`:''}</strong><span>{statuses[e.status]??e.status}</span></div><p className="mt-1 text-slate-500">{new Date(e.created_at).toLocaleString(locale)} · {e.actor}</p>{e.message && <p className="mt-1 break-words text-slate-600">{ts(e.message)}</p>}{selected?.id===e.id && <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-50 p-3 text-xs">{JSON.stringify({id:e.id,correlation_id:e.correlation_id,entity_id:e.entity_id,host_id:e.host_id,...e.details},null,2)}</pre>}</button>)}{!data.items.length && <p className="py-8 text-center text-slate-500">{t("audit.noEvents")}</p>}</div><div className="flex gap-2"><Button variant="ghost" disabled={!offset} onClick={()=>setOffset(n=>Math.max(0,n-50))}>{t("logs.back")}</Button><Button variant="ghost" disabled={offset+50>=data.total} onClick={()=>setOffset(n=>n+50)}>{t("logs.next")}</Button></div></>}</div>;
}
