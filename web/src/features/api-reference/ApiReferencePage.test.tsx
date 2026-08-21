import { render, screen, waitFor } from "@testing-library/react";
import { RouterProvider, createMemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

type ScalarProps = {
  configuration: Record<string, unknown>;
};

const scalarMock = vi.hoisted(() => vi.fn((_props: ScalarProps) => null));

vi.mock("@scalar/api-reference-react", () => ({
  ApiReferenceReact: scalarMock,
}));

import { appRoutes } from "../../app/router";

describe("ApiReferencePage", () => {
  it("renders the documentation-only Scalar surface", async () => {
    const router = createMemoryRouter(appRoutes, { initialEntries: ["/api-reference"] });

    render(<RouterProvider router={router} />);

    expect(await screen.findByRole("heading", { name: "API 参考" })).toBeInTheDocument();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: /OpenAPI JSON/ })).toHaveAttribute(
      "href",
      "/openapi.json",
    );
    await waitFor(() => expect(scalarMock).toHaveBeenCalled());
    const configuration = scalarMock.mock.calls.at(-1)?.[0]?.configuration ?? {};
    expect(configuration).toMatchObject({
      url: "/openapi.json",
      servers: [{ url: window.location.origin, description: "当前服务" }],
      layout: "modern",
      theme: "none",
      localization: { locale: "zh-CN" },
      withDefaultFonts: false,
      agent: { disabled: true },
      mcp: { disabled: true },
      telemetry: false,
      persistAuth: false,
      showDeveloperTools: "never",
      hideClientButton: true,
      hideTestRequestButton: true,
      hideModels: true,
    });
    expect(configuration).not.toHaveProperty("modelsSectionLabel");
    expect(configuration.customCss).toContain("--scalar-custom-header-height: 94px");
    expect(configuration.customCss).toContain(".z-overlay:has(.scalar-container)");
    expect(configuration.customCss).toContain("padding-right: 7rem !important");
    expect(configuration.customCss).toContain(".response-card .scalar-card-checkbox");
    expect(configuration.customFetch).toBeTypeOf("function");
  });
});
