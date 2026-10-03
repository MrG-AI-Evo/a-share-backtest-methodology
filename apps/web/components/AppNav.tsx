"use client";

import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";

const links: Array<{ href: Route; label: string; match: (path: string) => boolean }> = [
  { href: "/", label: "首页", match: (path) => path === "/" },
  { href: "/stocks", label: "个股", match: (path) => path.startsWith("/stocks") },
  { href: "/watchlist", label: "自选", match: (path) => path.startsWith("/watchlist") },
  { href: "/portfolio", label: "组合", match: (path) => path.startsWith("/portfolio") },
  { href: "/decisions", label: "决策", match: (path) => path.startsWith("/decisions") },
  { href: "/backtests", label: "回测", match: (path) => path.startsWith("/backtests") },
  { href: "/system", label: "系统", match: (path) => path.startsWith("/system") },
];

export function AppNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="主导航">
      {links.map((link) => (
        <Link
          className={link.match(pathname) ? "active" : undefined}
          href={link.href}
          aria-current={link.match(pathname) ? "page" : undefined}
          key={link.href}
        >
          {link.label}
        </Link>
      ))}
    </nav>
  );
}
