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
  themeColor: '#05070f',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen overflow-x-hidden">
        {/* Decorative ambient orbs — sit below all content */}
        <div aria-hidden="true" className="pointer-events-none fixed inset-0 z-0 overflow-hidden">
          <div className="absolute -top-40 -left-40 h-[40rem] w-[40rem] rounded-full bg-brand-600/20 blur-[140px]" />
          <div className="absolute top-1/3 -right-40 h-[36rem] w-[36rem] rounded-full bg-violet-500/15 blur-[140px]" />
          <div className="absolute bottom-0 left-1/3 h-[32rem] w-[32rem] rounded-full bg-cyan-500/10 blur-[140px]" />
        </div>

        <AuthProvider>
          <AuthBridge />
          <AuthGate>
            <Sidebar />
            <main
              className="
                relative z-10 min-h-screen
                md:ml-64
                pt-16 md:pt-0
                pb-8
              "
            >
              <div className="container-app px-3 xs:px-4 sm:px-6 lg:px-8 2xl:px-10">
                <div className="hidden md:flex justify-end pt-6 mb-2">
                  <HeaderUser />
                </div>
                <div className="md:hidden absolute top-3 right-4 z-50">
                  <HeaderUser />
                </div>
                <div className="animate-fade-in">
                  {children}
                </div>
              </div>
            </main>
          </AuthGate>
        </AuthProvider>
      </body>
    </html>
  );
}
