import { z } from "zod";
import { tokenResponseSchema } from "./contracts";

function env() {
  return {
    mode: process.env.NEXT_PUBLIC_SCHOOLSIFT_AUTH_MODE ?? "local",
    domain: process.env.NEXT_PUBLIC_SCHOOLSIFT_COGNITO_DOMAIN ?? "",
    clientId: process.env.NEXT_PUBLIC_SCHOOLSIFT_COGNITO_CLIENT_ID ?? "",
    redirectUri:
      process.env.NEXT_PUBLIC_SCHOOLSIFT_COGNITO_REDIRECT_URI ?? "",
  };
}

const VERIFIER_KEY = "schoolsift.pkce-verifier";
const STATE_KEY = "schoolsift.oauth-state";
const ID_TOKEN_KEY = "schoolsift.id-token";
export const ACTIVE_HOUSEHOLD_KEY = "schoolsift.active-household";

export class AuthError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "AuthError";
  }
}

function randomBase64Url(bytes: number): string {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return btoa(String.fromCharCode(...buf))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

async function sha256Base64Url(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return btoa(String.fromCharCode(...new Uint8Array(digest)))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

export function cognitoConfigured(): boolean {
  const { mode, domain, clientId, redirectUri } = env();
  return (
    mode === "cognito" &&
    domain !== "" &&
    clientId !== "" &&
    redirectUri !== ""
  );
}

export async function startSignIn(): Promise<string> {
  const { domain, clientId, redirectUri } = env();
  if (!cognitoConfigured()) {
    throw new AuthError("Sign-in is not configured.");
  }
  const verifier = randomBase64Url(48);
  const state = randomBase64Url(24);
  sessionStorage.setItem(VERIFIER_KEY, verifier);
  sessionStorage.setItem(STATE_KEY, state);
  const challenge = await sha256Base64Url(verifier);
  const params = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: "openid email profile",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  return `${domain}/oauth2/authorize?${params}`;
}

export async function handleCallback(
  searchParams: URLSearchParams,
): Promise<void> {
  const { domain, clientId, redirectUri } = env();
  const error = searchParams.get("error");
  if (error !== null) throw new AuthError("Sign-in was not completed.");
  const state = searchParams.get("state");
  const code = searchParams.get("code");
  const expectedState = sessionStorage.getItem(STATE_KEY);
  const verifier = sessionStorage.getItem(VERIFIER_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);
  if (
    state === null ||
    expectedState === null ||
    state !== expectedState ||
    code === null ||
    verifier === null
  ) {
    throw new AuthError("Sign-in could not be verified. Try again.");
  }
  const res = await fetch(`${domain}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      client_id: clientId,
      redirect_uri: redirectUri,
      code,
      code_verifier: verifier,
    }),
  });
  if (!res.ok) throw new AuthError("Sign-in could not be completed.");
  const parsed = tokenResponseSchema.safeParse(
    await res.json().catch(() => null),
  );
  if (!parsed.success) {
    throw new AuthError("Sign-in could not be completed.");
  }
  sessionStorage.setItem(ID_TOKEN_KEY, parsed.data.id_token);
}

export function getIdToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(ID_TOKEN_KEY);
}

export function getActiveHousehold(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(ACTIVE_HOUSEHOLD_KEY);
}

export function setActiveHousehold(householdId: string | null): void {
  if (householdId === null) {
    sessionStorage.removeItem(ACTIVE_HOUSEHOLD_KEY);
  } else {
    sessionStorage.setItem(ACTIVE_HOUSEHOLD_KEY, householdId);
  }
}

export function signOutUrl(): string {
  const { domain, clientId, redirectUri } = env();
  const params = new URLSearchParams({
    client_id: clientId,
    logout_uri: redirectUri.replace(/\/auth\/callback$/, "/"),
  });
  return `${domain}/logout?${params}`;
}

export function clearSession(): void {
  sessionStorage.removeItem(ID_TOKEN_KEY);
  sessionStorage.removeItem(STATE_KEY);
  sessionStorage.removeItem(VERIFIER_KEY);
  sessionStorage.removeItem(ACTIVE_HOUSEHOLD_KEY);
}

export type TokenResponse = z.infer<typeof tokenResponseSchema>;
