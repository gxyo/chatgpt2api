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
/** localStorage 里的同步镜像：localforage 是异步的，整页加载时首帧读不到会话，
    侧栏和页面守卫会先渲染一屏空白再等校验结果。镜像让首帧就能同步拿到身份。 */
const AUTH_SESSION_MIRROR_KEY = "chatgpt2api_auth_session_mirror";

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
    // 内存缓存为空（刚刷新或整页跳转）：先用 localStorage 镜像同步渲染外壳，
    // checkedAt 记 0，调用方拿到会话的同时会顺带触发一次后台复检。
    const mirrored = readMirroredSession();
    if (!mirrored) {
      return null;
    }
    cachedSession = mirrored;
    cachedAt = 0;
  }
  return { session: cachedSession, checkedAt: cachedAt };
}

export function setCachedAuthSession(session: StoredAuthSession | null) {
  cachedSession = session;
  cachedAt = Date.now();
}

function readMirroredSession(): StoredAuthSession | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(AUTH_SESSION_MIRROR_KEY);
    return raw ? normalizeSession(JSON.parse(raw)) : null;
  } catch {
    return null;
  }
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
  writeMirroredSession(normalizedSession);

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

function writeMirroredSession(session: StoredAuthSession) {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(AUTH_SESSION_MIRROR_KEY, JSON.stringify(session));
  } catch {
    // 隐私模式或存储被禁用：镜像写不进去就算了，退回原来的异步校验。
  }
}

export async function clearStoredAuthSession() {
  clearCachedAuthSession();
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.removeItem(AUTH_SESSION_MIRROR_KEY);
  } catch {
    // 同上，忽略存储异常。
  }
  await Promise.all([
    authStorage.removeItem(AUTH_KEY_STORAGE_KEY),
    authStorage.removeItem(AUTH_SESSION_STORAGE_KEY),
  ]);
}

export async function clearStoredAuthKey() {
  await clearStoredAuthSession();
}
