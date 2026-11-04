/* Forest Fair PWA authentication client.
 * Session token is kept in sessionStorage: it survives reloads in this tab,
 * but is cleared when the tab/window session ends. It remains JavaScript-
 * accessible, so keep the storefront free of untrusted injected scripts.
 */
(() => {
  "use strict";

  const API_BASE = "https://89.22.228.22.nip.io";
  const TOKEN_KEY = "forestfair_session_token";
  const SESSION_KEY = "forestfair_session_meta";

  function readToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY);
    } catch (_) {
      return null;
    }
  }

  function saveSession(data) {
    if (!data || typeof data.token !== "string" || !data.token) {
      throw new Error("Сервер не вернул токен сессии.");
    }
    window.sessionStorage.setItem(TOKEN_KEY, data.token);
    window.sessionStorage.setItem(SESSION_KEY, JSON.stringify({
      expires_at: data.expires_at,
      user: data.user,
      session_id: data.session_id
    }));
  }

  function clearSession() {
    try {
      window.sessionStorage.removeItem(TOKEN_KEY);
      window.sessionStorage.removeItem(SESSION_KEY);
    } catch (_) {
      // Storage may be unavailable in restricted browser contexts.
    }
  }

  async function request(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    const token = readToken();
    if (token) headers.set("Authorization", "Bearer " + token);

    let response;
    try {
      response = await fetch(API_BASE + path, {
        ...options,
        headers,
        cache: "no-store",
        credentials: "omit"
      });
    } catch (_) {
      throw new Error("Не удалось связаться с сервером авторизации. Возможно, API ещё не обновлён или сервер недоступен. Попробуйте позже.");
    }

    let payload;
    try {
      payload = await response.json();
    } catch (_) {
      throw new Error("Сервер вернул некорректный ответ.");
    }

    if (!response.ok || payload.ok !== true) {
      if (response.status === 401) clearSession();
      const messages = {
        telegram_auth_failed: "Не удалось подтвердить вход через Telegram. Откройте витрину заново из Telegram.",
        telegram_auth_unavailable: "Вход через Telegram временно недоступен.",
        unauthorized: "Сессия завершена. Войдите снова."
      };
      throw new Error(messages[payload.error] || "Ошибка авторизации. Попробуйте ещё раз.");
    }
    return payload;
  }

  async function loginTelegram() {
    const initData = window.Telegram &&
      window.Telegram.WebApp &&
      window.Telegram.WebApp.initData;

    if (!initData) {
      throw new Error("Для этого способа входа откройте витрину внутри Telegram.");
    }

    const data = await request("/api/auth/telegram", {
      method: "POST",
      body: JSON.stringify({ initData })
    });
    saveSession(data);
    return data.user;
  }

  async function me() {
    if (!readToken()) return null;
    try {
      const data = await request("/api/auth/me", { method: "GET" });
      return data.user;
    } catch (error) {
      if (error.message) throw error;
      return null;
    }
  }

  async function logout() {
    const token = readToken();
    if (!token) {
      clearSession();
      return true;
    }
    try {
      await request("/api/auth/logout", { method: "POST" });
      return true;
    } finally {
      clearSession();
    }
  }

  window.ForestFairAuth = Object.freeze({
    loginTelegram,
    me,
    logout,
    request,
    isSignedIn: () => Boolean(readToken())
  });
})();
