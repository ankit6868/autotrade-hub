'use client';

import { useEffect } from 'react';
import { useAuth } from '@clerk/nextjs';
import { setWsTokenProvider } from '@/lib/websocket';

const PUB_KEY = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

export default function AuthBridge() {
  if (!PUB_KEY) return null;
  return <Inner />;
}

function Inner() {
  const { getToken } = useAuth();
  useEffect(() => {
    // setWsTokenProvider sets BOTH the HTTP token (via api.ts) and WS token
    setWsTokenProvider(() => getToken());
    return () => setWsTokenProvider(null);
  }, [getToken]);
  return null;
}
