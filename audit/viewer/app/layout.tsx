import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "openclaw audit viewer",
  description: "Read-only audit log viewer — mTLS gated, tailnet only",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav>
          <strong>openclaw audit</strong>
          <a href="/">Recent</a>
          <a href="/verify">Verify</a>
        </nav>
        {children}
      </body>
    </html>
  );
}
