import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { Dashboard } from "@/components/Dashboard";

vi.mock("@/lib/api", () => ({
  getDashboard: vi.fn().mockRejectedValue(new Error("API offline")),
}));

vi.mock("@/components/NetValueChart", () => ({ NetValueChart: () => <div aria-label="模拟组合净值曲线" /> }));

test("shows an honest offline state when the API is unavailable", async () => {
  render(<Dashboard />);
  expect(await screen.findByText("本地数据服务未连接")).toBeInTheDocument();
  expect(screen.getByText("不会回退到伪造行情，也不会连接任何券商。")).toBeInTheDocument();
});
