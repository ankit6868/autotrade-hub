import { SignUp } from '@clerk/nextjs';

export default function Page() {
  return (
    <div className="min-h-screen w-full flex items-center justify-center p-6">
      <SignUp routing="path" path="/sign-up" />
    </div>
  );
}
