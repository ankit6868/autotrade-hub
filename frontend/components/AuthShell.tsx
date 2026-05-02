'use client';

import { ClerkProvider, SignInButton, UserButton, useAuth } from '@clerk/nextjs';

const PUB_KEY = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

export function AuthProvider({ children }: { children: React.ReactNode }) {
  if (!PUB_KEY) return <>{children}</>;
  return <ClerkProvider>{children}</ClerkProvider>;
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
        <SignInButton mode="modal">
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
  return <GateInner>{children}</GateInner>;
}

function HeaderUserInner() {
  const { isLoaded, isSignedIn } = useAuth();
  if (!isLoaded || !isSignedIn) return null;
  return <UserButton />;
}

export function HeaderUser() {
  if (!PUB_KEY) return null;
  return <HeaderUserInner />;
}
