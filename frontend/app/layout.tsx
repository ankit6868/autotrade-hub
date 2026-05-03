import type { Metadata, Viewport } from 'next';
import './globals.css';
import Sidebar from '@/components/ui/Sidebar';
import { AuthProvider, AuthGate, HeaderUser } from '@/components/AuthShell';
import AuthBridge from '@/components/AuthBridge';

export const metadata: Metadata = {
  title: 'AutoTrade Hub',
  description: 'AI-powered crypto trading platform — 100% free',
  applicationName: 'AutoTrade Hub',
  appleWebApp: {
    capable: true,
    title: 'AutoTrade Hub',
    statusBarStyle: 'black-translucent',
  },
  formatDetection: {
    telephone: false,
  },
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 5,
  viewportFit: 'cover',
  themeColor: '#060913',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <AuthProvider>
          <AuthBridge />
          <AuthGate>
            <Sidebar />
            <main
              className="
                min-h-screen
                md:ml-64
                pt-16 md:pt-0
                px-4 sm:px-6 lg:px-8
                pb-8
              "
            >
              <div className="hidden md:flex justify-end pt-6 mb-2">
                <HeaderUser />
              </div>
              <div className="md:hidden absolute top-3 right-4 z-50">
                <HeaderUser />
              </div>
              <div className="animate-fade-in">
                {children}
              </div>
            </main>
          </AuthGate>
        </AuthProvider>
      </body>
    </html>
  );
}
