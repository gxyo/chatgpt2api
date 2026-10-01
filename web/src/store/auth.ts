"use client";

import localforage from "localforage";

export type AuthRole = "admin" | "user";

export type StoredAuthSession = {
  key: string;
  role: AuthRole;
  subjectId: string;
  name: string;
};

export const AUTH_KEY_STORAGE_KEY = "chatgpt2api_auth_key";
export const AUTH_SESSION_STORAGE_KEY = "chatgpt2api_auth_session";

const authStorage = localforage.createInstance({
  name: "chatgpt2api",
  storeName: "auth",
});

/**
 * 校验结果的内存缓存。侧边栏和每个页面都会问一次"当前是谁"，每次都打一遍
 * /auth/login + localforage 会让切页明显卡一下。把结果留在内存里，站内跳转
 * 时同步就能拿到，一个网络请求都不用发。
 * undefined = 本次会话还没校验过（首次进站 / 刷新），null = 校验过且未登录。
 */
let cachedSession: StoredAuthSession | null | undefined;
let cachedAt = 0;

export type CachedAuthSession = {
  session: StoredAuthSession | null;
  /** 校验完成的时间戳，调用方据此判断缓存新不新鲜。 */
  checkedAt: number;
};

/** 同步读缓存。返回 null 表示"还没校验过"，调用方需要等一次网络。 */
export function getCachedAuthSession(): CachedAuthSession | null {
  if (cachedSession === undefined) {
    return null;
  }
  return { session: cachedSession, checkedAt: cachedAt };
}

export function setCachedAuthSession(session: StoredAuthSession | null) {
  cachedSession = session;
  cachedAt = Date.now();
}

/** 只清内存缓存，不动持久化存储。 */
export function clearCachedAuthSession() {
  cachedSession = undefined;
  cachedAt = 0;
}

function normalizeSession(value: unknown, fallbackKey = ""): StoredAuthSession | null {
  if (!value || typeof value !== "object") {
    return null;
  }

  const candidate = value as Partial<StoredAuthSession>;
  const key = String(candidate.key || fallbackKey || "").trim();
  const role = candidate.role === "admin" || candidate.role === "user" ? candidate.role : null;
  if (!key || !role) {
    return null;
  }

  return {
    key,
    role,
    subjectId: String(candidate.subjectId || "").trim(),
    name: String(candidate.name || "").trim(),
  };
}

export function getDefaultRouteForRole(role: AuthRole) {
  return role === "admin" ? "/accounts" : "/image";
}

export async function getStoredAuthKey() {
  if (typeof window === "undefined") {
    return "";
  }
  const value = await authStorage.getItem<string>(AUTH_KEY_STORAGE_KEY);
  return String(value || "").trim();
}

export async function getStoredAuthSession() {
  if (typeof window === "undefined") {
    return null;
  }

  const [storedKey, storedSession] = await Promise.all([
    authStorage.getItem<string>(AUTH_KEY_STORAGE_KEY),
    authStorage.getItem<StoredAuthSession>(AUTH_SESSION_STORAGE_KEY),
  ]);

  const normalizedSession = normalizeSession(storedSession, String(storedKey || ""));
  if (normalizedSession) {
    if (normalizedSession.key !== String(storedKey || "").trim()) {
      await authStorage.setItem(AUTH_KEY_STORAGE_KEY, normalizedSession.key);
    }
    return normalizedSession;
  }

  if (String(storedKey || "").trim()) {
    await clearStoredAuthSession();
  }
  return null;
}

export async function setStoredAuthSession(session: StoredAuthSession) {
  const normalizedSession = normalizeSession(session);
  if (!normalizedSession) {
    await clearStoredAuthSession();
    return;
  }

  // 调用方都是刚拿 /auth/login 验过的结果（登录页、会话复检），顺手写进内存缓存。
  setCachedAuthSession(normalizedSession);

  await Promise.all([
    authStorage.setItem(AUTH_KEY_STORAGE_KEY, normalizedSession.key),
    authStorage.setItem(AUTH_SESSION_STORAGE_KEY, normalizedSession),
  ]);
}

export async function setStoredAuthKey(authKey: string) {
  const normalizedAuthKey = String(authKey || "").trim();
  if (!normalizedAuthKey) {
    await clearStoredAuthSession();
    return;
  }
  await authStorage.setItem(AUTH_KEY_STORAGE_KEY, normalizedAuthKey);
}

export async function clearStoredAuthSession() {
  clearCachedAuthSession();
  if (typeof window === "undefined") {
    return;
  }
  await Promise.all([
    authStorage.removeItem(AUTH_KEY_STORAGE_KEY),
    authStorage.removeItem(AUTH_SESSION_STORAGE_KEY),
  ]);
}

export async function clearStoredAuthKey() {
  await clearStoredAuthSession();
}
