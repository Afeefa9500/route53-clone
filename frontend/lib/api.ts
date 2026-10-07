export const API = "";
export type Zone = { id:string; name:string; type:"public"|"private"; comment:string; vpc_id:string|null; record_count:number; created_at:number };
export type Rec = { id:number; zone_id:string; name:string; type:string; ttl:number; value:string; routing_policy:string; created_at?:number };
export type Page<T> = { items:T[]; total:number; page:number; page_size:number };
export const RECORD_TYPES=["A","AAAA","CNAME","TXT","MX","NS","PTR","SRV","CAA"];
export async function api<T=any>(path:string,opts:RequestInit={}):Promise<T>{
 const token=typeof window!=="undefined"?localStorage.getItem("r53_token"):null;
 const res=await fetch(API+path,{...opts,headers:{"Content-Type":"application/json",...(token?{Authorization:`Bearer ${token}`}:{ }),...(opts.headers||{})}});
 if(res.status===401&&!path.includes("/auth/login")){if(typeof window!=="undefined"){localStorage.removeItem("r53_token");window.location.href="/login";}throw new Error("Session expired.");}
 const data=await res.json().catch(()=>({}));
 if(!res.ok) throw new Error(typeof data.detail==="string"?data.detail:data.detail?.[0]?.msg||"Request failed");
 return data;
}
