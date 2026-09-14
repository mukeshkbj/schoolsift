"use client";

import { useState } from "react";
import { AuthError, startSignIn } from "../lib/auth";

export default function SignIn() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSignIn() {
    setBusy(true);
    setError(null);
    try {
      window.location.assign(await startSignIn());
    } catch (e) {
      setError(
        e instanceof AuthError ? e.message : "Sign-in could not be started.",
      );
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="btn btn-primary btn-hero"
        onClick={() => void onSignIn()}
        disabled={busy}
        aria-busy={busy}
      >
        Sign in
        {busy && <span className="btn-busy" aria-hidden="true" />}
      </button>
      {error !== null && (
        <p role="alert" className="app-error">
          {error}
        </p>
      )}
    </>
  );
}
