import { clerkMiddleware, createRouteMatcher } from '@clerk/nextjs/server';
import { NextResponse } from 'next/server';

// Public routes that never require Clerk auth at the edge.
//
// /api/* is forwarded to the FastAPI backend via Next.js rewrites
// (see next.config.js). The backend validates the Clerk JWT on its own,
// so the edge middleware must let those requests through unchanged.
const isPublic = createRouteMatcher([
  '/sign-in(.*)',
  '/sign-up(.*)',
  '/api/(.*)',
  '/ws/(.*)',
]);

const clerkEnabled = !!process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY;

export default clerkEnabled
  ? clerkMiddleware(async (auth, req) => {
      if (isPublic(req)) return;

      // Clerk v7's `auth.protect()` returns 404 for signed-out users.
      // We want them sent to /sign-in instead, so check the userId
      // explicitly and call redirectToSignIn() when missing.
      const { userId, redirectToSignIn } = await auth();
      if (!userId) {
        return redirectToSignIn({ returnBackUrl: req.url });
      }
    })
  : () => NextResponse.next();

export const config = {
  matcher: [
    // Skip Next internals & static files; run on everything else including /api.
    '/((?!_next|.*\\..*).*)',
    '/(api|trpc)(.*)',
  ],
};
