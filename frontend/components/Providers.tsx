"use client";
import {createContext,useCallback,useContext,useEffect,useState} from "react";
type Toast={id:number;msg:string;err?:boolean};
const C=createContext<{toast:(m:string,e?:boolean)=>void;theme:string;toggleTheme:()=>void}>({toast:()=>{},theme:"light",toggleTheme:()=>{}});
export const useApp=()=>useContext(C);
export function Providers({children}:{children:React.ReactNode}){const[toastList,setToastList]=useState<Toast[]>([]);const[theme,setTheme]=useState("light");
 useEffect(()=>{const t=localStorage.getItem("r53_theme")||"light";setTheme(t);document.documentElement.dataset.theme=t},[]);
 const toast=useCallback((msg:string,err=false)=>{const id=Date.now()+Math.random();setToastList(x=>[...x,{id,msg,err}]);setTimeout(()=>setToastList(x=>x.filter(t=>t.id!==id)),4500)},[]);
 const toggleTheme=()=>{const t=theme==="light"?"dark":"light";setTheme(t);localStorage.setItem("r53_theme",t);document.documentElement.dataset.theme=t};
 return <C.Provider value={{toast,theme,toggleTheme}}>{children}<div className="toasts">{toastList.map(t=><div className={`toast ${t.err?"err":""}`} key={t.id}>{t.msg}</div>)}</div></C.Provider>}
