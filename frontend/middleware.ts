import { clerkMiddleware, createRouteMatcher } from '@clerk/nextjs/server';
import { NextResponse } from 'next/server';

// Public routes that never require auth.
const isPublic = createRouteMatcher([
  '/sign-in(.*)',
  '/sign-up(.*)',
]);

const clerkEnabled = !!process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

export default clerkEnabled
  ? clerkMiddleware(async (auth, req) => {
      if (isPublic(req)) return;
      await auth.protect();
    })
  : () => NextResponse.next();

export const config = {
  matcher: [
    // Skip Next internals & static files; run on everything else including /api.
    '/((?!_next|.*\\..*).*)',
    '/(api|trpc)(.*)',
  ],
};
