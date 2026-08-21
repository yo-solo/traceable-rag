import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { appRoutes } from "./router";

const formalRoutes = [
  ["/knowledge-bases", "知识库"],
  ["/knowledge-bases/kb-1", "知识库"],
  ["/knowledge-bases/kb-1/documents/doc-1", "知识库"],
  ["/retrieval", "检索调试"],
  ["/api-reference", "API 参考"],
  ["/settings", "设置"],
] as const;

function renderRoute(initialEntry: string) {
  const router = createMemoryRouter(appRoutes, {
    initialEntries: [initialEntry],
  });

  return {
    router,
    user: userEvent.setup(),
    ...render(<RouterProvider router={router} />),
  };
}

describe("AppShell", () => {
  it("uses the production Traceable RAG vector mark", () => {
    renderRoute("/knowledge-bases");

    const brand = screen.getByRole("link", { name: "Traceable RAG 首页" });
    expect(brand.querySelector("img.app-brand__mark")).toHaveAttribute(
      "src",
      "/brand/traceable-rag-mark.svg",
    );
  });

  it.each(formalRoutes)(
    "keeps the four primary destinations on %s and highlights %s",
    (route, activeLabel) => {
      renderRoute(route);

      const navigation = screen.getByRole("navigation", { name: "主导航" });
      const links = Array.from(navigation.querySelectorAll("a"));

      expect(links.map((link) => link.textContent)).toEqual([
        "知识库",
        "检索调试",
        "API 参考",
        "设置",
      ]);
      expect(screen.getByRole("link", { name: activeLabel })).toHaveAttribute(
        "aria-current",
        "page",
      );
    },
  );

  it("keeps the shell on an unknown route and offers a working recovery link", async () => {
    const { router, user } = renderRoute("/missing-page");

    expect(screen.getByRole("heading", { name: "页面未找到" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "主导航" })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "返回知识库" }));

    expect(router.state.location.pathname).toBe("/knowledge-bases");
    expect(screen.getByRole("heading", { name: "知识库" })).toBeInTheDocument();
  });
});
