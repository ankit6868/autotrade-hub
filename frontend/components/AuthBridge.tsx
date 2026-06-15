'use client';

import { useEffect } from 'react';
import { useAuth, useUser } from '@clerk/nextjs';
import { setWsTokenProvider } from '@/lib/websocket';
import { setUserEmail } from '@/lib/api';

const PUB_KEY = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

export default function AuthBridge() {
  if (!PUB_KEY) return null;
  return <Inner />;
}

function Inner() {
  const { getToken } = useAuth();
  const { user } = useUser();
  useEffect(() => {
    // setWsTokenProvider sets BOTH the HTTP token (via api.ts) and WS token
    setWsTokenProvider(() => getToken());
    return () => setWsTokenProvider(null);
  }, [getToken]);
  useEffect(() => {
    // Push the Clerk-verified email to api.ts so it's sent as X-User-Email
    // (admin / unlimited-allowlist resolution). Cleared on sign-out.
    const email = user?.primaryEmailAddress?.emailAddress
      || user?.emailAddresses?.[0]?.emailAddress || null;
    setUserEmail(email ? email.toLowerCase() : null);
    return () => setUserEmail(null);
  }, [user]);
  return null;
}
