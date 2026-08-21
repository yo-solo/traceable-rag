import {
  Link,
  Navigate,
  createBrowserRouter,
  type RouteObject,
} from "react-router-dom";
import { lazy, Suspense } from "react";

import { AppShell } from "./AppShell";
import { KnowledgeBaseDetailPage } from "../features/knowledge-bases/KnowledgeBaseDetailPage";
import { KnowledgeBaseListPage } from "../features/knowledge-bases/KnowledgeBaseListPage";
import { RetrievalPage } from "../features/retrieval/RetrievalPage";
import { SetupPage } from "../features/providers/SetupPage";
import { SettingsPage } from "../features/settings/SettingsPage";

const DocumentViewerPage = lazy(async () => {
  const module = await import("../features/documents/DocumentViewerPage");
  return { default: module.DocumentViewerPage };
});

const SourceVerificationPage = lazy(async () => {
  const module = await import("../features/sources/SourceVerificationPage");
  return { default: module.SourceVerificationPage };
});

const ApiReferencePage = lazy(async () => {
  const module = await import("../features/api-reference/ApiReferencePage");
  return { default: module.ApiReferencePage };
});

function NotFoundPage() {
  return (
    <section className="not-found-page" aria-labelledby="not-found-title">
      <div className="not-found-page__code" aria-hidden="true">
        404
      </div>
      <div>
        <h1 id="not-found-title">页面未找到</h1>
        <p>这个地址不属于当前应用。你可以返回知识库继续操作。</p>
        <Link className="text-link" to="/knowledge-bases">
          返回知识库
        </Link>
      </div>
    </section>
  );
}

export const appRoutes: RouteObject[] = [
  {
    path: "setup",
    element: <SetupPage />,
  },
  {
    path: "sources/:sourceId",
    element: (
      <Suspense
        fallback={(
          <main className="source-route-state">
            <div className="async-state async-state--loading" role="status">
              <span className="async-state__spinner" aria-hidden="true" />
              <span>正在加载原文核验页…</span>
            </div>
          </main>
        )}
      >
        <SourceVerificationPage />
      </Suspense>
    ),
  },
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate replace to="/knowledge-bases" /> },
      {
        path: "knowledge-bases",
        element: <KnowledgeBaseListPage />,
      },
      {
        path: "knowledge-bases/:knowledgeBaseId",
        element: <KnowledgeBaseDetailPage />,
      },
      {
        path: "knowledge-bases/:knowledgeBaseId/documents/:documentId",
        element: (
          <Suspense
            fallback={(
              <div className="async-state async-state--loading" role="status">
                <span className="async-state__spinner" aria-hidden="true" />
                <span>正在加载文档核验页…</span>
              </div>
            )}
          >
            <DocumentViewerPage />
          </Suspense>
        ),
      },
      {
        path: "retrieval",
        element: <RetrievalPage />,
      },
      {
        path: "api-reference",
        element: (
          <Suspense
            fallback={(
              <div className="async-state async-state--loading" role="status">
                <span className="async-state__spinner" aria-hidden="true" />
                <span>正在加载 API 参考…</span>
              </div>
            )}
          >
            <ApiReferencePage />
          </Suspense>
        ),
      },
      {
        path: "settings",
        element: <SettingsPage />,
      },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];

export function createAppRouter() {
  return createBrowserRouter(appRoutes);
}
