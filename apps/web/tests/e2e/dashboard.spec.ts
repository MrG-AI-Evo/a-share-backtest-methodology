import { expect, test } from "@playwright/test";

test("dashboard remains usable on touch viewports", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /市场驾驶舱|本地数据服务未连接/ })).toBeVisible();
  const viewport = page.viewportSize();
  if (viewport && viewport.width < 768) {
    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  }
  await expect(page.locator("body")).not.toHaveCSS("overflow-x", "scroll");
});
