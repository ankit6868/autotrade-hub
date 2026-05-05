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

// Custom dropdown that uses useClerk().signOut() directly, with an explicit
// hard-redirect to /sign-in. We use this instead of <UserButton/>'s built-in
// "Sign out" item because the popover's React tree is unmounted the instant
// the session is destroyed (since AuthGate flips to SignedOutLanding), which
// can interrupt UserButton's internal navigation. Calling signOut with
// redirectUrl gives Clerk a chance to do a hard window.location redirect
// before any tree unmounts.
function HeaderUserInner() {
  const { isLoaded, isSignedIn } = useAuth();
  const clerk = useClerk();
  if (!isLoaded || !isSignedIn) return null;

  const handleSignOut = async () => {
    try {
      await clerk.signOut({ redirectUrl: AFTER_SIGN_OUT_URL });
    } catch {
      // Fall back to a hard navigation if Clerk's redirect fails for any
      // reason (network, racing component unmount, etc.).
      window.location.href = AFTER_SIGN_OUT_URL;
    }
  };

  return (
    <div className="flex items-center gap-2">
      <UserButton showName={false} />
      <button
        type="button"
        onClick={handleSignOut}
        className="text-xs text-slate-400 hover:text-slate-200 underline-offset-2 hover:underline"
        aria-label="Sign out"
      >
        Sign out
      </button>
    </div>
  );
}

export function HeaderUser() {
  if (!PUB_KEY) return null;
  return <HeaderUserInner />;
}
