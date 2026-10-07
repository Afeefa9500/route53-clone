import "./globals.css";
import { Providers } from "@/components/Providers";
export const metadata={title:"Route 53 Management Console",description:"AWS Route 53 clone"};
export default function Root({children}:{children:React.ReactNode}){return <html lang="en"><body><Providers>{children}</Providers></body></html>}
