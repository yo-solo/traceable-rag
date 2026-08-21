import { expect, test } from "@playwright/test";
import { resolve } from "node:path";

const primaryDestinations = ["知识库", "检索调试", "API 参考", "设置"];

test.describe("Traceable-RAG demo shell", () => {
  test("keeps API reference navigation interactive and disables online debugging", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium", "desktop API reference flow");
    const businessApiRequests: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/api/")) {
        businessApiRequests.push(`${request.method()} ${request.url()}`);
      }
    });

    await page.goto("/api-reference");
    await expect(page.getByRole("heading", { name: "API 参考", level: 1 })).toBeVisible();
    await expect(page.getByRole("note")).toHaveCount(0);
    const sidebar = page.getByRole("complementary", { name: /Retrieval API 的侧边栏/ });
    await expect(sidebar).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /Schemas/ })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /测试请求/ })).toHaveCount(0);
    await expect(page.getByText("生成 MCP", { exact: true })).toHaveCount(0);
    await expect(page.getByText("VS Code", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Cursor", { exact: true })).toHaveCount(0);

    await page.keyboard.press("Control+k");
    const search = page.getByRole("combobox", { name: "输入搜索查询" });
    await search.fill("上传并处理文档");
    await expect(page.getByRole("option", { name: /上传并处理文档/ })).toBeVisible();
    await page.keyboard.press("Escape");

    const knowledgeBaseGroup = sidebar.getByRole("button", { name: /知识库 打开分组/ });
    await expect(knowledgeBaseGroup).toHaveCSS("cursor", "pointer");
    expect(await knowledgeBaseGroup.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const target = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
      return target !== null && element.contains(target);
    })).toBe(true);
    await knowledgeBaseGroup.click();

    const createKnowledgeBase = sidebar.getByRole("button", {
      name: /创建知识库 HTTP 方法: POST/,
    });
    await expect(createKnowledgeBase).toHaveCSS("cursor", "pointer");
    await createKnowledgeBase.click();
    await expect(page).toHaveURL(/#tag\/.*\/POST\/api\/knowledge-bases$/);
    await expect(page.getByRole("heading", { name: "创建知识库", level: 3 })).toBeInViewport();
    await expect(page.locator(".response-card .scalar-card-checkbox").first()).toBeHidden();

    await page.keyboard.press("Control+k");
    await search.fill("获取文档图片");
    await page.getByRole("option", { name: /获取文档图片/ }).click();
    await expect(page.getByRole("heading", { name: "获取文档图片", level: 3 })).toBeInViewport();
    const assetSnippet = page.locator(".code-snippet")
      .filter({ hasText: "/assets/{asset_id}" })
      .first();
    await assetSnippet.hover();
    const snippetScroll = assetSnippet.locator(".custom-scroll");
    await snippetScroll.evaluate((element) => {
      element.scrollLeft = element.scrollWidth;
    });
    const codeBox = await assetSnippet.locator("pre").first().boundingBox();
    const copyBox = await assetSnippet.getByRole("button", { name: /Copy|复制/ }).first().boundingBox();
    expect(codeBox).not.toBeNull();
    expect(copyBox).not.toBeNull();
    expect(codeBox!.x + codeBox!.width).toBeLessThanOrEqual(copyBox!.x - 8);

    await page.evaluate(() => window.scrollTo(0, Math.min(3_000, document.body.scrollHeight / 2)));
    await expect(sidebar).toHaveCSS("position", "sticky");
    const sidebarBox = await sidebar.boundingBox();
    expect(sidebarBox?.y ?? 0).toBeGreaterThanOrEqual(92);
    expect(sidebarBox?.y ?? 999).toBeLessThanOrEqual(96);
    expect(businessApiRequests).toEqual([]);

    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(1);
  });

  test("leaves a loaded document through navigation and browser back without crashing", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium", "desktop document navigation flow");
    const knowledgeBase = {
      id: "kb-navigation",
      name: "ViDoRe demo collection",
      description: "验证文档详情页离开流程",
      created_at: "2026-08-09T00:00:00Z",
      updated_at: "2026-08-09T00:00:00Z",
    };
    const document = {
      id: "doc-navigation",
      knowledge_base_id: knowledgeBase.id,
      original_filename: "openstax-introduction-to-computer-science-p357.pdf",
      display_name: "OpenStax compiler stages",
      source_type: "pdf",
      page_count: 1,
      processing_status: "ready",
      processing_error: null,
      parser_provider: "mineru",
      parser_version: "vlm",
      created_at: "2026-08-09T00:00:00Z",
      updated_at: "2026-08-09T00:00:00Z",
      stats: { blocks: 1, assets: 0, chunks: 1, index_records: 1, embedding_records: 1 },
      embedding_record_count: 1,
      embedding_ready: true,
      embedding_status: "ready",
      embedding_warning: null,
      file_url: `/api/knowledge-bases/${knowledgeBase.id}/documents/doc-navigation/file`,
      web_url: `/knowledge-bases/${knowledgeBase.id}/documents/doc-navigation`,
      latest_job: null,
    };
    const blocks = {
      blocks: [{
        id: "block-navigation",
        document_id: document.id,
        page_idx: 0,
        human_page_number: 1,
        block_idx: 0,
        block_type: "paragraph",
        text: "A compiler front end performs lexical, syntax, and semantic analysis.",
        html: null,
        asset_id: null,
        bbox: [10, 20, 100, 120],
        page_size: [1000, 1400],
        is_discarded: false,
        metadata: {},
      }],
      pages: [{ page_idx: 0, human_page_number: 1, page_size: [1000, 1400], block_count: 1 }],
      assets: {},
    };
    const pdfPath = resolve(
      process.cwd(),
      "../examples/vidore/documents/openstax-introduction-to-computer-science-p357.pdf",
    );

    await page.route("**/api/**", async (route) => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      if (request.method() !== "GET") {
        await route.fulfill({ status: 405 });
      } else if (pathname === "/api/knowledge-bases") {
        await route.fulfill({
          json: {
            knowledge_bases: [{
              ...knowledgeBase,
              document_count: 1,
              ready_document_count: 1,
              processing_document_count: 0,
              failed_document_count: 0,
            }],
          },
        });
      } else if (pathname === `/api/knowledge-bases/${knowledgeBase.id}`) {
        await route.fulfill({ json: { knowledge_base: knowledgeBase } });
      } else if (pathname === `/api/knowledge-bases/${knowledgeBase.id}/documents`) {
        await route.fulfill({ json: { documents: [document] } });
      } else if (pathname === `/api/knowledge-bases/${knowledgeBase.id}/documents/${document.id}`) {
        await route.fulfill({ json: { document } });
      } else if (pathname === `/api/knowledge-bases/${knowledgeBase.id}/documents/${document.id}/blocks`) {
        await route.fulfill({ json: blocks });
      } else if (pathname === document.file_url) {
        await route.fulfill({ contentType: "application/pdf", path: pdfPath });
      } else {
        await route.fulfill({ status: 404 });
      }
    });

    const openDocument = async () => {
      await page.goto(`/knowledge-bases/${knowledgeBase.id}`);
      await page.getByRole("link", { name: document.display_name, exact: true }).click();
      await expect(page.getByRole("img", { name: `${document.display_name} PDF 第 1 页` })).toBeVisible();
    };

    await openDocument();
    await page.getByRole("navigation", { name: "主导航" })
      .getByRole("link", { name: "知识库", exact: true })
      .click();
    await expect(page).toHaveURL(/\/knowledge-bases$/);
    await expect(page.getByText("Unexpected Application Error!")).toHaveCount(0);

    await openDocument();
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`/knowledge-bases/${knowledgeBase.id}$`));
    await expect(page.getByRole("heading", { name: knowledgeBase.name, level: 1 })).toBeVisible();
    await expect(page.getByText("Unexpected Application Error!")).toHaveCount(0);
  });

  test("keeps the shared navigation and completes empty knowledge-base CRUD", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop-chromium", "desktop core flow");
    const knowledgeBaseName = `E2E Demo ${Date.now()}`;

    await page.goto("/knowledge-bases");
    const navigation = page.getByRole("navigation", { name: "主导航" });
    await expect(navigation.getByRole("link")).toHaveText(primaryDestinations);

    await page.getByRole("button", { name: "新建知识库" }).click();
    const createDialog = page.getByRole("dialog", { name: "新建知识库" });
    await createDialog.getByRole("textbox", { name: "名称" }).fill(knowledgeBaseName);
    await createDialog.getByRole("button", { name: "创建知识库" }).click();
    await expect(page.getByText(knowledgeBaseName, { exact: true })).toBeVisible();

    await navigation.getByRole("link", { name: "检索调试" }).click();
    await expect(page).toHaveURL(/\/retrieval$/);
    await expect(page.getByRole("heading", { name: "检索调试", level: 1 })).toBeVisible();
    await expect(navigation.getByRole("link")).toHaveText(primaryDestinations);

    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    await expect(page.locator(".app-header")).toHaveCSS("position", "sticky");
    await expect(page.locator(".app-header")).toHaveCSS("top", "12px");
    const headerBox = await page.locator(".app-header").boundingBox();
    expect(headerBox?.y ?? 999).toBeGreaterThanOrEqual(11);
    expect(headerBox?.y ?? 999).toBeLessThanOrEqual(13);

    await navigation.getByRole("link", { name: "知识库" }).click();
    const card = page.locator("article", { hasText: knowledgeBaseName });
    await card.getByRole("button", { name: `管理${knowledgeBaseName}` }).click();
    await page.getByRole("menuitem", { name: "删除" }).click();
    await page.getByRole("dialog", { name: "删除知识库" }).getByRole("button", { name: "确认删除" }).click();
    await expect(page.getByText(knowledgeBaseName, { exact: true })).toHaveCount(0);
  });

  test("keeps all main destinations usable without page-level horizontal overflow", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name === "desktop-chromium", "responsive viewport flow");

    for (const [path, heading] of [
      ["/knowledge-bases", "知识库"],
      ["/retrieval", "检索调试"],
      ["/api-reference", "API 参考"],
      ["/settings", "设置"],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
      await expect(page.getByRole("navigation", { name: "主导航" }).getByRole("link")).toHaveText(primaryDestinations);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, `${path} should not overflow the mobile viewport`).toBeLessThanOrEqual(1);
    }
  });
});
