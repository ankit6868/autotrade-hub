'use client';

import {
  ClerkProvider,
  SignInButton,
  UserButton,
  useAuth,
  useClerk,
  ClerkLoaded,
  ClerkLoading,
} from '@clerk/nextjs';

const PUB_KEY = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

// Where to send users after they click "Sign out".
// Must be a public route (the middleware allows /sign-in without auth).
const AFTER_SIGN_OUT_URL = '/sign-in';

export function AuthProvider({ children }: { children: React.ReactNode }) {
  if (!PUB_KEY) return <>{children}</>;
  return (
    <ClerkProvider
      afterSignOutUrl={AFTER_SIGN_OUT_URL}
      signInUrl="/sign-in"
      signUpUrl="/sign-up"
      signInFallbackRedirectUrl="/"
      signUpFallbackRedirectUrl="/setup"
    >
      {children}
    </ClerkProvider>
  );
}

function SignedOutLanding() {
  return (
    <div className="min-h-screen w-full flex items-center justify-center p-8">
      <div className="max-w-sm w-full text-center space-y-6 p-8 bg-[#111827] rounded-2xl border border-[#2a3a52]">
        <div>
          <h1 className="text-2xl font-bold text-white">AutoTrade Hub</h1>
          <p className="text-sm text-slate-400 mt-2">
            Sign in to access your strategies, backtests, and live trading.
          </p>
        </div>
        <SignInButton mode="modal" forceRedirectUrl="/">
          <button className="btn-primary w-full">Sign in / Sign up</button>
        </SignInButton>
      </div>
    </div>
  );
}

function GateInner({ children }: { children: React.ReactNode }) {
  const { isLoaded, isSignedIn } = useAuth();
  if (!isLoaded) {
    return (
      <div className="min-h-screen w-full flex items-center justify-center text-slate-400 text-sm">
        Loading…
      </div>
    );
  }
  if (!isSignedIn) return <SignedOutLanding />;
  return <>{children}</>;
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  if (!PUB_KEY) return <>{children}</>;
  return (
    <>
      <ClerkLoading>
        <div className="min-h-screen w-full flex items-center justify-center text-slate-400 text-sm">
          Loading…
        </div>
      </ClerkLoading>
      <ClerkLoaded>
        <GateInner>{children}</GateInner>
      </ClerkLoaded>
    </>
  );
}

function HeaderUserInner() {
  const { isLoaded, isSignedIn } = useAuth();
  if (!isLoaded || !isSignedIn) return null;
  // afterSignOutUrl is set on ClerkProvider above (v7), which makes the
  // built-in "Sign out" item in the UserButton popover redirect properly.
  return <UserButton />;
}

export function HeaderUser() {
  if (!PUB_KEY) return null;
  return <HeaderUserInner />;
}

/**
 * Standalone "Sign out" button suitable for nav drawers / sidebars.
 *
 * Used by the mobile sidebar (where <UserButton/> would otherwise sit
 * behind the hamburger menu icon and be unreachable). Calls
 * useClerk().signOut() with an explicit redirectUrl so the redirect
 * fires before any React tree unmounts can interrupt it. Falls back
 * to a hard window.location navigation if signOut() rejects.
 */
function SidebarSignOutInner() {
  const { isLoaded, isSignedIn } = useAuth();
  const clerk = useClerk();
  if (!isLoaded || !isSignedIn) return null;

  const handleSignOut = async () => {
    try {
      await clerk.signOut({ redirectUrl: AFTER_SIGN_OUT_URL });
    } catch {
      window.location.href = AFTER_SIGN_OUT_URL;
    }
  };

  return (
    <button
      type="button"
      onClick={handleSignOut}
      className="
        w-full flex items-center justify-center gap-2
        px-3 py-2.5 rounded-xl text-sm font-medium
        text-slate-200 bg-white/[0.04] hover:bg-white/[0.08]
        active:scale-[0.98] transition
        border border-white/[0.06]
      "
      aria-label="Sign out"
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
        <polyline points="16 17 21 12 16 7" />
        <line x1="21" y1="12" x2="9" y2="12" />
      </svg>
      <span>Sign out</span>
    </button>
  );
}

export function SidebarSignOut() {
  if (!PUB_KEY) return null;
  return <SidebarSignOutInner />;
}
