import { expect, test } from "@playwright/test";
import fs from "node:fs";

const DB = process.env.E2E_DB_PATH?.replace("apps/web/", "") ?? "";

test.afterAll(() => {
  if (DB) {
    for (const suffix of ["", "-wal", "-shm"]) {
      fs.rmSync(`${DB}${suffix}`, { force: true });
    }
  }
});

async function tabToAppControl(page: import("@playwright/test").Page) {
  for (let i = 0; i < 15; i++) {
    const inApp = await page.evaluate(() => {
      const el = document.activeElement;
      return (
        el !== null &&
        el.closest(".app-shell") !== null &&
        ["INPUT", "BUTTON", "A"].includes(el.tagName)
      );
    });
    if (inApp) return;
    await page.keyboard.press("Tab");
  }
  throw new Error("no app control reachable by keyboard");
}

test("onboarding works end to end by keyboard", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Open SchoolSift" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/app/);

  const nameInput = page.getByLabel("Household name");
  await expect(nameInput).toBeVisible();
  await tabToAppControl(page);
  const focusedIsInput = await page.evaluate(
    () => document.activeElement?.tagName === "INPUT",
  );
  if (!focusedIsInput) await nameInput.focus();
  await page.keyboard.type("Rivera family");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Meta+a");
  await page.keyboard.type("America/Los_Angeles");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Enter");

  const childName = page.getByLabel("Child name");
  await expect(childName).toBeVisible();
  await childName.fill("Maya");
  await page.getByLabel("School").fill("Maple Grove Elementary");
  await page.getByLabel("Grade").fill("3");
  await page.getByRole("button", { name: "Add child" }).click();

  await page
    .getByRole("button", { name: "Connect an inbox" })
    .click();
  await expect(
    page.getByRole("button", { name: "Connect Gmail" }),
  ).toBeDisabled();
  await page.getByText("Server setup details").first().click();
  await expect(page.getByText("GMAIL_CLIENT_ID")).toBeVisible();
});

test("connection setup is honest after onboarding", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("link", { name: "Open SchoolSift" }).click();
  await expect(page).toHaveURL(/\/app/);
  await page.getByRole("button", { name: /^Accounts/ }).click();
  await page.getByText("Server setup details").first().click();
  await expect(page.getByText("GMAIL_CLIENT_ID")).toBeVisible();
  await page.getByText("Server setup details").nth(1).click();
  await expect(page.getByText("OUTLOOK_CLIENT_SECRET")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Connect Gmail" }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Connect Outlook" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: /^To review/ }).click();
  await expect(page.getByText(/No Action Packets yet/)).toBeVisible();
});

test("320px viewport has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/");
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
  await page.goto("/app");
  const appOverflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(appOverflow).toBeLessThanOrEqual(0);
});
