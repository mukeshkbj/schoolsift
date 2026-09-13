import { expect, test } from "@playwright/test";

test.beforeEach(async ({ request }) => {
  await request.post("http://localhost:8000/v1/demo/reset");
});

test("landing to approved demo reply end to end", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", {
      name: "Every school email, turned into the next right action.",
    }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Try the demo" }).click();

  await expect(page.getByText("Demo data").first()).toBeVisible();
  await page.getByRole("button", { name: "Process demo inbox" }).click();

  await page
    .getByRole("button", { name: /field trip/i })
    .first()
    .click();
  await expect(page.getByText("For Maya")).toBeVisible();
  await expect(page.getByText(/September 18/).first()).toBeVisible();

  const reply = page.getByLabel(/Draft reply proposal/);
  await reply.getByLabel("Body").fill("Edited: Maya will attend the museum trip. — Alex");
  await reply
    .getByRole("button", { name: "Save edit as new version" })
    .click();
  await expect(reply.getByText(/v2/)).toBeVisible();

  await reply.getByRole("button", { name: "Send demo reply" }).click();
  await expect(
    page.getByText(/Demo reply to office@maplegrove\.example/),
  ).toBeVisible();
  await expect(
    page.getByText(/recorded in the demo outbox/),
  ).toBeVisible();
});

test("escalations can never be approved", async ({ page }) => {
  await page.goto("/demo");
  await page.getByRole("button", { name: "Process demo inbox" }).click();
  await page.getByRole("button", { name: /photo day/i }).click();
  await expect(page.getByText(/Approval unavailable/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add to demo calendar" }),
  ).toHaveCount(0);

  await page.getByRole("button", { name: /materials fee/i }).click();
  await expect(page.getByText(/Reason: payment/)).toBeVisible();
  await expect(page.getByRole("button", { name: /Send demo/ })).toHaveCount(0);
});

test("mobile 320px has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/demo");
  await page.getByRole("button", { name: "Process demo inbox" }).click();
  await page.getByRole("button", { name: /field trip/i }).first().click();
  const overflow = await page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

test("keyboard reaches the primary flow with visible focus", async ({
  page,
}) => {
  await page.goto("/");
  let tries = 0;
  while (tries < 8) {
    await page.keyboard.press("Tab");
    const tag = await page.evaluate(() =>
      document.activeElement?.textContent?.trim(),
    );
    if (tag === "Try the demo") break;
    tries += 1;
  }
  await expect(page.getByRole("link", { name: "Try the demo" })).toBeFocused();
  const outline = await page.evaluate(() => {
    const el = document.activeElement;
    return el ? getComputedStyle(el).outlineWidth : "0px";
  });
  expect(outline).not.toBe("0px");
  await page.keyboard.press("Enter");
  await expect(
    page.getByRole("button", { name: "Process demo inbox" }),
  ).toBeVisible();
});
