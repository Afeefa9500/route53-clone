"use client";
import{useSearchParams}from"next/navigation";import Shell from"@/components/Shell";
export default function ComingSoon(){const p=useSearchParams();const name=p.get("p")||"This section";return <Shell><div className="crumbs">Route 53 › {name}</div><div className="coming"><div className="coming-icon">⌁</div><h1>{name}</h1><p>This section is mocked as permitted by the assignment.</p><span className="badge large">Coming soon</span></div></Shell>}
