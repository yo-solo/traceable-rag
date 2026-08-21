import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";

const ADMIN_TOKEN_SESSION_KEY = "traceable-rag.provider-admin-token";

function sessionStorageOrNull(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function useAdminTokenFromUrl() {
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    if (!fragment.has("token")) {
      return;
    }

    const token = fragment.get("token")?.trim() ?? "";
    if (token) {
      try {
        sessionStorageOrNull()?.setItem(ADMIN_TOKEN_SESSION_KEY, token);
      } catch {
        // A blocked session store should not leave the token visible in the fragment.
      }
    }

    fragment.delete("token");
    const nextFragment = fragment.toString();
    navigate(
      {
        pathname: location.pathname,
        search: location.search,
        hash: nextFragment ? `#${nextFragment}` : "",
      },
      { replace: true },
    );
  }, [location.hash, location.pathname, location.search, navigate]);
}

export function providerAdminHeaders(): HeadersInit {
  try {
    const token = sessionStorageOrNull()?.getItem(ADMIN_TOKEN_SESSION_KEY)?.trim();
    return token ? { "X-Traceable-RAG-Admin-Token": token } : {};
  } catch {
    return {};
  }
}
