"use client";
import { useEffect, useState } from "react";
import { api, Host } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { useLanguage } from "@/components/LanguageProvider";
export function OutboundModes({hosts}: {hosts: Host[]}) {
  const {t}=useLanguage();
  const [rows,setRows]=useState<{name:string;mode:string|null;allowed:boolean}[]>([]);
  const [reload,setReload]=useState(0);
  const key=hosts.filter(h=>h.enabled).map(h=>h.id).sort().join(',');
  useEffect(()=>{let active=true;setRows([]);Promise.all(hosts.filter(h=>h.enabled).map(async h=>{try{const m=await api.getOutboundMode(h.id);return {name:h.name,mode:m.mode,allowed:m.can_deploy};}catch{return {name:h.name,mode:null,allowed:false};}})).then(r=>{if(active)setRows(r);});return()=>{active=false;};},[key,reload]);
  return <div className="mb-4 space-y-2 rounded-xl border border-slate-200 p-3 text-sm"><div className="flex items-center justify-between gap-2"><strong>{t('outboundModes.title')}</strong><Button variant="ghost" onClick={()=>setReload(n=>n+1)}>{t('outboundModes.refresh')}</Button></div>{!rows.length && key && <p>{t('common.loading')}</p>}{rows.map(r=><p key={r.name} className={r.allowed?'text-emerald-700':'text-flame-700'}>{r.name}: {r.mode ?? t('outboundModes.unavailable')}{r.allowed?'':t('outboundModes.pushBlocked')}</p>)}<p className="text-slate-500">{t('outboundModes.hint')}</p></div>;
}
