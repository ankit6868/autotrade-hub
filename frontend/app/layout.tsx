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
            {/* main content margin reflows with the sidebar's open/closed
                state. Sidebar.tsx sets data-sidebar-open on <body>; matching
                CSS in globals.css drives the desktop margin (16rem when open,
                0 when closed). Mobile keeps no margin (sidebar overlays). */}
            <main
              className="
                app-main relative z-10 min-h-screen
                pt-16 md:pt-0
                pb-8
                transition-[margin-left] duration-300 ease-out
              "
            >
              <div className="container-app px-3 xs:px-4 sm:px-6 lg:px-8 2xl:px-10">
                {/* Desktop only: account button (UserButton) at top right.
                    On mobile the floating button collided with the hamburger
                    icon, so the sign-out action was moved into the sidebar
                    footer (see Sidebar.tsx → SidebarSignOut). */}
                <div className="hidden md:flex justify-end pt-6 mb-2">
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
