import { expect, test } from "@playwright/test";

const routes = [
  ["/", /市场驾驶舱|本地数据服务未连接/],
  ["/stocks", /个股研究终端/],
  ["/stocks/002594", /比亚迪|002594/],
  ["/watchlist", /自选与候选/],
  ["/portfolio", /模拟组合|模拟账户读取失败/],
  ["/decisions", /决策历史/],
  ["/system", /系统状态/],
  ["/backtests", /十年回测/],
] as const;

test("all V1 pages stay readable without page-level horizontal overflow", async ({ page }) => {
  for (const [path, heading] of routes) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
  }
});

test("navigation and safety controls meet the touch baseline", async ({ page }) => {
  await page.goto("/portfolio");
  await expect(page.getByRole("heading", { level: 1, name: "模拟组合" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/真实下单关闭/)).toBeVisible();
  const sizes = await page.getByRole("navigation", { name: "主导航" }).getByRole("link").evaluateAll(
    (links) => links.map((link) => {
      const rect = link.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }),
  );
  for (const size of sizes) {
    expect(size.width).toBeGreaterThanOrEqual(44);
    expect(size.height).toBeGreaterThanOrEqual(44);
  }

  await page.goto("/system");
  await expect(page.getByText("真实券商")).toBeVisible();
  await expect(page.getByText("自动交易", { exact: true })).toBeVisible();
  await expect(page.getByText("关闭", { exact: true }).first()).toBeVisible();
});

test("visible V1 controls meet the 44px touch baseline", async ({ page }) => {
  for (const [path, heading] of routes) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
    const undersized = await page.locator("a, button, input, select, textarea, summary").evaluateAll((elements) =>
      elements.flatMap((element) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        const ignored = element.getAttribute("aria-label") === "Open Next.js Dev Tools";
        if (ignored || style.display === "none" || style.visibility === "hidden" || rect.width === 0 || rect.height === 0) return [];
        if (rect.width >= 44 && rect.height >= 44) return [];
        return [{
          tag: element.tagName,
          text: element.textContent?.trim().slice(0, 40) ?? "",
          width: Math.round(rect.width),
          height: Math.round(rect.height),
        }];
      }),
    );
    expect(undersized, `${path} contains undersized controls`).toEqual([]);
  }
});

test("keyboard entry starts with the skip link", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop-chromium", "移动 WebKit 默认不启用硬件键盘 Tab 导航");
  await page.goto("/");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "跳到主要内容" })).toBeFocused();
});
