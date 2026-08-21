import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { AsyncState } from "./AsyncState";

describe("AsyncState", () => {
  it("shows a loading message before content", () => {
    render(
      <AsyncState loading loadingLabel="正在读取知识库">
        <p>内容</p>
      </AsyncState>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在读取知识库");
    expect(screen.queryByText("内容")).not.toBeInTheDocument();
  });

  it("shows an actionable error and retries", async () => {
    const retry = vi.fn();
    const user = userEvent.setup();
    render(
      <AsyncState error="服务暂时不可用" onRetry={retry}>
        <p>内容</p>
      </AsyncState>,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("服务暂时不可用");
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("shows an empty state instead of children", () => {
    render(
      <AsyncState empty emptyTitle="暂无文档" emptyDescription="请先上传文档。">
        <p>内容</p>
      </AsyncState>,
    );

    expect(screen.getByRole("heading", { name: "暂无文档" })).toBeInTheDocument();
    expect(screen.getByText("请先上传文档。")).toBeInTheDocument();
    expect(screen.queryByText("内容")).not.toBeInTheDocument();
  });
});
