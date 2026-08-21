import {
  Braces,
  Database,
  Search,
  Settings,
  type LucideIcon,
} from "lucide-react";
import { Link, NavLink, Outlet } from "react-router-dom";

import { useAdminTokenFromUrl } from "../features/providers/adminToken";

type PrimaryNavigationItem = {
  label: string;
  to: string;
  icon: LucideIcon;
  hierarchical?: boolean;
};

const primaryNavigation: PrimaryNavigationItem[] = [
  { label: "知识库", to: "/knowledge-bases", icon: Database, hierarchical: true },
  { label: "检索调试", to: "/retrieval", icon: Search },
  { label: "API 参考", to: "/api-reference", icon: Braces },
  { label: "设置", to: "/settings", icon: Settings },
];

export function AppShell() {
  useAdminTokenFromUrl();

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="app-header">
        <div className="app-header__inner">
          <Link className="app-brand" to="/knowledge-bases" aria-label="Traceable RAG 首页">
            <img
              alt=""
              aria-hidden="true"
              className="app-brand__mark"
              src="/brand/traceable-rag-mark.svg"
            />
            <span className="app-brand__name">Traceable RAG</span>
          </Link>

          <nav className="primary-navigation" aria-label="主导航">
            {primaryNavigation.map(({ label, to, icon: Icon, hierarchical }) => (
              <NavLink
                className={({ isActive }) =>
                  `primary-navigation__link${isActive ? " is-active" : ""}`
                }
                end={!hierarchical}
                key={to}
                to={to}
              >
                <Icon aria-hidden="true" size={17} strokeWidth={1.9} />
                <span>{label}</span>
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      <main className="app-main" id="main-content">
        <Outlet />
      </main>
    </div>
  );
}
