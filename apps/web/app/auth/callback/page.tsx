"use client";

import { useRouter } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { AuthError, handleCallback } from "../../../lib/auth";

function CallbackInner() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    handleCallback(params)
      .then(() => router.replace("/app"))
      .catch((e: unknown) => {
        setError(
          e instanceof AuthError
            ? e.message
            : "Sign-in could not be completed.",
        );
      });
  }, [router]);

  return (
    <main className="landing">
      <div className="landing-inner">
        {error === null ? (
          <p className="app-status" role="status">
            Completing sign-in…
          </p>
        ) : (
          <>
            <p role="alert" className="app-error">
              {error}
            </p>
            <a className="btn btn-secondary" href="/">
              Back to sign in
            </a>
          </>
        )}
      </div>
    </main>
  );
}

export default function AuthCallbackPage() {
  return (
    <Suspense>
      <CallbackInner />
    </Suspense>
  );
}
