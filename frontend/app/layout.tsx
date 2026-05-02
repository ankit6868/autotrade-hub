import type { Metadata } from 'next';
import './globals.css';
import Sidebar from '@/components/ui/Sidebar';
import { AuthProvider, AuthGate, HeaderUser } from '@/components/AuthShell';
import AuthBridge from '@/components/AuthBridge';

export const metadata: Metadata = {
  title: 'AutoTrade Hub',
  description: 'AI-powered crypto trading platform — 100% free',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen flex">
        <AuthProvider>
          <AuthBridge />
          <AuthGate>
            <Sidebar />
            <main className="flex-1 ml-64 p-8 overflow-auto">
              <div className="flex justify-end mb-4">
                <HeaderUser />
              </div>
              {children}
            </main>
          </AuthGate>
        </AuthProvider>
      </body>
    </html>
  );
}
