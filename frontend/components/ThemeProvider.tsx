"use client";
import { createContext, useContext, useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { api, Theme } from "@/lib/api";
const ThemeContext=createContext<{theme:Theme;setTheme:(theme:Theme)=>void}>({theme:'system',setTheme:()=>{}});
export function ThemeProvider({children}:{children:React.ReactNode}) {
  const [theme,setValue]=useState<Theme>('system');
  // The first render must match the server (which cannot read localStorage), so it starts with 'system'. Until the stored value has been read
  // nothing may be applied: the inline script in app/layout.tsx has already set data-theme correctly, and applying 'system' here would flash
  // the other theme for one frame whenever the stored choice differs from the operating system's.
  const [loaded,setLoaded]=useState(false);
  const login=usePathname()==='/login';
  function setTheme(value:Theme){setValue(value);try{localStorage.setItem('opnexus-theme',value);}catch{} }
  useEffect(()=>{try{const value=localStorage.getItem('opnexus-theme');if(value==='light'||value==='dark'||value==='system')setValue(value);}catch{}setLoaded(true);},[]);
  useEffect(()=>{if(login)return;let active=true;api.me().then(user=>user.authenticated?api.settings():null).then(data=>{if(active&&data)setTheme(data.theme);}).catch(()=>{});return()=>{active=false;};},[login]);
  useEffect(()=>{
    const media=matchMedia('(prefers-color-scheme: dark)');
    const apply=()=>{const resolved=theme==='system'?(media.matches?'dark':'light'):theme;document.documentElement.dataset.theme=resolved;document.documentElement.style.colorScheme=resolved;};
    if(!loaded)return;
    apply();media.addEventListener('change',apply);
    const sync=(e:StorageEvent)=>{if(e.key==='opnexus-theme'&&(e.newValue==='light'||e.newValue==='dark'||e.newValue==='system'))setValue(e.newValue);};
    window.addEventListener('storage',sync);
    return()=>{media.removeEventListener('change',apply);window.removeEventListener('storage',sync);};
  },[theme,loaded]);
  return <ThemeContext.Provider value={{theme,setTheme}}>{children}</ThemeContext.Provider>;
}
export const useTheme=()=>useContext(ThemeContext);
