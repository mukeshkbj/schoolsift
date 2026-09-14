import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ACTIVE_HOUSEHOLD_KEY,
  AuthError,
  clearSession,
  getActiveHousehold,
  handleCallback,
  setActiveHousehold,
  startSignIn,
} from "../lib/auth";

process.env.NEXT_PUBLIC_SCHOOLSIFT_AUTH_MODE = "cognito";
process.env.NEXT_PUBLIC_SCHOOLSIFT_COGNITO_DOMAIN = "https://auth.example.com";
process.env.NEXT_PUBLIC_SCHOOLSIFT_COGNITO_CLIENT_ID = "client-1";
process.env.NEXT_PUBLIC_SCHOOLSIFT_COGNITO_REDIRECT_URI =
  "https://app.example.com/auth/callback";

function mockCrypto() {
  vi.stubGlobal("crypto", {
    getRandomValues: (buf: Uint8Array) => {
      buf.fill(7);
      return buf;
    },
    subtle: {
      digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(9).buffer),
    },
  });
}

describe("pkce sign-in", () => {
  beforeEach(() => {
    sessionStorage.clear();
    vi.restoreAllMocks();
    mockCrypto();
  });

  it("builds an authorize url with S256 challenge and stores state", async () => {
    const url = await startSignIn();
    const parsed = new URL(url);
    expect(parsed.origin).toBe("https://auth.example.com");
    expect(parsed.pathname).toBe("/oauth2/authorize");
    expect(parsed.searchParams.get("response_type")).toBe("code");
    expect(parsed.searchParams.get("client_id")).toBe("client-1");
    expect(parsed.searchParams.get("code_challenge_method")).toBe("S256");
    expect(parsed.searchParams.get("code_challenge")).toBeTruthy();
    expect(sessionStorage.getItem("schoolsift.pkce-verifier")).toBeTruthy();
    expect(sessionStorage.getItem("schoolsift.oauth-state")).toBe(
      parsed.searchParams.get("state"),
    );
  });

  it("callback rejects state mismatch", async () => {
    await startSignIn();
    const params = new URLSearchParams({ state: "forged", code: "c" });
    await expect(handleCallback(params)).rejects.toBeInstanceOf(AuthError);
    expect(sessionStorage.getItem("schoolsift.id-token")).toBeNull();
  });

  it("callback exchanges code and stores only the id token", async () => {
    await startSignIn();
    const state = sessionStorage.getItem("schoolsift.oauth-state")!;
    const verifier = sessionStorage.getItem("schoolsift.pkce-verifier")!;
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        id_token: "id-tok",
        access_token: "acc",
        token_type: "Bearer",
        expires_in: 3600,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const params = new URLSearchParams({ state, code: "the-code" });
    await handleCallback(params);
    expect(sessionStorage.getItem("schoolsift.id-token")).toBe("id-tok");
    expect(sessionStorage.getItem("schoolsift.access-token")).toBeNull();
    const body = fetchMock.mock.calls[0][1].body as URLSearchParams;
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("code")).toBe("the-code");
    expect(body.get("code_verifier")).toBe(verifier);
  });

  it("callback fails safely on token endpoint failure", async () => {
    await startSignIn();
    const state = sessionStorage.getItem("schoolsift.oauth-state")!;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    await expect(
      handleCallback(new URLSearchParams({ state, code: "c" })),
    ).rejects.toBeInstanceOf(AuthError);
    expect(sessionStorage.getItem("schoolsift.id-token")).toBeNull();
  });

  it("callback rejects a token response with unexpected fields", async () => {
    await startSignIn();
    const state = sessionStorage.getItem("schoolsift.oauth-state")!;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          id_token: "id",
          token_type: "Bearer",
          smuggled: "field",
        }),
      }),
    );
    await expect(
      handleCallback(new URLSearchParams({ state, code: "c" })),
    ).rejects.toBeInstanceOf(AuthError);
    expect(sessionStorage.getItem("schoolsift.id-token")).toBeNull();
  });

  it("callback rejects error responses from the provider", async () => {
    await startSignIn();
    await expect(
      handleCallback(new URLSearchParams({ error: "access_denied" })),
    ).rejects.toBeInstanceOf(AuthError);
  });

  it("active household helpers round-trip and clear", () => {
    expect(getActiveHousehold()).toBeNull();
    setActiveHousehold("hh-9");
    expect(sessionStorage.getItem(ACTIVE_HOUSEHOLD_KEY)).toBe("hh-9");
    setActiveHousehold(null);
    expect(getActiveHousehold()).toBeNull();
    sessionStorage.setItem("schoolsift.id-token", "t");
    clearSession();
    expect(sessionStorage.getItem("schoolsift.id-token")).toBeNull();
  });
});
